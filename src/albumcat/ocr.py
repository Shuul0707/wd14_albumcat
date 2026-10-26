"""Read the text printed in a photo.

Why RapidOCR and not Umi-OCR
    Umi-OCR is a Windows desktop application: a Qt UI, its own HTTP server, its
    own model manager. What we need is the *engine* underneath it, and we need
    it inside this process, on the same ``onnxruntime`` we already ship. That
    engine is RapidOCR (PaddleOCR's detection + recognition models converted to
    ONNX), published as ``rapidocr_onnxruntime``. Depending on the desktop
    bundle would mean shipping a second Python environment and a windowed app
    that cannot run on a headless server.

Why it is off by default
    Measured on this machine (CPU): **2.5-5.5 s per photo**, against 0.1-0.6 s
    for WD14 tagging and 0.1-3 s for the CLIP image tower. OCR is the most
    expensive thing this service can do, so it is opt-in per request
    (``include_ocr``) rather than part of every tagging run. A client that wants
    it pays for it explicitly.

What the phone stores
    One newline-joined string per photo, in reading order. No boxes, no
    per-line scores: the only question the phone asks is "which of my photos
    contain this word", and that is a substring match. Keeping the string flat
    also keeps the column small (a few hundred bytes for a poster).

    Lines are joined with ``\\n``; a blank line between two paragraphs of text
    is preserved so that a search for a phrase spanning one line break still
    has a chance to be approximated by the phone's own matching.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import OcrConfig


@dataclass(frozen=True)
class OcrInfo:
    """What ``GET /ocr`` reports. Mirrors ``SemanticInfo`` on purpose."""

    enabled: bool
    available: bool
    reason: str = ""
    engine: str = ""
    max_side: int = 0
    min_score: float = 0.0

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


class OcrEngine:
    """Lazily loaded RapidOCR wrapper.

    Construction never raises and never loads models: the service must start
    even when OCR is unavailable, exactly like the semantic towers. The first
    ``read()`` pays the model-loading cost (~1.2 s here) and every later call
    reuses it.
    """

    def __init__(self, config: OcrConfig):
        self.config = config
        self._engine = None
        self._reason = ""
        self._load_error: str | None = None
        if not config.enabled:
            self._reason = "disabled in config"

    # -- lifecycle --------------------------------------------------------- #

    def _ensure(self) -> bool:
        """Load the engine once. Returns True when it is usable."""
        if self._engine is not None:
            return True
        if not self.config.enabled:
            return False
        if self._load_error is not None:
            return False
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
            return True
        except Exception as exc:  # noqa: BLE001 - absence must not break startup
            self._load_error = f"{type(exc).__name__}: {exc}"
            return False

    @property
    def available(self) -> bool:
        """Whether OCR can actually run right now (loads the engine once)."""
        return self._ensure()

    def info(self) -> OcrInfo:
        available = self.available
        reason = self._reason or (self._load_error or "")
        if not available and not reason:
            reason = "rapidocr_onnxruntime not installed"
        return OcrInfo(
            enabled=self.config.enabled,
            available=available,
            reason=reason,
            engine="rapidocr" if available else "",
            max_side=self.config.max_side,
            min_score=self.config.min_score,
        )

    # -- inference --------------------------------------------------------- #

    def read(self, image) -> dict | None:
        """Recognise text in a PIL image.

        Returns ``{"text", "lines", "engine", "ms"}`` or ``None`` when OCR is
        unavailable, the photo contains no text, or inference fails. ``None``
        is the normal answer for most album photos: a character illustration
        usually has no text at all.

        Never raises: a photo without OCR text is still a perfectly good photo.
        """
        if not self._ensure():
            return None

        started = time.perf_counter()
        try:
            import numpy as np
            from PIL import Image

            work = image.convert("RGB")
            # Big posters are the reason this is slow: the detector walks the
            # whole image. Capping the long side keeps the text readable for
            # recognition while cutting detector cost roughly quadratically.
            max_side = self.config.max_side
            if max_side and max(work.size) > max_side:
                work = work.copy()
                work.thumbnail((max_side, max_side), Image.LANCZOS)

            # RapidOCR expects BGR when handed an ndarray (it is cv2-shaped).
            array = np.asarray(work)[:, :, ::-1]
            result, _elapse = self._engine(array)
        except Exception as exc:  # noqa: BLE001 - degrade, never abort the request
            self._reason = f"inference failed: {type(exc).__name__}: {exc}"
            return None

        lines = self._collect(result)
        if not lines:
            return None
        return {
            "text": "\n".join(lines),
            "lines": len(lines),
            "engine": "rapidocr",
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _collect(self, result) -> list[str]:
        """Pull plain strings out of RapidOCR's raw tuples, in reading order.

        Each item is ``[box, text, score]`` where the score is a *string* in
        this release. Low-confidence fragments are dropped: OCR on stylised
        anime lettering happily hallucinates a character or two, and a wrong
        word in the index means the phone shows a photo the user never asked
        for -- worse than showing none.
        """
        if not result:
            return []
        minimum = self.config.min_score
        lines: list[str] = []
        for item in result:
            try:
                text = item[1]
                score = float(item[2])
            except (IndexError, TypeError, ValueError):
                continue
            if not text or not text.strip():
                continue
            if score < minimum:
                continue
            lines.append(text.strip())
        return lines
