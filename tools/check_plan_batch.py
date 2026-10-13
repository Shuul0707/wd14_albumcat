"""Lock the /plan_batch contract that the Aves fork parses.

Why this exists
---------------
The Android side (the Aves fork's `lib/services/ai_tagging_service.dart`) is
compiled by a separate toolchain and never sees the server's source.  If the
server renames `flat`, drops `needs_review`, or starts emitting a `;` inside a
tag, the phone breaks silently — the tags simply never show up.

So this probe runs the *real* server over the *real* probe images and asserts
the exact keys and types the Dart client reads:

  envelope : ok / plan_version / results / total / failed / ms
  result   : id / name / flat / hierarchical / rating / needs_review /
             characters / tags / engine / ms
  invariants: ids are echoed verbatim (they are the MediaStore row ids, which is
             how the phone maps a result back to a photo), every `flat` entry is
             free of `;` (Aves' tag separator), and a character label always
             resolves to a tag in `flat`.

Usage
-----
    tools\\albumcat.cmd serve            # in one terminal
    python tools\\check_plan_batch.py    # in another (defaults to 127.0.0.1:8899)
    python tools\\check_plan_batch.py http://10.0.0.5:8899

It needs the probe images, so run `python tools\\make_aves_probe.py` once first.
This is not part of check_all.cmd: it requires a live server and a GPU tagger.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / ".tmp" / "aves-probe"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"

failures: list[str] = []
checks = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}{(' — ' + detail) if detail else ''}")


def post(path: str, payload: object, token: str = "") -> tuple[int, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    if token:
        request.add_header("X-Albumcat-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def main() -> int:
    images = sorted(PROBE.glob("*.jpg"))
    print(f"server   : {BASE}")
    print(f"probe dir: {PROBE} ({len(images)} jpeg)")
    if not images:
        print("  no probe images — run tools/make_aves_probe.py first")
        return 2

    payload = []
    for index, path in enumerate(images[:3]):
        payload.append(
            {
                # the phone sends the MediaStore row id as a string
                "id": str(1000000019 + index),
                "name": path.name,
                "size": path.stat().st_size,
                "image": base64.b64encode(path.read_bytes()).decode("ascii"),
                "cache": True,
            }
        )

    started = time.perf_counter()
    status, body = post("/plan_batch", payload)
    elapsed = time.perf_counter() - started

    print(f"\nPOST /plan_batch -> HTTP {status} in {elapsed:.1f}s")
    check(status == 200, "the batch endpoint answers 200", str(status))
    if not isinstance(body, dict):
        print(f"  body is not a JSON object: {type(body)}")
        return 1

    for key in ("ok", "plan_version", "results", "total", "failed", "ms"):
        check(key in body, f"envelope carries `{key}`")
    check(body.get("ok") is True, "envelope ok is true")
    check(isinstance(body.get("results"), list), "results is a list")

    results = body.get("results") or []
    check(len(results) == len(payload), "one result per request", f"{len(results)} != {len(payload)}")

    sent_ids = {item["id"] for item in payload}
    got_ids: set[str] = set()
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            check(False, f"result[{index}] is an object")
            continue
        check(result.get("ok") is True, f"result[{index}] ok", str(result.get("error")))
        for key in ("id", "name", "flat", "hierarchical", "rating", "needs_review", "characters", "tags", "engine", "ms"):
            check(key in result, f"result[{index}] carries `{key}`")
        got_ids.add(str(result.get("id")))

        flat = result.get("flat")
        check(isinstance(flat, list) and all(isinstance(v, str) for v in flat), f"result[{index}] flat is list[str]")
        for value in flat or []:
            check(";" not in value, f"result[{index}] tag has no `;` (Aves separator)", repr(value))

        characters = result.get("characters")
        check(isinstance(characters, list), f"result[{index}] characters is a list")
        for character in characters or []:
            check(isinstance(character, dict), f"result[{index}] character is an object")
            for key in ("key", "label", "score", "source"):
                check(key in character, f"result[{index}] character carries `{key}`")

        review = result.get("needs_review")
        check(isinstance(review, bool), f"result[{index}] needs_review is a bool")

    check(got_ids == sent_ids, "the ids come back exactly as sent", f"{sorted(got_ids)} != {sorted(sent_ids)}")

    # what the Dart side would store, given `aiTaggingIncludeGeneral = false`
    print("\nwhat the phone would store (characters only):")
    for result in results:
        flat = result.get("flat") or []
        labels = [c.get("label") for c in (result.get("characters") or [])]
        picked = []
        for label in labels:
            for tag in flat:
                if tag == label or tag.endswith(f"|{label}"):
                    picked.append(tag)
                    break
        print(f"  {result.get('id')}  {result.get('name')}: {picked}")
        check(bool(picked), f"{result.get('name')} yields at least one character tag")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
