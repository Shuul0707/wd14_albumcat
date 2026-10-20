"""Prove the Aves fork tagged photos in its own database and left the files alone.

WHY THIS EXISTS
---------------
The AI tags live in Aves' own SQLite database (`appTags` table); the image bytes
are never touched. That leaves exactly two claims that have to be checked on a
real device, and neither one can be checked from the desktop:

  1. the tags really landed in `appTags` -- which means the Dart service
     reached the PC server, mapped the ids back to entries, and persisted them
     through the sqflite layer into a table the stock app does not know about
  2. the photos on disk are untouched -- same sha256, same size, same mtime

Claim 2 is the one the user actually cares about, so this script always
reports it, pass or fail.

WHY TWO PHASES
--------------
The comparison has to happen around a manual step (tapping the AI button in
Aves), so the "before" state is written to a JSON file:

    python tools/verify_aves_tags.py snapshot     # before
    ...  open Aves, tap the AI button, wait  ...
    python tools/verify_aves_tags.py verify       # after

Usage:
    python tools/verify_aves_tags.py [snapshot|verify] [--serial emulator-5554]
                                     [--package deckers.thibault.aves.libre.debug]
                                     [--probe-dir /sdcard/DCIM/AlbumcatProbe]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".tmp" / "aves-tags-state.json"
# The post-tagging fingerprints go to their own file: `verify` used to rewrite
# the baseline, so running it twice compared the second run against the first
# run's output and quietly stopped testing anything.
STATE_AFTER = ROOT / ".tmp" / "aves-tags-state-after.json"
DBDIR = ROOT / ".tmp" / "aves-db"

SDK = Path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or r"E:\Android\sdk")
ADB = SDK / "platform-tools" / "adb.exe"

DEFAULT_PKG = "deckers.thibault.aves.libre.debug"
DEFAULT_PROBE = "/sdcard/DCIM/AlbumcatProbe"

# The four rehearsal photos, and the character the tagger is expected to name.
# aves-probe-reimu.png is the same picture re-encoded as PNG, which is here to
# prove the fork's path is format-agnostic -- the JPEG-only rule belonged to
# the old XMP writer, not to tagging.
EXPECTED = {
    "aves-probe-aya.jpg": "Shameimaru Aya",
    "aves-probe-reimu.jpg": "Hakurei Reimu",
    "aves-probe-reimu.png": "Hakurei Reimu",
    "aves-probe-teto.jpg": "Kasane Teto",
}

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line, flush=True)
    return ok


def run(args, binary=False, timeout=180):
    proc = subprocess.run(
        [str(ADB), *args] if not binary else [str(ADB), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if binary:
        return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


class Device:
    def __init__(self, serial):
        self.serial = serial

    def adb(self, *args, binary=False, timeout=180):
        base = [str(ADB)]
        if self.serial:
            base += ["-s", self.serial]
        proc = subprocess.run(
            base + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
        if binary:
            return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"),
        )

    def shell(self, cmd, binary=False, timeout=180):
        return self.adb("shell", cmd, binary=binary, timeout=timeout)

    def out(self, cmd):
        return self.shell(cmd)[1].strip()

    def root(self):
        """Return True if adbd is root, which is what lets us read /data."""
        self.adb("root")
        time.sleep(2)
        return self.out("id -u").strip() == "0"

    def check_alive(self):
        rc, out, _ = self.adb("devices")
        if self.serial not in out:
            raise SystemExit(f"device {self.serial!r} is not connected:\n{out}")
        boot = self.out("getprop sys.boot_completed")
        if boot != "1":
            raise SystemExit(f"device is still booting (sys.boot_completed={boot!r})")


def list_probe_files(device, probe_dir):
    rc, out, _ = device.shell(f"ls -1 {probe_dir}")
    if rc != 0 or not out.strip():
        raise SystemExit(f"no files under {probe_dir} -- run tools/make_aves_probe.py and push them")
    return [n for n in out.split() if n.lower().endswith((".jpg", ".jpeg", ".png"))]


def file_fingerprint(device, path):
    """Return (sha256, size, mtime) for one file on the device.

    `sha256sum` and `stat -c` both come from toybox, so this works on a stock
    Android without pushing anything.  `%Y` is the mtime in seconds -- the
    value the gallery sorts by, and the one a rewrite would clobber.
    """
    rc, out, err = device.shell(f"sha256sum {path}")
    if rc != 0 or not out.strip():
        raise SystemExit(f"sha256sum failed on {path}: {err!r}")
    digest = out.split()[0]

    rc, out, err = device.shell(f"stat -c '%s %Y' {path}")
    if rc != 0 or not out.strip():
        raise SystemExit(f"stat failed on {path}: {err!r}")
    size, mtime = out.split()[:2]
    return {"sha256": digest, "size": int(size), "mtime": int(mtime)}


def snapshot(device, probe_dir, path=STATE):
    files = list_probe_files(device, probe_dir)
    state = {"dir": probe_dir, "files": {}}
    for name in files:
        state["files"][name] = file_fingerprint(device, f"{probe_dir}/{name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"snapshotted {len(files)} file(s) from {probe_dir} -> {path.name}")
    for name, fp in sorted(state["files"].items()):
        print(f"    {name:26s} {fp['size']:>9,d} B  mtime={fp['mtime']}  {fp['sha256'][:16]}")
    return state


def pull_db(device, pkg):
    """Copy Aves' SQLite database off the device.

    The -wal and -shm sidecars matter: sqflite writes in WAL mode, and a row
    committed moments ago lives in the -wal file, not the .db.  Pulling only
    the .db would show a stale database and make a working feature look broken.
    """
    DBDIR.mkdir(parents=True, exist_ok=True)
    remote = f"/data/data/{pkg}/databases"
    got = {}
    for suffix in ("", "-wal", "-shm"):
        name = f"metadata.db{suffix}"
        local = DBDIR / name
        if local.exists():
            local.unlink()
        rc, out, err = device.adb("pull", f"{remote}/{name}", str(local), timeout=180)
        if local.exists():
            got[name] = local.stat().st_size
    if "metadata.db" not in got:
        raise SystemExit(
            f"could not pull {remote}/metadata.db -- is {pkg} installed and has it been "
            f"launched at least once?  (adb root is required)"
        )
    return got


def expected_schema_version(default=18):
    """The fork's schema version, read from the Dart source instead of hardcoded.

    This check used to assert `== 17`, which turned a *correct* database into a
    failure the moment the fork added a migration (the Chinese-CLIP vectors took
    the schema to 18).  Reading `version: N` out of `db_sqflite.dart` keeps the
    check honest as the fork grows; the fallback is only used when the script is
    run outside a checkout.
    """
    dart = ROOT / "aves" / "lib" / "model" / "db" / "db_sqflite.dart"
    try:
        match = re.search(r"^\s*version:\s*(\d+),\s*$", dart.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return default
    return int(match.group(1)) if match else default


def read_app_tags(db_path):
    """Return (user_version, cols, rows, tables) for the fork's own table.

    `rows` is None when the table does not exist at all, which is the shape a
    stock (unforked) Aves database has -- reporting that as "0 rows" would be
    misleading, since the two mean very different things.

    Each row is (id, path, tags).  The path comes from Aves' `entry` table
    (singular -- the first version of this script looked for `entries`, found
    nothing, and every row printed title=None).  It is what lets a tag be tied
    to *one* photo: asserting against the union of every tag in the table would
    pass as long as some row anywhere carried the character.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        version = cur.execute("pragma user_version").fetchone()[0]
        tables = {r[0] for r in cur.execute("select name from sqlite_master where type='table'")}
        if "appTags" not in tables:
            return version, None, None, tables
        paths = {}
        for name in ("entry", "entries"):
            if name not in tables:
                continue
            cols = {r[1] for r in cur.execute(f"pragma table_info({name})")}
            if "path" in cols:
                paths = {r[0]: r[1] for r in cur.execute(f"select id, path from {name}")}
            break
        rows = [
            (rid, paths.get(rid), [t for t in (tags or "").split(";") if t])
            for rid, tags in cur.execute("select id, tags from appTags order by id")
        ]
        return version, ["id", "path", "tags"], rows, tables
    finally:
        con.close()


def do_snapshot(device, args):
    snapshot(device, args.probe_dir)


def do_verify(device, args):
    if not STATE.exists():
        raise SystemExit(f"{STATE} is missing -- run the `snapshot` phase first")
    before = json.loads(STATE.read_text(encoding="utf-8"))

    # ---- claim 2: the photos are untouched ---------------------------- #
    print("\n-- files on disk --")
    after = snapshot(device, args.probe_dir, path=STATE_AFTER)
    untouched = True
    for name, fp in sorted(before["files"].items()):
        now = after["files"].get(name)
        if now is None:
            untouched = False
            check(f"{name} still exists", False, "it disappeared")
            continue
        same_hash = now["sha256"] == fp["sha256"]
        same_mtime = now["mtime"] == fp["mtime"]
        untouched &= same_hash and same_mtime
        check(
            f"{name}: bytes and mtime unchanged",
            same_hash and same_mtime,
            f"sha256 {'same' if same_hash else 'CHANGED'} ({fp['sha256'][:16]} -> {now['sha256'][:16]}), "
            f"mtime {'same' if same_mtime else 'CHANGED'} ({fp['mtime']} -> {now['mtime']})",
        )

    # ---- claim 1: the tags are in Aves' database ---------------------- #
    print("\n-- Aves database --")
    got = pull_db(device, args.package)
    print("    pulled: " + ", ".join(f"{k} ({v:,d} B)" for k, v in sorted(got.items())))
    version, _, rows, tables = read_app_tags(DBDIR / "metadata.db")
    expected = expected_schema_version()
    check(
        f"schema version is at least {expected} (the fork's migrations)",
        version >= expected,
        f"user_version={version}",
    )
    check("appTags table exists", rows is not None, "" if rows is not None else "the table is absent")
    check(
        "appVectors table exists (Chinese-CLIP side table)",
        "appVectors" in tables,
        "" if "appVectors" in tables else "the table is absent -- did the 17->18 migration run?",
    )
    rows = rows or []
    check("appTags has at least one row", len(rows) > 0, f"{len(rows)} row(s)")
    for rid, path, tags in rows:
        print(f"    id={rid}  path={path}  tags={tags}")

    # Map each probe file to the tags Aves holds for *that* file.  MediaStore
    # reports paths under /storage/emulated/0, while the caller may pass the
    # /sdcard alias, so normalise before matching.
    probe_norm = re.sub(r"^/sdcard", "/storage/emulated/0", args.probe_dir.rstrip("/"))
    tags_by_name = {}
    for _, path, tags in rows:
        if path and path.startswith(probe_norm + "/"):
            tags_by_name[os.path.basename(path)] = tags

    for name, character in sorted(EXPECTED.items()):
        if name not in before["files"]:
            continue
        # `tree_in_subject = true` on the server means the flat tag carries the
        # hierarchy, so 角色|Name is what lands in Aves -- which is exactly what
        # makes Aves group every character under one 角色 node.  The bare label
        # must NOT be accepted here: that is the shape a silent loss of the
        # grouping produces, which is the bug this check was written for.
        tags = tags_by_name.get(name)
        hit = tags is not None and f"角色|{character}" in tags
        check(
            f"{name} tagged as 角色|{character}",
            hit,
            f"entry tags: {tags}" if tags is not None else f"no appTags row for {name}",
        )

    stray = sorted({t for _, _, tags in rows for t in tags} - {f"角色|{c}" for c in EXPECTED.values()})
    print(f"    other tags in the table (other photos): {stray}")

    print()
    if results and all(ok for _, ok, _ in results):
        print(f"All {len(results)} checks passed.")
        return 0
    failed = [label for label, ok, _ in results if not ok]
    print(f"{len(failed)} of {len(results)} checks FAILED:")
    for label in failed:
        print(f"  - {label}")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", nargs="?", default="verify", choices=["snapshot", "verify"])
    ap.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", "emulator-5554"))
    ap.add_argument("--package", default=DEFAULT_PKG)
    ap.add_argument("--probe-dir", default=DEFAULT_PROBE)
    args = ap.parse_args()

    if not ADB.exists():
        raise SystemExit(f"adb not found at {ADB} -- set ANDROID_SDK_ROOT")

    device = Device(args.serial)
    device.check_alive()
    if not device.root():
        raise SystemExit("adb root is required to read /data/data/<pkg>/databases")

    print(f"device={args.serial}  package={args.package}  phase={args.phase}")
    if args.phase == "snapshot":
        do_snapshot(device, args)
        return 0
    return do_verify(device, args)


if __name__ == "__main__":
    sys.exit(main())
