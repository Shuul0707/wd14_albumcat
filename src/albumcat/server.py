"""The tagging service the phone talks to (PC side).

The work is split in two:

    phone  : walk its own gallery, build a 448px thumbnail per photo
    PC     : ID the thumbnail, apply the rules, hand back the *finished* tag set
    phone  : store that tag set in the gallery app's own tag table

The photo file itself is never rewritten -- the tags live in the gallery app's
database, so the pixels and the file's metadata are untouched by design.

The PC deliberately returns a finished answer -- flat tags, hierarchical
paths, star rating -- rather than raw WD14 scores. That way the rules, the
thresholds, the plugin registry and the denylist all live in one place and the
phone stays a dumb applier. Changing a threshold here changes behaviour on the
phone with no app rebuild.

Venue: LAN only, no cloud. Nothing but a downscaled thumbnail ever leaves the
phone, and the original photo never leaves the device at all.

Endpoints
    GET  /health    liveness + what model/plugins are loaded
    GET  /config    the roots and thresholds the phone should agree with
    GET  /semantic  is Chinese-CLIP loaded, and at what width/dtype
    POST /plan      one image  -> one finished tag set
    POST /plan_batch  [{...}, ...] -> [{...}, ...]
    POST /encode_text  {"text": "沙滩"} -> one query vector
    POST /query     {"text": "黑暗的初音未来"} -> tags named + vector of the rest

Semantic search splits across the two machines on purpose: the PC runs the
image tower once per photo (it is the expensive half) and hands the vector
back with the tags; the phone keeps that vector beside ``appTags`` and runs
the *text* tower per query -- or calls ``/encode_text`` when it has no model
of its own. See ``semantic.py`` and ``docs/SEMANTIC.md``.

``/query`` is the other half: the Chinese tag table lives with the model, so
the PC splits the phrase into WD14 tags (a named character is the strongest
signal a query can carry) and returns the leftover words separately. The phone
then ranks its own photos with that split -- the vectors are on the phone, so
only the parse has to travel. Without a text tower ``/query`` still answers
200 with ``"vector": null``; only the ranking degrades.

Request body for /plan, either form:

    JSON   {"id": "...", "name": "...", "size": 123, "date_taken": 0,
            "image": "<base64 jpeg>"}
    RAW    body is the JPEG bytes; pass ?id=... or X-Albumcat-Id

Usage:
    tools\\albumcat.cmd serve                 # 0.0.0.0:8899
    tools\\albumcat.cmd serve --port 9000 --token secret
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Config
from .pipeline import Pipeline, load_tagger
from .semantic import (
    SemanticEncoder,
    SemanticUnavailable,
    apply_vector_policy,
    pack_vector,
)
from .tagdict import TagDictionary, load_dictionary
from .tagfields import plan_to_tag_fields

__all__ = ["TaggingService", "serve"]

log = logging.getLogger("albumcat.server")

PLAN_VERSION = 1
MAX_BODY = 32 * 1024 * 1024  # a 448px thumbnail is ~50 KB; 32 MB is pure paranoia


def _lan_ips() -> list[str]:
    """Best-effort list of this machine's LAN addresses, to print at startup."""
    ips: list[str] = []
    try:
        # No packet is actually sent; this just asks the routing table which
        # local address would be used to reach the outside world.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


class TaggingService:
    """Loads the model once and answers plan requests."""

    def __init__(self, config: Config, *, warmup: bool = True) -> None:
        self.config = config
        self.pipeline = Pipeline(config, tagger=load_tagger(config, warmup=warmup))
        # Chinese-CLIP is optional: a missing model leaves `available` False and
        # the tagging endpoints keep working exactly as before.
        self.semantic = SemanticEncoder(config.semantic)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_order: list[str] = []
        self.cache_hits = 0
        self.started = time.time()
        self._tagdict: TagDictionary | None = None

    # -- introspection ----------------------------------------------------- #

    @property
    def tagdict(self) -> TagDictionary:
        """The Chinese name table, loaded on first use (~1 MB of JSON)."""
        if self._tagdict is None:
            self._tagdict = load_dictionary()
        return self._tagdict

    @property
    def tagger(self):
        return self.pipeline.tagger

    def health(self) -> dict:
        plugins: list[dict] = []
        plugin_errors: list[str] = []
        reg = self.pipeline.registry
        if reg is not None:
            plugins = [
                {"id": m.id, "kind": m.kind, "target": m.target, "priority": m.priority}
                for m in reg.active()
            ]
            plugin_errors = [f"{pid}: {why}" for pid, why in reg.errors]
        return {
            "status": "ok",
            "service": "albumcat",
            "plan_version": PLAN_VERSION,
            "model": self.tagger.describe() if self.tagger else None,
            "plugins": plugins,
            "plugin_errors": plugin_errors,
            "semantic": self.semantic.info().to_dict(),
            "uptime_s": round(time.time() - self.started, 1),
            "cache": {"entries": len(self._cache), "hits": self.cache_hits},
        }

    def config_view(self) -> dict:
        p = self.config.publish
        return {
            "plan_version": PLAN_VERSION,
            "character_root": p.character_root,
            "series_root": p.series_root,
            "copyright_root": p.copyright_root,
            "rating_root": p.rating_root,
            "rating_enabled": p.rating_enabled,
            "write_star_rating": p.write_star_rating,
            "review_low": p.review_low,
            "review_high": p.review_high,
            "publish_review_band": p.publish_review_band,
            "max_characters_per_image": p.max_characters_per_image,
            "max_general_tags_per_image": p.max_general_tags_per_image,
            "threshold": self.config.model.threshold,
            "character_threshold": self.config.model.character_threshold,
            "semantic": {
                "enabled": self.config.semantic.enabled,
                "available": self.semantic.available,
                "model": self.config.semantic.model_id,
                "dim": self.config.semantic.dim,
                "dtype": self.config.semantic.dtype,
            },
        }

    # -- work -------------------------------------------------------------- #

    def _remember(self, key: str, payload: dict) -> None:
        self._cache[key] = payload
        self._cache_order.append(key)
        limit = 4096
        while len(self._cache_order) > limit:
            self._cache.pop(self._cache_order.pop(0), None)

    def plan_bytes(
        self,
        data: bytes,
        *,
        asset_id: str = "",
        name: str = "",
        size: int = 0,
        date_taken: int = 0,
        use_cache: bool = True,
        include_vector: bool = False,
        vector_dtype: str | None = None,
    ) -> dict:
        """Tag one already-encoded image and return the finished tag set.

        With ``include_vector`` the response also carries the Chinese-CLIP
        embedding for this photo, so a tagging run doubles as an indexing run.
        ``vector_dtype`` overrides the configured wire format per request --
        a client that can memory-map float32 (``Float32List.view``) asks for
        that instead of decoding half-floats by hand.
        """
        dtype = vector_dtype or self.config.semantic.dtype
        if dtype not in ("float16", "float32"):
            raise ValueError(f"unsupported vector_dtype: {dtype}")
        from PIL import Image  # local import: keeps module import cheap

        digest = hashlib.sha256(data).hexdigest()
        started = time.perf_counter()
        if use_cache and digest in self._cache:
            self.cache_hits += 1
            cached = dict(self._cache[digest])
            cached["id"] = asset_id or cached.get("id", "")
            cached["cached"] = True
            # report the cache lookup, not the inference that filled it
            cached["ms"] = round((time.perf_counter() - started) * 1000, 2)
            return self._with_vector_policy(cached, include_vector, dtype)

        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load()
                image = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        except Exception as exc:  # noqa: BLE001 - any decode failure is a bad request
            raise ValueError(f"cannot decode image: {type(exc).__name__}: {exc}") from exc

        # onnxruntime sessions are thread-safe, but serialising here keeps the
        # GPU from being oversubscribed when several phones sync at once.
        with self._lock:
            plan, tagset = self.pipeline.plan_image(image, asset_id=asset_id, path=name)
            semantic_block = self._semantic_block(image, dtype)

        flat, hierarchical = plan_to_tag_fields(plan, self.config.publish)
        star = plan.star_rating if self.config.publish.write_star_rating else None

        payload = {
            "ok": True,
            "plan_version": PLAN_VERSION,
            "id": asset_id,
            "name": name,
            "size": size,
            "date_taken": date_taken,
            # exactly the tag list the phone stores
            "flat": flat,
            "hierarchical": hierarchical,
            "rating": star or 0,
            # why, so the phone (or you) can show a review list
            "needs_review": plan.needs_review,
            "star_rating": plan.star_rating,
            "notes": list(plan.notes),
            "characters": [
                {
                    "key": p.key,
                    "label": p.display,
                    "series": p.series,
                    "score": round(p.score, 4),
                    "source": p.source,
                }
                for p in tagset.characters
            ],
            "tags": [
                {"key": p.key, "label": p.display, "score": round(p.score, 4), "source": p.source}
                for p in tagset.general
            ],
            "ratings": {k: round(v, 4) for k, v in tagset.ratings.items()},
            # the phone stores this next to the tags; see docs/SEMANTIC.md
            "semantic": semantic_block,
            "engine": {
                "model": Path(self.config.model.dir).name,
                "device": self.config.model.device,
                "threshold": self.config.model.threshold,
                "character_threshold": self.config.model.character_threshold,
            },
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "cached": False,
        }
        if use_cache:
            self._remember(digest, payload)
        return self._with_vector_policy(payload, include_vector, dtype)

    # -- semantic ---------------------------------------------------------- #

    def _semantic_block(self, image, dtype: str | None = None) -> dict | None:
        """Encode one photo, or ``None`` when the towers are not loaded.

        Called inside ``self._lock`` so image encoding and tagging never fight
        over the GPU. A failure here must not fail the tag request -- a photo
        without a vector is still a perfectly good tagged photo.
        """
        if not self.semantic.available:
            return None
        try:
            vector = self.semantic.encode_image(image)
        except Exception as exc:  # noqa: BLE001 - degrade, never abort
            log.warning("semantic encode failed: %s", exc)
            return None
        dtype = dtype or self.config.semantic.dtype
        return {
            "model": self.config.semantic.model_id,
            "dim": int(vector.shape[0]),
            "dtype": dtype,
            "vector": pack_vector(vector, dtype),
        }

    @staticmethod
    def _with_vector_policy(payload: dict, include_vector: bool, dtype: str | None = None) -> dict:
        """Kept as a thin delegate so the logic is testable without the tagger."""
        return apply_vector_policy(payload, include_vector, dtype)

    def semantic_info(self) -> dict:
        return {"ok": True, **self.semantic.info().to_dict()}

    def encode_text(self, text: str, dtype: str | None = None) -> dict:
        """Encode one query phrase. Raises SemanticUnavailable when text is off."""
        started = time.perf_counter()
        vector = self.semantic.encode_text(text)
        dtype = dtype or self.config.semantic.dtype
        if dtype not in ("float16", "float32"):
            raise ValueError(f"unsupported vector_dtype: {dtype}")
        return {
            "ok": True,
            "model": self.config.semantic.model_id,
            "dim": int(vector.shape[0]),
            "dtype": dtype,
            "vector": pack_vector(vector, dtype),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def parse_query(self, text: str, *, with_vector: bool = False,
                    dtype: str | None = None) -> dict:
        """Split a search phrase into tag mentions, plus its CLIP vector.

        This is the hybrid-search endpoint: the phone sends what the user typed
        ("黑暗的初音未来") and gets back both halves -- the character tag the
        phrase names, and a vector for the words that are left over ("黑暗").

        A missing Chinese-CLIP model is *not* an error here. The tag half still
        works, and answering 503 would break search exactly when the user would
        have been happy with tag-only results.
        """
        started = time.perf_counter()
        parsed = self.tagdict.parse(text)
        payload: dict[str, Any] = {"ok": True, **parsed.to_dict()}
        payload["counts"] = {
            "mentions": len(parsed.mentions),
            "characters": len(parsed.character_keys),
            "series": sum(1 for m in parsed.mentions if m.is_series),
        }
        payload["residual"] = parsed.residual
        if with_vector:
            payload["vector"] = self._query_vector(parsed.vector_text, dtype)
        payload["ms"] = round((time.perf_counter() - started) * 1000, 1)
        return payload

    def _query_vector(self, text: str, dtype: str | None = None) -> dict | None:
        """Vector for the leftover phrase, or None if that is not possible."""
        if not text or not self.semantic.available:
            return None
        dtype = dtype or self.config.semantic.dtype
        if dtype not in ("float16", "float32"):
            raise ValueError(f"unsupported vector_dtype: {dtype}")
        try:
            with self._lock:
                vector = self.semantic.encode_text(text)
        except Exception as exc:  # noqa: BLE001 - tags alone still rank
            log.warning("query encode failed: %s", exc)
            return None
        return {
            "model": self.config.semantic.model_id,
            "dim": int(vector.shape[0]),
            "dtype": dtype,
            "vector": pack_vector(vector, dtype),
            "text": text,
        }


# --------------------------------------------------------------------------- #
# HTTP


class _Handler(BaseHTTPRequestHandler):
    server_version = "albumcat"
    protocol_version = "HTTP/1.1"

    # injected by make_handler
    service: TaggingService
    token: str = ""

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, *, asset_id: str = "") -> None:
        self._send(code, {"ok": False, "id": asset_id, "error": message})

    def _authed(self) -> bool:
        if not self.token:
            return True
        given = self.headers.get("X-Albumcat-Token", "")
        return given == self.token

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ValueError(f"body too large ({length} bytes)")
        return self.rfile.read(length)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    # -- routes ------------------------------------------------------------ #

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/health"):
            self._send(200, self.service.health())
            return
        if path == "/config":
            self._send(200, self.service.config_view())
            return
        if path == "/semantic":
            self._send(200, self.service.semantic_info())
            return
        if path == "/semantic/model":
            self._send_model()
            return
        self._error(404, f"no such endpoint: {path}")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path not in ("/plan", "/plan_batch", "/encode_text", "/query"):
            self._error(404, f"no such endpoint: {path}")
            return
        if not self._authed():
            self._error(401, "bad or missing X-Albumcat-Token")
            return
        try:
            raw = self._read_body()
        except ValueError as exc:
            self._error(413, str(exc))
            return
        if not raw:
            self._error(400, "empty body")
            return

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if path == "/encode_text":
                self._send(200, self._handle_encode_text(raw))
            elif path == "/query":
                self._send(200, self._handle_query(raw))
            elif path == "/plan":
                self._send(200, self._handle_one(raw, ctype))
            else:
                self._send(200, self._handle_batch(raw, ctype))
        except SemanticUnavailable as exc:
            self._error(503, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 - never kill the connection silently
            log.exception("plan failed")
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- payload handling -------------------------------------------------- #

    @staticmethod
    def _decode_b64(value: str) -> bytes:
        if value.startswith("data:"):
            _, _, value = value.partition(",")
        try:
            return base64.b64decode(value, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"bad base64 image: {exc}") from exc

    def _one_from_json(self, obj: dict) -> dict:
        image = obj.get("image") or obj.get("data")
        if not isinstance(image, str) or not image:
            raise ValueError("missing 'image' (base64)")
        return self.service.plan_bytes(
            self._decode_b64(image),
            asset_id=str(obj.get("id") or ""),
            name=str(obj.get("name") or ""),
            size=int(obj.get("size") or 0),
            date_taken=int(obj.get("date_taken") or 0),
            use_cache=bool(obj.get("cache", True)),
            include_vector=bool(obj.get("include_vector", False)),
            vector_dtype=(str(obj["vector_dtype"]) if obj.get("vector_dtype") else None),
        )

    def _send_model(self) -> None:
        """Stream one model file to the phone, which keeps its own copy.

        Only the two text-tower files are served: the phone never runs the
        image tower, and handing out 300 MB over the LAN should not be a
        one-liner either.
        """
        if not self._authed():
            self._error(401, "bad or missing X-Albumcat-Token")
            return
        allowed = {"text_encoder.onnx", "vocab.txt"}
        name = (self._query().get("file") or ["text_encoder.onnx"])[0]
        if name not in allowed:
            self._error(400, f"only {sorted(allowed)} can be served")
            return
        path = self.service.config.semantic.path / name
        if not path.exists():
            self._error(404, f"not found: {name} (run tools/export_chinese_clip_onnx.py)")
            return

        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Albumcat-File", name)
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle_encode_text(self, raw: bytes) -> dict:
        """POST /encode_text  {"text": "沙滩"}  -> one unit vector."""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bad JSON: {exc}") from exc
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("query") or ""
        elif isinstance(obj, str):
            text = obj
        else:
            raise ValueError("expected {'text': '...'} or a bare JSON string")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("missing 'text'")
        dtype = obj.get("dtype") if isinstance(obj, dict) else None
        return self.service.encode_text(text, dtype=(str(dtype) if dtype else None))

    def _handle_query(self, raw: bytes) -> dict:
        """POST /query  {"text": "黑暗的初音未来"}  -> tags + optional vector."""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bad JSON: {exc}") from exc
        if isinstance(obj, str):
            obj = {"text": obj}
        if not isinstance(obj, dict):
            raise ValueError("expected {'text': '...'} or a bare JSON string")
        text = obj.get("text") or obj.get("q") or obj.get("query") or ""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("missing 'text'")
        dtype = obj.get("dtype")
        return self.service.parse_query(
            text,
            with_vector=bool(obj.get("vector", True)),
            dtype=(str(dtype) if dtype else None),
        )

    def _handle_one(self, raw: bytes, ctype: str) -> dict:
        q = self._query()
        if ctype == "application/json" or raw[:1] in (b"{", b"["):
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"bad JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError("expected a JSON object")
            return self._one_from_json(obj)
        # raw image bytes
        asset_id = (
            self.headers.get("X-Albumcat-Id")
            or (q.get("id") or [""])[0]
        )
        return self.service.plan_bytes(
            raw,
            asset_id=asset_id,
            name=(q.get("name") or [""])[0],
            size=len(raw),
        )

    def _handle_batch(self, raw: bytes, ctype: str) -> dict:
        try:
            objs = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"bad JSON: {exc}") from exc
        if isinstance(objs, dict):
            objs = objs.get("images")
        if not isinstance(objs, list):
            raise ValueError("expected a JSON array of requests")
        if len(objs) > 256:
            raise ValueError(f"too many images ({len(objs)} > 256)")

        started = time.perf_counter()
        results = []
        for obj in objs:
            if not isinstance(obj, dict):
                results.append({"ok": False, "error": "expected an object"})
                continue
            try:
                results.append(self._one_from_json(obj))
            except ValueError as exc:
                results.append({"ok": False, "id": str(obj.get("id") or ""), "error": str(exc)})
        return {
            "ok": True,
            "plan_version": PLAN_VERSION,
            "results": results,
            "total": len(results),
            "failed": sum(1 for r in results if not r.get("ok")),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }


def make_handler(service: TaggingService, token: str = ""):
    return type("Handler", (_Handler,), {"service": service, "token": token})


def _warn_if_firewalled(host: str, port: int, ips: list[str]) -> None:
    """Say something if Windows is likely to drop the phone's packets.

    Loopback answers regardless, so a firewall that blocks the LAN looks
    exactly like a working server from the PC.  Worth one netsh query.
    """
    if host in ("127.0.0.1", "localhost", "::1") or not ips:
        return
    try:
        from .firewall import preflight
    except Exception as exc:  # noqa: BLE001
        log.debug("firewall check unavailable: %s", exc)
        return
    verdict, message = preflight(port)
    if verdict is False:
        log.warning("the phone may not be able to reach this server:\n  %s", message)
    elif verdict is None:
        log.debug("firewall check skipped: %s", message)


def serve(
    config: Config,
    *,
    host: str = "0.0.0.0",
    port: int = 8899,
    token: str = "",
    warmup: bool = True,
) -> None:
    service = TaggingService(config, warmup=warmup)
    httpd = ThreadingHTTPServer((host, port), make_handler(service, token))
    httpd.daemon_threads = True

    ips = _lan_ips()
    log.info("albumcat serve listening on %s:%d", host, port)
    for ip in ips:
        log.info("  phone should use  http://%s:%d", ip, port)
    if not token:
        log.warning("no token set - anyone on this LAN can use the model (--token to lock it)")
    _warn_if_firewalled(host, port, ips)
    log.info("model: %s", (service.tagger.describe() if service.tagger else "none"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        httpd.server_close()
