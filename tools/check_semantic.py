"""Lock the semantic-search HTTP contract that the Aves fork parses.

Why this exists
---------------
`/plan_batch` has a contract checker because the phone is compiled by another
toolchain and never sees the server's source.  The semantic endpoints have the
same problem with one extra twist: **they have to work in two states**.

  * models built   -- vectors come back, dim/dtype must match what the phone
                      stored, and the query vector must be unit length
                      (Aves ranks with a plain dot product, so an unnormalised
                      vector silently ranks by magnitude)
  * models missing -- the feature is off, but it must be *politely* off:
                      `available: false` with a reason, HTTP 503 with a JSON
                      `error` on /encode_text, and never a traceback, because
                      the Dart client shows that string to the user

It also pins the one place where a bad request could read files it should not:
`/semantic/model` must serve only `text_encoder.onnx` and `vocab.txt`.

And `/query` -- the endpoint the phone's hybrid ranking calls. It must split
"黑暗的初音未来" into the character mention `hatsune_miku` plus the leftover mood
"黑暗", expand a series name ("东方的角色") into the cast, and still answer 200
with a null vector when the text tower is missing (a missing embedding degrades
the ranking, it does not break the search).

Usage
-----
    tools\\albumcat.cmd serve             # one terminal
    python tools\\check_semantic.py       # another (defaults to 127.0.0.1:8899)
    python tools\\check_semantic.py http://10.0.0.5:8899 [token]

`tools\\check_all.py` starts its own server, so it needs neither a phone nor a
model on disk -- without the models it simply verifies the degraded path.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import urllib.error
import urllib.request

import numpy as np

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8899"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""

checks = 0
failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    line = f"  [{'PASS' if condition else 'FAIL'}] {label}"
    if detail:
        line += f"   {detail}"
    print(line)
    if not condition:
        failures.append(label)


def _request(path: str, *, payload: object | None = None) -> tuple[int, bytes, dict]:
    headers = {"X-Albumcat-Token": TOKEN} if TOKEN else {}
    data = None
    if payload is not None:
        if isinstance(payload, bytes):
            data = payload
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(BASE + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _error_of(raw: bytes) -> str:
    body = _json(raw)
    if isinstance(body, dict):
        return str(body.get("error") or body.get("message") or "")
    return ""


def main() -> int:
    print(f"semantic contract @ {BASE}")
    status, raw, _headers = _request("/semantic")
    info = _json(raw)
    if status != 200 or not isinstance(info, dict):
        sys.exit(f"GET /semantic 返回 {status}，不像 JSON：{raw[:200]!r}")

    print("\nGET /semantic")
    check(info.get("ok") is True, "ok is true")
    for key, kind in (
        ("enabled", bool),
        ("available", bool),
        ("model", str),
        ("dim", int),
        ("dtype", str),
        ("image_model", str),
        ("text_model", str),
        ("reason", str),
    ):
        check(isinstance(info.get(key), kind), f"{key} is {kind.__name__}",
              repr(info.get(key)))
    check(isinstance(info.get("dim"), int) and int(info["dim"]) > 0, "dim > 0", repr(info.get("dim")))
    check(info.get("dtype") in ("float16", "float32"), "dtype is float16/float32", repr(info.get("dtype")))
    check(
        bool(info.get("available")) or bool(info.get("reason")),
        "unavailable comes with a reason",
        repr(info.get("reason")),
    )

    available = bool(info.get("available"))
    dim = int(info.get("dim") or 0)
    dtype = info.get("dtype")
    print(f"\n  state: available={available} dim={dim} dtype={dtype} model={info.get('model')}")
    print(f"  reason: {info.get('reason') or '(none)'}")

    print("\nPOST /encode_text")
    status, raw, _headers = _request("/encode_text", payload={"text": "沙滩"})
    body = _json(raw)
    if available:
        check(status == 200, "200 with models built", f"got {status}")
        if isinstance(body, dict):
            check(body.get("ok") is True, "ok is true")
            check(body.get("dim") == dim, "dim matches /semantic", repr(body.get("dim")))
            check(body.get("dtype") == dtype, "dtype matches /semantic", repr(body.get("dtype")))
            encoded = body.get("vector")
            check(isinstance(encoded, str) and bool(encoded), "vector is a base64 string")
            if isinstance(encoded, str) and encoded:
                width = 2 if dtype == "float16" else 4
                try:
                    vector = np.frombuffer(base64.b64decode(encoded), dtype=np.float16 if width == 2 else np.float32)
                    check(vector.size == dim, f"vector has {dim} components", str(vector.size))
                    with np.errstate(invalid="ignore", divide="ignore"):
                        norm = float(np.linalg.norm(vector.astype(np.float32)))
                    # Aves ranks by dot product, so unit length is part of the contract.
                    check(abs(norm - 1.0) <= 0.01, "vector is unit length", f"|v|={norm:.4f}")
                except Exception as exc:  # noqa: BLE001
                    check(False, "vector decodes", f"{type(exc).__name__}: {exc}")
    else:
        check(status == 503, "503 when models are missing", f"got {status}")
        message = _error_of(raw)
        check(bool(message), "carries a readable error", repr(message))
        check("Traceback" not in raw.decode("utf-8", "replace"), "no traceback leaks to the phone")

    status, raw, _headers = _request("/encode_text", payload={"text": "   "})
    check(status == 400, "400 on a blank query", f"got {status}")
    check(bool(_error_of(raw)), "blank query explains itself", repr(_error_of(raw)))

    status, raw, _headers = _request("/encode_text", payload=b"{ not json")
    check(status == 400, "400 on broken JSON", f"got {status}")

    print("\nPOST /query")
    # The phone's hybrid ranking needs the query split into named tags plus the
    # words left over for CLIP. Both halves are checked here because the Dart
    # side is compiled by another toolchain and never sees this source.
    status, raw, _headers = _request("/query", payload={"text": "黑暗的初音未来", "vector": False})
    body = _json(raw)
    check(status == 200, "200 for a plain /query", f"got {status}")
    if isinstance(body, dict):
        mentions = body.get("mentions") or []
        by_key = {m.get("key"): m for m in mentions if isinstance(m, dict)}
        check("hatsune_miku" in by_key, "找到角色 hatsune_miku", repr(list(by_key)))
        if "hatsune_miku" in by_key:
            check(by_key["hatsune_miku"].get("category") == "character",
                  "角色 mention 的 category 是 character",
                  repr(by_key["hatsune_miku"].get("category")))
            check(bool(by_key["hatsune_miku"].get("series")),
                  "角色 mention 带系列名（手机端用于展示）",
                  repr(by_key["hatsune_miku"].get("series")))
        check("dark" in by_key, "找到通用标签 dark", repr(list(by_key)))
        check(body.get("vector_text") == "黑暗", "vector_text 是剩下的情绪词",
              repr(body.get("vector_text")))
        check(body.get("has_character") is True, "has_character is true")
        counts = body.get("counts")
        check(isinstance(counts, dict) and counts.get("characters") == 1,
              "counts 里有一个角色", repr(counts))
        check(body.get("vector") is None, "vector=false 时不下发向量", repr(body.get("vector"))[:40])
    else:
        check(False, "/query 返回 JSON", repr(raw[:120]))

    status, raw, _headers = _request("/query", payload={"text": "东方的角色"})
    body = _json(raw)
    check(status == 200, "200 for a series query", f"got {status}")
    if isinstance(body, dict):
        series = [m for m in (body.get("mentions") or [])
                  if isinstance(m, dict) and m.get("category") == "series"]
        check(bool(series), "把东方Project 认成系列", repr([m.get("key") for m in series]))
        keys = (series[0].get("keys") if series else None) or []
        check(len(keys) > 10, "系列展开成整组角色", str(len(keys)))
        check("hakurei_reimu" in keys, "展开的角色里有博丽灵梦")

    status, raw, _headers = _request(
        "/query", payload={"text": "黑暗的初音未来", "vector": True, "dtype": "float32"}
    )
    body = _json(raw)
    check(status == 200, "vector=true 时 /query 仍然 200（不 503）", f"got {status}")
    if isinstance(body, dict):
        block = body.get("vector")
        if available:
            # The phone asks for float32 and its Dart decoder drops anything
            # else, so a silent float16 here would end as "no vector at all".
            check(isinstance(block, dict) and block.get("dtype") == "float32",
                  "vector=true 时下发 float32 向量块",
                  repr(block.get("dtype")) if isinstance(block, dict) else repr(type(block).__name__))
            if isinstance(block, dict):
                try:
                    values = struct.unpack("<512f", base64.b64decode(block.get("vector") or ""))
                    check(max(abs(v) for v in values) <= 1.0001, "向量已经归一化",
                          f"max |v| = {max(abs(v) for v in values):.3f}")
                except Exception as exc:  # noqa: BLE001
                    check(False, "向量块能解成 512 个 float32", repr(exc))
        else:
            check(block is None, "没有文本塔时 vector 是 null", repr(block))

    status, raw, _headers = _request("/query", payload={"text": "  "})
    check(status == 400, "400 on a blank /query", f"got {status}")

    print("\nGET /semantic/model")
    status, raw, _headers = _request("/semantic/model?file=../config.toml")
    check(status == 400, "refuses a path outside the model dir", f"got {status}")
    status, raw, _headers = _request("/semantic/model?file=image_encoder.onnx")
    check(status == 400, "refuses the image tower (phone never runs it)", f"got {status}")

    if TOKEN:
        status, raw, _headers = _request("/semantic/model?file=vocab.txt")
        check(status in (200, 404), "serves vocab.txt or says it is missing", f"got {status}")
    else:
        status, raw, _headers = _request("/semantic/model?file=vocab.txt")
        check(status in (200, 404), "serves vocab.txt or says it is missing", f"got {status}")
        if status == 404:
            check("export_chinese_clip_onnx" in _error_of(raw), "404 points at the export script",
                  repr(_error_of(raw)))

    print()
    if failures:
        print(f"{checks - len(failures)}/{checks} checks passed；失败：{failures}")
        return 1
    state = "模型在位" if available else "没有模型（降级路径）"
    print(f"{checks}/{checks} checks passed（{state}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
