"""Windows inbound-firewall preflight for ``albumcat serve``.

The PC half of Route A is an HTTP service the phone talks to over Wi-Fi.  If
Windows will not let our Python process accept an inbound TCP connection on
that port, the phone simply times out -- and *nothing on the PC looks wrong*,
because loopback traffic is exempt from the firewall.  ``curl
http://127.0.0.1:8899/health`` answers perfectly while the phone can never
connect.  That asymmetry is worth a check of its own.

This module never changes anything.  It reads the rule list, decides whether
some enabled inbound allow rule covers *this* executable and *this* port on
the *currently active* network profile, and -- if not -- hands back the exact
elevated command that fixes it.

Windows Firewall matches program-scoped rules on the executable **path**, so a
rule created for some other ``python.exe`` does not cover a virtualenv's copy.
That is the failure this is designed to catch.
"""

from __future__ import annotations

import locale
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

__all__ = [
    "active_profiles",
    "add_rule_command",
    "coverage",
    "local_programs",
    "parse_profiles",
    "parse_rules",
    "preflight",
]

log = logging.getLogger("albumcat.firewall")

_RANGE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")

# netsh prints localized field names on a localized Windows. Only the keys we
# actually need are listed, and every lookup tolerates a miss -- an unparsed
# field must degrade to "unknown", never to a false accusation.
_KEYS = {
    "Rule Name": ("Rule Name", "规则名称"),
    "Enabled": ("Enabled", "已启用"),
    "Direction": ("Direction", "方向"),
    "Profiles": ("Profiles", "配置文件"),
    "Protocol": ("Protocol", "协议"),
    "LocalPort": ("LocalPort", "本地端口"),
    "Program": ("Program", "程序"),
    "Action": ("Action", "操作"),
}

_YES = ("yes", "是")
_IN = ("in", "入")
_ALLOW = ("allow", "允许")
_ANY = ("any", "任何")

# netsh answers in the system UI language and, annoyingly, switches language
# depending on whether a console is attached -- the same query came back in
# English from a shell and in Chinese from this module.  Everything below is
# therefore bilingual, and profile names are folded to these canonicals so the
# rule list and the active-profile list can be compared at all.
_PROFILE_WORDS = {
    "domain": "Domain",
    "域": "Domain",
    "private": "Private",
    "专用": "Private",
    "public": "Public",
    "公用": "Public",
    "any": "",
    "任何": "",
}


def _decode(raw: bytes) -> str:
    """Decode netsh output without trusting the console codepage."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _netsh(*args: str) -> str | None:
    """Run a read-only netsh query. Returns None if netsh is unavailable."""
    try:
        proc = subprocess.run(
            ["netsh", *args],
            capture_output=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("netsh %s failed: %s", " ".join(args), exc)
        return None
    return _decode(proc.stdout) + _decode(proc.stderr)


def parse_rules(text: str) -> list[dict[str, str]]:
    """Split ``netsh ... show rule`` output into one dict per rule.

    Records are keyed off the ``Rule Name:`` header rather than off the dashed
    line, because netsh draws a dash rule *underneath* that header as well as
    between records -- treating the dashes as a separator cuts every rule in
    half and silently gives each one its neighbour's ports and program.
    """
    rules: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue  # dashed lines, blanks, stray footers
        name, _, value = line.partition(":")
        name = name.strip()
        for canonical, spellings in _KEYS.items():
            if name in spellings:
                if canonical == "Rule Name" and current:
                    rules.append(current)
                    current = {}
                current[canonical] = value.strip()
                break
    if current:
        rules.append(current)
    return rules


def _is(value: str, wanted: tuple[str, ...]) -> bool:
    low = value.strip().lower()
    return any(low == w or low.startswith(w) for w in wanted)


def _ports(spec: str) -> list[tuple[int, int]] | None:
    """Parse a netsh port spec. ``None`` means "any port"."""
    spec = spec.strip()
    if not spec or _is(spec, _ANY):
        return None
    out: list[tuple[int, int]] = []
    for part in spec.split(","):
        m = _RANGE.match(part.strip())
        if not m:
            return None  # unparsable -> treat as "any", the safe answer
        lo = int(m.group(1))
        out.append((lo, int(m.group(2)) if m.group(2) else lo))
    return out


def current_profile() -> str:
    """``Domain`` / ``Private`` / ``Public`` for the active connection."""
    return ",".join(active_profiles())


def _profile_tokens(spec: str) -> list[str]:
    """Canonicalise a ``Profiles:`` field into Domain/Private/Public names.

    An empty token means "any profile" and is returned as ``""``, which the
    caller treats as a match against everything.
    """
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(_PROFILE_WORDS.get(part.lower(), part.lower()))
    return out


def parse_profiles(text: str) -> list[str]:
    """Profile names out of ``netsh advfirewall monitor show currentprofile``.

    The output is one ``<Name> Profile:`` (``<名>配置文件:``) section per
    profile in use, each followed by the adapters sitting on it::

        Private Profile:
        ----------------------------------------------------------------------
        Radmin VPN

        Public Profile:
        ----------------------------------------------------------------------
        i-HDU
    """
    found: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(.+?)(?:\s*Profile|配置文件)\s*:", line, re.IGNORECASE)
        if not m:
            continue
        # Only accept a word we actually know.  Without this, any incidental
        # line ending in "Profile:" (a description, say) becomes a profile.
        name = _PROFILE_WORDS.get(m.group(1).strip().lower())
        if name and name.lower() not in [f.lower() for f in found]:
            found.append(name)
    return found


def active_profiles() -> list[str]:
    """Every network profile some adapter is currently attached to.

    A machine can be on several at once -- a VPN adapter on ``Private`` while
    Wi-Fi sits on ``Public`` -- and Windows picks the rule profile by the
    interface the packet arrives on.  Guessing a single one is how you end up
    with a rule that looks right and still drops the phone's packets.
    """
    text = _netsh("advfirewall", "monitor", "show", "currentprofile")
    return parse_profiles(text) if text else []


def _normalize(path: str) -> str:
    try:
        return str(Path(path.strip().strip('"')).resolve()).lower()
    except OSError:
        return path.strip().strip('"').lower()


def local_programs() -> list[str]:
    """Every executable that already has a program-scoped inbound rule."""
    text = _netsh("advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose")
    if text is None:
        return []
    return sorted({r["Program"] for r in parse_rules(text) if r.get("Program")})


def coverage(
    rules: list[dict[str, str]],
    exe: str,
    port: int,
    *,
    profiles: Sequence[str] = (),
) -> list[str]:
    """Names of enabled inbound allow rules that would admit ``exe`` on ``port``.

    ``profiles`` is the set of currently active network profiles; a rule counts
    only if it applies to at least one of them.  Pass nothing to skip the check.
    """
    want = _normalize(exe)
    hits: list[str] = []
    for rule in rules:
        if not _is(rule.get("Enabled", ""), _YES):
            continue
        if not _is(rule.get("Direction", ""), _IN):
            continue
        if not _is(rule.get("Action", ""), _ALLOW):
            continue
        if not _is(rule.get("Protocol", ""), ("tcp", "any")):
            continue
        if profiles:
            declared = _profile_tokens(rule.get("Profiles", ""))
            if declared and not any(
                d == "" or d.lower() == p.lower() for d in declared for p in profiles
            ):
                continue
        spans = _ports(rule.get("LocalPort", "Any"))
        if spans is not None and not any(lo <= port <= hi for lo, hi in spans):
            continue
        prog = rule.get("Program", "").strip()
        # A missing Program field means the rule is not program-scoped, so it
        # admits us too.  "System" is how Windows spells kernel/service rules;
        # those do not admit an arbitrary user process, so they never count.
        if prog:
            if prog.lower() == "system":
                continue
            if _normalize(prog) != want:
                continue
        hits.append(rule.get("Rule Name", "?"))
    return hits


def add_rule_command(port: int, exe: str | None = None, *, name: str = "albumcat serve") -> str:
    """The one elevated command that would unblock the LAN.

    ``profile=any`` on purpose: the rule is already pinned to one executable
    and one port, and a machine that is on a VPN *and* Wi-Fi has two active
    profiles at once, so naming one of them is a coin flip.
    """
    target = exe or sys.executable
    return (
        f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow '
        f'protocol=TCP localport={port} profile=any program="{target}"'
    )


def preflight(port: int, exe: str | None = None) -> tuple[bool | None, str]:
    """Decide whether Windows will admit inbound TCP on ``port``.

    Returns ``(ok, message)``.  ``ok`` is ``None`` when netsh is missing or its
    output could not be read -- "could not tell", deliberately distinct from
    "blocked", so a parsing gap never turns into a bogus warning.
    """
    if not sys.platform.startswith("win"):
        return None, "not Windows - the phone reaches us through whatever the host firewall allows"

    target = exe or sys.executable
    text = _netsh("advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose")
    if text is None:
        return None, "could not run netsh - skipping the firewall check"

    rules = parse_rules(text)
    if not rules:
        # netsh answered but we understood nothing -- almost certainly a
        # localization we do not know yet.  Say so instead of guessing.
        return None, "could not read the firewall rules (netsh output was not recognised)"

    profiles = active_profiles()
    hits = coverage(rules, target, port, profiles=profiles)
    if hits:
        return True, f"inbound TCP {port} is already allowed for {Path(target).name} ({hits[0]})"

    where = f" (active profiles: {', '.join(profiles)})" if profiles else ""
    return False, (
        f"no inbound rule covers {Path(target).name} on TCP {port}{where} - the phone will "
        f"probably time out even though http://127.0.0.1:{port}/health works.\n"
        f"    fix (needs an Administrator prompt):\n"
        f"      {add_rule_command(port, target)}\n"
        f"    or double-click tools\\allow_firewall.cmd"
    )
