"""Run every self-check in the repo and say whether they all passed.

Usage:
    tools\\check_all.cmd            (or: python tools\\check_all.py)
    python tools\\check_all.py -k runner      only the ones whose name matches
    python tools\\check_all.py --list

These are the checks behind the claims in the docs: the plugin chain, the
phone-side pipeline (plus the retired XMP / Immich write-out paths, which still
run), the firewall preflight and the environment.  They are deliberately
separate scripts rather than one test suite, because each one needs a different
harness (the two JVM ones start a real `albumcat serve` and run the ONNX model,
so a full pass takes a couple of minutes).

Everything is launched as a subprocess with its output streaming straight
through, so a failure is visible in context instead of buried in a summary.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
TMP = ROOT / ".tmp"

# (label, argv-from-repo-root).  Ordered cheapest-first so an obvious breakage
# shows up in seconds rather than after the ONNX model has loaded twice.
CHECKS: list[tuple[str, list[str]]] = [
    ("XMP 读写：Python 与 Kotlin 两份实现逐字节对拍",
     ["tools/check_kotlin_xmp.py"]),
    ("feature 插件链路（768 维 ONNX head）",
     ["tools/check_feature_plugin.py"]),
    ("XMP 边界情况（Python）",
     [".tmp/test_xmp.py"]),
    ("PNG 的 XMP（iTXt 块，Python）",
     [".tmp/test_png_xmp.py"]),
    ("别人的 XMP 不被我们抹掉",
     [".tmp/test_foreign_xmp.py"]),
    ("防火墙 preflight 的 netsh 解析（中英双语）",
     [".tmp/test_firewall.py"]),
    ("手机端整条链路彩排（Python 侧）",
     ["tools/simulate_phone.py"]),
    ("App 的 HTTP 客户端与撤销日志（桌面 JVM）",
     ["tools/check_android_client.py"]),
    ("App 的写入主循环 TagRunner（桌面 JVM）",
     ["tools/check_android_runner.py"]),
    ("环境自检",
     ["tools/albumcat.cmd", "doctor"]),
]


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
            # The .tmp ones are scratch files; a missing one is a real failure
            # (someone cleaned .tmp) but it should say so plainly.
            print(f"  [SKIP] 找不到 {target.relative_to(ROOT)}")
            bad.append((label, f"missing {target.relative_to(ROOT)}"))
            continue

        if target.suffix.lower() == ".cmd":
            argv2 = ["cmd", "/c", str(target), *cmd[1:]]
        else:
            argv2 = [python, "-u", str(target), *cmd[1:]]

        started = time.perf_counter()
        try:
            proc = subprocess.run(argv2, cwd=str(ROOT), env=env)  # noqa: S603
        except KeyboardInterrupt:
            print("\n  被打断。")
            return 130
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
        print(f"  全部 {len(ok)} 项通过 —— 推理后端、插件、手机端链路和防火墙预检"
              f"都在真实的代码上验过了。")
        return 0

    print(f"  {len(ok)} 项通过，{len(bad)} 项失败：")
    for label, why in bad:
        print(f"    - {label}  ({why})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
