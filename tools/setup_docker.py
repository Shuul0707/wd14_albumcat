"""Install Docker Desktop onto E:, after the VirtualMachinePlatform reboot.

Why a Python script and not a .cmd: this machine's Windows SChannel TLS is
broken (``SEC_E_NO_CREDENTIALS``), so anything that downloads through
WinHTTP/.NET fails. Python's own TLS stack works, which is how the model and
the Docker installer were fetched. If ``wsl --update`` can't fetch the WSL2
kernel, this falls back to downloading the MSI with urllib.

Run it any time after the reboot that followed enabling VirtualMachinePlatform::

    .venv\\Scripts\\python.exe tools\\setup_docker.py

It re-launches itself elevated (one UAC prompt) and logs to
``.tmp\\setup_docker.log``.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

UA = "albumcat-setup"

INSTALLER = Path(r"E:\DockerSetup\DockerDesktopInstaller.exe")
INSTALL_DIR = Path(r"E:\Docker")
DATA_ROOT = Path(r"E:\DockerData")
LOG = Path(__file__).resolve().parents[1] / ".tmp" / "setup_docker.log"
WSL_KERNEL = Path(r"C:\Windows\System32\lxss\tools\kernel")

_log_fh = None


def log(msg: str = "") -> None:
    line = str(msg)
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def run(cmd: list[str], label: str, timeout: int = 1800) -> int:
    """Run a command, tee its output into the log, return the exit code."""
    log(f"\n$ {' '.join(cmd)}")
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        log(f"[{label}] command not found")
        return 127
    except subprocess.TimeoutExpired:
        log(f"[{label}] TIMEOUT after {timeout}s")
        return 124
    for stream, name in ((p.stdout, "out"), (p.stderr, "err")):
        for line in (stream or "").splitlines():
            if line.strip():
                log(f"  [{name}] {line.rstrip()}")
    log(f"[{label}] exit={p.returncode}")
    return p.returncode


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def relaunch_elevated() -> int:
    params = " ".join(f'"{a}"' for a in [str(Path(__file__).resolve()), *sys.argv[1:]])
    log("not elevated -- asking for administrator rights (one UAC prompt)...")
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    if rc <= 32:
        log(f"elevation failed (ShellExecuteW returned {rc})")
        return 1
    return 0


def wsl_ready() -> bool:
    """True when WSL can actually run a distro.

    Modern WSL ships as an app that carries its own kernel, so the legacy
    ``lxss\\tools\\kernel`` file is not a reliable signal -- ask WSL instead.
    """
    if WSL_KERNEL.exists():
        return True
    try:
        p = subprocess.run(
            ["wsl", "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except Exception:  # noqa: BLE001
        return False
    return p.returncode == 0


def _github_latest_wsl_msi() -> tuple[str, str] | None:
    """Newest WSL x64 MSI, without touching the GitHub API.

    The unauthenticated API is rate-limited (we got HTTP 403 "rate limit
    exceeded"), but the ``/releases/latest`` redirect and the
    ``expanded_assets`` fragment are ordinary page loads and are not.
    """
    req = urllib.request.Request(
        "https://github.com/microsoft/WSL/releases/latest", headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tag = r.geturl().rstrip("/").rsplit("/", 1)[-1]

    req = urllib.request.Request(
        f"https://github.com/microsoft/WSL/releases/expanded_assets/{tag}",
        headers={"User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")

    log(f"  latest WSL release: {tag}")
    for href in re.findall(r'href="([^"]*/releases/download/[^"]+)"', html):
        name = href.rsplit("/", 1)[-1]
        if name.endswith(".msi") and "x64" in name:
            url = href if href.startswith("http") else f"https://github.com{href}"
            return url, name
    return None


def ensure_wsl_kernel() -> bool:
    if wsl_ready():
        log("WSL already usable")
        return True

    log("WSL not usable; trying `wsl --update`")
    run(["wsl", "--update"], "wsl --update")
    run(["wsl", "--install", "--no-distribution"], "wsl --install --no-distribution")
    if wsl_ready():
        log("WSL ready after wsl --update")
        return True

    log("still not usable; downloading the WSL MSI directly with Python (bypasses SChannel)")
    try:
        setup_dir = Path(r"E:\DockerSetup")

        # reuse an already-downloaded MSI -- the download needs no admin, so it is
        # often done separately from the (elevated) install
        dest = next(iter(sorted(setup_dir.glob("wsl.*x64.msi"))), None)
        if dest:
            log(f"  reusing {dest} ({dest.stat().st_size / 1024**2:.1f} MiB)")
        else:
            found = _github_latest_wsl_msi()
            if not found:
                raise RuntimeError("no x64 .msi asset in the latest microsoft/WSL release")
            url, name = found

            dest = setup_dir / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(
                urllib.request.Request(url, headers={"User-Agent": UA}), timeout=300
            ) as r, open(dest, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
            log(f"  downloaded {dest} ({dest.stat().st_size / 1024**2:.1f} MiB)")

        run(["msiexec", "/i", str(dest), "/qn", "/norestart"], "msiexec wsl")
    except Exception as exc:  # noqa: BLE001
        log(f"  WSL MSI path failed: {exc!r}")

    if wsl_ready():
        log("WSL ready after MSI install")
        return True
    log("WARNING: WSL still not ready -- the Docker Desktop install can still succeed,")
    log("         but its wsl-2 backend needs WSL. Re-run this script once WSL works.")
    return False


def main(argv: list[str] | None = None) -> int:
    global _log_fh
    argv = list(sys.argv[1:] if argv is None else argv)

    if not is_admin():
        return relaunch_elevated()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    if "--wsl-only" in argv:
        # keep appending: repeated WSL attempts are the normal case here
        _log_fh = open(LOG, "a", encoding="utf-8")
    else:
        _log_fh = open(LOG, "w", encoding="utf-8")
    log(f"==== {datetime.now():%Y-%m-%d %H:%M:%S} elevated (argv={argv}) ====")

    log("--- environment ---")
    log(f"  installer     : {INSTALLER}  (exists={INSTALLER.exists()})")
    log(f"  install dir   : {INSTALL_DIR}")
    log(f"  wsl data root : {DATA_ROOT}")
    run(["wsl", "--status"], "wsl --status")

    log("\n--- vmcompute present? ---")
    log(f"  {Path(r'C:\Windows\System32\vmcompute.exe').exists()}")

    wsl_ok = ensure_wsl_kernel()
    if "--wsl-only" in argv:
        log(f"\nWSL ready = {wsl_ok}")
        return 0 if wsl_ok else 1

    if not INSTALLER.exists():
        log(f"FATAL: installer not found at {INSTALLER}")
        return 2

    log("\n--- installing Docker Desktop ---")
    rc = run(
        [
            str(INSTALLER),
            "install",
            "--quiet",
            "--accept-license",
            "--backend=wsl-2",
            f"--installation-dir={INSTALL_DIR}",
            f"--wsl-default-data-root={DATA_ROOT}",
        ],
        "docker desktop installer",
    )
    log(f"  (3010 would mean 'installed, restart required')")

    log("\n--- verification ---")
    run(["wsl", "-l", "-v"], "wsl -l -v")
    docker = Path(r"E:\Docker\resources\bin\docker.exe")
    log(f"  {docker} exists={docker.exists()}")
    if docker.exists():
        run([str(docker), "--version"], "docker --version")
        run([str(docker), "compose", "version"], "docker compose version")

    log(f"\nDONE (installer exit={rc}) -- log at {LOG}")
    return 0


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Windows only")
    raise SystemExit(main())
