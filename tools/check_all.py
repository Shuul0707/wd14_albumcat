"""Run every self-check in the repo and say whether they all passed.

Usage:
    tools\\check_all.cmd            (or: python tools\\check_all.py)
    python tools\\check_all.py -k firewall     only the ones whose name matches
    python tools\\check_all.py --list

Four checks, one per thing the docs claim -- and nothing else, because these are
the paths that are actually wired up:

  * the feature plugin chain (a head is trained, written, loaded back)
  * the firewall preflight parser (English + Chinese netsh output)
  * the environment (`albumcat doctor`: model, packages, backend)
  * the /plan_batch HTTP contract the phone parses. This is the expensive one:
    it starts a real `albumcat serve` on a free port, generates the probe images
    if they are missing, and runs the ONNX model over them. It needs the weights
    in models/ -- `tools\\download_model.py` fetches them.

Everything is launched as a subprocess with its output streaming straight
through, so a failure is visible in context instead of buried in a summary.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
TMP = ROOT / ".tmp"

# (label, argv-from-repo-root).  Ordered cheapest-first so an obvious breakage
# shows up in seconds rather than after the ONNX model has loaded twice.
CHECKS: list[tuple[str, list[str]]] = [
    ("feature 插件链路（768 维 ONNX head）",
     ["tools/check_feature_plugin.py"]),
    ("防火墙 preflight 的 netsh 解析（中英双语）",
     ["tools/check_firewall.py"]),
    ("环境自检",
     ["tools/albumcat.cmd", "doctor"]),
    ("/plan_batch 的 HTTP 契约（真的起一个 serve 跑一遍模型）",
     ["tools/check_plan_batch.py"]),
]

BATCH_CHECK = ROOT / "tools" / "check_plan_batch.py"
SERVE_LOG = TMP / "check_all-serve.log"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_serve(python: str, env: dict, timeout: float = 300.0):
    """Start `albumcat serve` on loopback and wait until /health answers.

    The HTTP check needs a live server; rather than make the caller remember to
    start one (and rather than leave check_all dependent on a hand-started
    process), check_all owns it for the duration of that one check.
    """
    probe = TMP / "aves-probe"
    if not any(probe.glob("*.jpg")):
        print("  .tmp/aves-probe 是空的，先生成探针图…")
        subprocess.run(  # noqa: S603
            [python, "-u", str(TOOLS / "make_aves_probe.py")],
            cwd=str(ROOT), env=env, check=False,
        )

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    TMP.mkdir(parents=True, exist_ok=True)
    # The server's own chatter goes to a file: it is useful when the check
    # fails, noise when it does not.
    logfile = open(SERVE_LOG, "wb")
    proc = subprocess.Popen(  # noqa: S603
        [python, "-u", "-m", "albumcat", "serve",
         "--host", "127.0.0.1", "--port", str(port), "--no-warmup"],
        cwd=str(ROOT), env=env, stdout=logfile, stderr=subprocess.STDOUT,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            logfile.close()
            raise RuntimeError(
                f"serve 自己退出了（exit {proc.returncode}）；日志在 "
                f"{SERVE_LOG.relative_to(ROOT)}"
            )
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                if response.status == 200:
                    return proc, base, logfile
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    proc.kill()
    logfile.close()
    raise RuntimeError(f"serve 在 {timeout:.0f} 秒内没有就绪；日志在 "
                       f"{SERVE_LOG.relative_to(ROOT)}")


def _stop_serve(proc, logfile) -> None:
    if logfile is not None:
        try:
            logfile.close()
        except OSError:
            pass
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-k", "--only", default="",
                    help="只跑名字里含这个子串的检查")
    ap.add_argument("--list", action="store_true", help="列出检查项就退出")
    args = ap.parse_args(argv)

    if args.list:
        for label, cmd in CHECKS:
            print(f"  {cmd[0]:<34} {label}")
        return 0

    selected = [(label, cmd) for label, cmd in CHECKS
                if args.only.lower() in label.lower()
                or args.only.lower() in cmd[0].lower()]
    if not selected:
        allowed = ", ".join(cmd[0] for _l, cmd in CHECKS)
        sys.exit(f"没有匹配 {args.only!r} 的检查项；可选：{allowed}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT / "src")
    python = sys.executable

    ok: list[str] = []
    bad: list[tuple[str, str]] = []   # (label, why)

    width = 78
    for label, cmd in selected:
        target = ROOT / cmd[0]
        print()
        print("-" * width)
        print(f"  {label}")
        print(f"  {' '.join(cmd)}")
        print("-" * width)
        sys.stdout.flush()

        if not target.exists():
            print(f"  [SKIP] 找不到 {target.relative_to(ROOT)}")
            bad.append((label, f"missing {target.relative_to(ROOT)}"))
            continue

        server = logfile = None
        extra: list[str] = []
        if target == BATCH_CHECK:
            try:
                server, base, logfile = _start_serve(python, env)
            except RuntimeError as exc:
                print(f"  [FAIL] {exc}")
                bad.append((label, str(exc)))
                continue
            extra = [base]

        if target.suffix.lower() == ".cmd":
            argv2 = ["cmd", "/c", str(target), *cmd[1:]]
        else:
            argv2 = [python, "-u", str(target), *cmd[1:], *extra]

        started = time.perf_counter()
        try:
            proc = subprocess.run(argv2, cwd=str(ROOT), env=env)  # noqa: S603
        except KeyboardInterrupt:
            print("\n  被打断。")
            _stop_serve(server, logfile)
            return 130
        finally:
            _stop_serve(server, logfile)
        took = time.perf_counter() - started

        if proc.returncode == 0:
            ok.append(label)
            print(f"  [ OK ] {label}  ({took:.1f}s)")
        else:
            bad.append((label, f"exit {proc.returncode}"))
            print(f"  [FAIL] {label}  (exit {proc.returncode}, {took:.1f}s)")
        sys.stdout.flush()

    print()
    print("=" * width)
    if not bad:
        print(f"  全部 {len(ok)} 项通过 —— 插件链路、防火墙预检、环境自检和 "
              f"/plan_batch 契约都在真实的代码上验过了。")
        return 0

    print(f"  {len(ok)} 项通过，{len(bad)} 项失败：")
    for label, why in bad:
        print(f"    - {label}  ({why})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
