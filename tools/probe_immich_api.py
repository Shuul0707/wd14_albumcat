#!/usr/bin/env python3
"""Fetch Immich's OpenAPI spec and print the tag-related API surface.

Route B needs to put character names *into* Immich. Rather than guess at the
endpoints, pull the machine-readable spec and read exactly what is available.

Network note: this machine's PowerShell/curl TLS is broken, so Python does the
fetch. The spec is a few MB, so it is cached under ``data/`` and only the
tag-relevant slice is printed.

Usage::

    python tools/probe_immich_api.py            # cached if present
    python tools/probe_immich_api.py --refresh  # re-download the spec
    python tools/probe_immich_api.py --grep asset   # show paths matching a word
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE = PROJECT_ROOT / "data" / "immich-openapi.json"

SOURCES = [
    "https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json",
    "https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.yaml",
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) albumcat-immich-probe/1.0"


def fetch(refresh: bool) -> dict:
    if CACHE.exists() and not refresh:
        print(f"using cached spec: {CACHE}")
        return json.loads(CACHE.read_text(encoding="utf-8"))

    last: Exception | None = None
    for url in SOURCES:
        if url.endswith(".yaml"):
            continue  # we only parse JSON here
        try:
            print(f"downloading {url}")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
            spec = json.loads(raw)
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_bytes(raw)
            print(f"  saved {len(raw) / 1024 / 1024:.1f} MB -> {CACHE}")
            return spec
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  failed: {exc!r}")
    raise SystemExit(f"could not fetch the spec: {last!r}")


def resolve(spec: dict, schema: dict, depth: int = 0) -> str:
    """Render a schema compactly, following $ref one or two levels down."""
    if depth > 2 or not isinstance(schema, dict):
        return "..."

    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        target = spec.get("components", {}).get("schemas", {}).get(name)
        if target is None:
            return name
        rendered = resolve(spec, target, depth + 1)
        return f"{name}{rendered}"

    kind = schema.get("type")
    if kind == "array":
        return f"[{resolve(spec, schema.get('items', {}), depth + 1)}]"
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        bits = []
        for prop, sub in props.items():
            mark = "" if prop in required else "?"
            bits.append(f"{prop}{mark}: {resolve(spec, sub, depth + 1)}")
        return "{" + ", ".join(bits) + "}"
    if kind == "string" and "enum" in schema:
        return "|".join(str(e) for e in schema["enum"])
    return str(kind or "any")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-download the spec")
    ap.add_argument("--grep", default="tag", help="substring to match in paths (default: tag)")
    ap.add_argument("--all", action="store_true", help="ignore --grep, list every path")
    args = ap.parse_args()

    spec = fetch(args.refresh)
    paths = spec.get("paths", {})
    print(f"\nspec version: {spec.get('info', {}).get('version')}   total paths: {len(paths)}")

    needle = "" if args.all else args.grep.lower()
    selected = [
        (path, ops)
        for path, ops in sorted(paths.items())
        if needle in path.lower() or needle in json.dumps(ops).lower()
    ]

    print(f"matching '{args.grep}': {len(selected)} path(s)\n")
    for path, ops in selected:
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            summary = op.get("summary", "")
            print(f"  {method.upper():6} {path}")
            if summary:
                print(f"         {summary}")

            params = op.get("parameters", [])
            interesting = [
                p for p in params
                if p.get("in") in ("path", "query") and p.get("name", "").lower() not in ("x-api-key",)
            ]
            if interesting:
                bits = []
                for p in interesting:
                    required = "*" if p.get("required") else ""
                    bits.append(f"{p.get('name')}{required}")
                print(f"         params: {', '.join(bits)}")

            body = op.get("requestBody", {}).get("content", {})
            for media, payload in body.items():
                schema = payload.get("schema", {})
                print(f"         body ({media}): {resolve(spec, schema)}")
            print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
