"""Minimal Immich REST client.

Endpoints here were read off the machine-readable OpenAPI spec
(spec version 3.3.0-rc.0, 193 paths) -- fetch/inspect it with
``python tools/probe_immich_api.py``. The ones this project depends on:

======================================  ==========================================
``GET  /server/ping``                   liveness -> ``{"res": "pong"}``
``GET  /server/version``                ``{major, minor, patch}``
``GET  /tags``                          list tags
``POST /tags``                          ``TagCreateDto{name, color?, parentId?}``
``PUT  /tags``                          ``TagUpsertDto{tags: [...]}``
``PUT  /tags/assets``                   ``TagBulkAssetsDto{assetIds, tagIds}``
``PUT  /tags/{id}/assets``              ``BulkIdsDto{ids}``
``DELETE /tags/{id}/assets``            ``BulkIdsDto{ids}``
``PUT  /assets``                        ``AssetBulkUpdateDto{ids, rating?, ...}``
``POST /search/metadata``               ``MetadataSearchDto{..., tagIds, page, size}``
``GET  /assets/{id}/original``          original bytes
``GET  /libraries`` / ``POST /libraries/{id}/scan``
======================================  ==========================================

``TagCreateDto.parentId`` is what makes hierarchical ``角色/初音未来`` tags
possible; ``PUT /tags/assets`` is what makes bulk tagging cheap (no file I/O,
no XMP sidecar, nothing touching the photos).
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import requests

__all__ = ["ImmichClient", "ImmichError"]

DEFAULT_TIMEOUT = 60.0
#: Immich caps most `size`/`limit` params well below this, but we only ever ask
#: for 1000 at a time and follow the returned page cursor.
PAGE_SIZE = 1000


class ImmichError(RuntimeError):
    """Raised when Immich answers with a non-2xx status."""

    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:400]}")


class ImmichClient:
    """Thin synchronous wrapper around the Immich REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key,
                "Accept": "application/json",
                "User-Agent": "albumcat/0.1",
            }
        )
        self.session.verify = verify_tls
        # Cache of existing tags keyed by (parent_id, lowercase name) -> tag dict.
        self._tag_cache: dict[tuple[str | None, str], dict] = {}

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(method, url, **kwargs)
        if not response.ok:
            raise ImmichError(response.status_code, method, path, response.text)
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.content

    # ------------------------------------------------------------------ #
    # server
    # ------------------------------------------------------------------ #
    def ping(self) -> str:
        data = self._request("GET", "/server/ping")
        return data.get("res", "") if isinstance(data, dict) else ""

    def version(self) -> dict:
        return self._request("GET", "/server/version") or {}

    def about(self) -> dict:
        return self._request("GET", "/server/about") or {}

    # ------------------------------------------------------------------ #
    # tags
    # ------------------------------------------------------------------ #
    def list_tags(self) -> list[dict]:
        return self._request("GET", "/tags") or []

    def refresh_tag_cache(self) -> dict[tuple[str | None, str], dict]:
        self._tag_cache = {
            (tag.get("parentId"), tag["name"].lower()): tag for tag in self.list_tags()
        }
        return self._tag_cache

    def create_tag(self, name: str, parent_id: str | None = None, color: str | None = None) -> dict:
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parentId"] = parent_id
        if color:
            payload["color"] = color
        tag = self._request("POST", "/tags", json=payload)
        self._tag_cache[(tag.get("parentId"), tag["name"].lower())] = tag
        return tag

    def ensure_tag(self, name: str, parent_id: str | None = None, color: str | None = None) -> dict:
        """Return the tag named ``name`` under ``parent_id``, creating it if needed."""
        key = (parent_id, name.lower())
        cached = self._tag_cache.get(key)
        if cached is not None:
            return cached
        # Cache may be cold (client constructed, never listed) -- fill it once.
        if not self._tag_cache:
            self.refresh_tag_cache()
            cached = self._tag_cache.get(key)
            if cached is not None:
                return cached
        return self.create_tag(name, parent_id=parent_id, color=color)

    def ensure_tag_path(self, parts: Sequence[str], color: str | None = None) -> dict:
        """Create/get a hierarchy like ``["角色", "初音未来"]`` and return the leaf tag."""
        if not parts:
            raise ValueError("tag path must have at least one component")
        parent_id: str | None = None
        leaf: dict | None = None
        for part in parts:
            leaf = self.ensure_tag(part, parent_id=parent_id, color=color if leaf is None else None)
            parent_id = leaf["id"]
        assert leaf is not None
        return leaf

    def tag_assets(self, tag_ids: Iterable[str], asset_ids: Iterable[str]) -> None:
        """``PUT /tags/assets`` -- apply many tags to many assets in one call."""
        tag_ids = list(tag_ids)
        asset_ids = list(asset_ids)
        if not tag_ids or not asset_ids:
            return
        self._request(
            "PUT", "/tags/assets", json={"tagIds": tag_ids, "assetIds": asset_ids}
        )

    def tag_asset_ids(self, tag_id: str, asset_ids: Iterable[str]) -> None:
        asset_ids = list(asset_ids)
        if not asset_ids:
            return
        self._request("PUT", f"/tags/{tag_id}/assets", json={"ids": asset_ids})

    def untag_assets(self, tag_id: str, asset_ids: Iterable[str]) -> None:
        asset_ids = list(asset_ids)
        if not asset_ids:
            return
        self._request("DELETE", f"/tags/{tag_id}/assets", json={"ids": asset_ids})

    # ------------------------------------------------------------------ #
    # assets
    # ------------------------------------------------------------------ #
    def update_assets(self, asset_ids: Iterable[str], **fields: Any) -> None:
        """``PUT /assets`` -- bulk update. Supports ``rating``, ``description``, ..."""
        asset_ids = list(asset_ids)
        if not asset_ids:
            return
        payload = {"ids": asset_ids, **fields}
        self._request("PUT", "/assets", json=payload)

    def set_rating(self, asset_ids: Iterable[str], rating: int) -> None:
        """Immich's star rating is an integer; WD14's ratings are 4 probabilities."""
        self.update_assets(asset_ids, rating=int(rating))

    def search_metadata(
        self,
        page: int = 1,
        size: int = PAGE_SIZE,
        tag_ids: Sequence[str] | None = None,
        **filters: Any,
    ) -> dict:
        payload: dict[str, Any] = {"page": page, "size": size, **filters}
        if tag_ids:
            payload["tagIds"] = list(tag_ids)
        return self._request("POST", "/search/metadata", json=payload) or {}

    def iter_assets(
        self,
        tag_ids: Sequence[str] | None = None,
        page_size: int = PAGE_SIZE,
        **filters: Any,
    ) -> Iterator[dict]:
        """Yield assets, following Immich's ``nextPage`` cursor."""
        page = 1
        while True:
            body = self.search_metadata(page=page, size=page_size, tag_ids=tag_ids, **filters)
            items = (body.get("assets") or {}).get("items") or []
            for item in items:
                yield item
            next_page = (body.get("assets") or {}).get("nextPage")
            if not next_page or not items:
                return
            page = int(next_page)

    def download_original(self, asset_id: str, dest: Path) -> Path:
        data = self._request("GET", f"/assets/{asset_id}/original")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data if isinstance(data, bytes) else _json.dumps(data).encode())
        return dest

    def download_thumbnail(
        self,
        asset_id: str,
        dest_dir: Path,
        size: str = "preview",
        fmt: str = "jpeg",
    ) -> Path:
        """Fetch a derived size (``thumbnail`` | ``preview`` | ``fullsize``).

        WD14 only ever sees 448x448, so ``preview`` (typically ~1440px) is the
        right trade: a few hundred KB instead of the multi-MB original.
        """
        dest = Path(dest_dir) / f"{asset_id}.{fmt}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        data = self._request(
            "GET",
            f"/assets/{asset_id}/thumbnail",
            params={"size": size, "format": fmt},
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data if isinstance(data, bytes) else bytes(data))
        return dest

    # ------------------------------------------------------------------ #
    # libraries / jobs
    # ------------------------------------------------------------------ #
    def list_libraries(self) -> list[dict]:
        return self._request("GET", "/libraries") or []

    def scan_library(self, library_id: str) -> None:
        self._request("POST", f"/libraries/{library_id}/scan")

    def run_job(self, name: str) -> None:
        self._request("POST", "/jobs", json={"name": name})
