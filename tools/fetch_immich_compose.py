"""Fetch the current Immich compose file + env template into ``immich/``.

Immich only ships Docker Compose installs, and its compose file changes between
releases, so pull the ones matching the latest release rather than pasting a
stale copy. Downloads go through Python because Windows SChannel TLS is broken
on this machine.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "immich"

BASE = "https://github.com/immich-app/immich/releases/latest/download"
FILES = {
    "docker-compose.yml": "docker-compose.yml",
    "example.env": ".env.example",
}

# The default UPLOAD_LOCATION is ./library next to the compose file, and C: only
# has ~22 GB free here, so pin it to the data drive explicitly.
UPLOAD_HINT = r"E:\AlbumCategories\immich\library"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "albumcat-setup"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for remote, local in FILES.items():
        url = f"{BASE}/{remote}"
        data = fetch(url)
        out = DEST / local
        out.write_bytes(data)
        print(f"{url}\n  -> {out}  ({len(data)} bytes)")

    env = (DEST / ".env.example").read_text(encoding="utf-8")
    if "UPLOAD_LOCATION" in env:
        print(
            "\nSet UPLOAD_LOCATION in your .env before starting. Suggested for this"
            f" machine (C: is nearly full):\n  UPLOAD_LOCATION={UPLOAD_HINT}"
        )
    print(
        "\nnext:\n"
        f"  cd {DEST}\n"
        "  copy .env.example .env      then edit UPLOAD_LOCATION\n"
        "  docker compose up -d"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
