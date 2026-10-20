#!/usr/bin/env python3
"""Export Chinese-CLIP as two ONNX towers for the albumcat semantic service.

Run this **once on a dev machine**; the runtime only needs onnxruntime. Two
towers are exported separately because they have very different homes:

    image_encoder.onnx   PC, once per photo (ViT-B/16, heavy)
    text_encoder.onnx    phone per query, or PC via /encode_text (light)

Usage
    python -m pip install torch transformers onnx onnxruntime
    python tools/export_chinese_clip_onnx.py                # -> models/chinese-clip/
    python tools/export_chinese_clip_onnx.py --quantize     # int8, ~1/4 the size
    python tools/export_chinese_clip_onnx.py --self-test    # forward pass check

Notes
    * Output is **not** L2-normalised; `semantic.py` normalises, and so must
      any on-device implementation. Comparing a normalised vector with an
      unnormalised one silently ranks by magnitude instead of similarity.
    * Batch is fixed at 1 on purpose: smaller graph, no dynamic-shape ops, and
      the service processes photos one at a time anyway.
    * Text input is padded to ``--max-text-length`` so the ONNX graph keeps a
      static shape, which is friendlier to mobile runtimes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "models" / "chinese-clip"


def export(model_id: str, out_dir: Path, opset: int, max_text_length: int) -> None:
    try:
        import torch
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor
    except ImportError:  # pragma: no cover
        sys.exit(
            "需要 torch + transformers（仅导出时）：\n"
            "  python -m pip install torch transformers onnx"
        )

    print(f"loading {model_id} …")
    model = ChineseCLIPModel.from_pretrained(model_id).eval()
    processor = ChineseCLIPProcessor.from_pretrained(model_id)

    class ImageTower(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, pixel_values):
            return self.inner.get_image_features(pixel_values=pixel_values)

    class TextTower(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.inner.get_text_features(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    size = 224

    image_path = out_dir / "image_encoder.onnx"
    print(f"exporting image tower -> {image_path}")
    torch.onnx.export(
        ImageTower(model),
        torch.zeros(1, 3, size, size),
        str(image_path),
        input_names=["pixel_values"],
        output_names=["image_features"],
        opset_version=opset,
        do_constant_folding=True,
        dynamic_axes=None,
    )

    text_path = out_dir / "text_encoder.onnx"
    print(f"exporting text tower  -> {text_path}")
    torch.onnx.export(
        TextTower(model),
        (
            torch.zeros(1, max_text_length, dtype=torch.long),
            torch.ones(1, max_text_length, dtype=torch.long),
            torch.zeros(1, max_text_length, dtype=torch.long),
        ),
        str(text_path),
        input_names=["input_ids", "attention_mask", "token_type_ids"],
        output_names=["text_features"],
        opset_version=opset,
        do_constant_folding=True,
        dynamic_axes=None,
    )

    vocab_path = out_dir / "vocab.txt"
    try:
        processor.tokenizer.save_vocabulary(str(out_dir))
        produced = out_dir / "vocab.txt"
        if not produced.exists():
            # some versions write `vocab.txt` under a subdirectory
            for candidate in out_dir.rglob("vocab.txt"):
                candidate.replace(vocab_path)
                break
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not save vocab ({exc}); copy it from the HF repo by hand")
    if not vocab_path.exists():
        print("warning: vocab.txt missing — the text tower will not load")

    for path in (image_path, text_path):
        print(f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB")


def quantize(out_dir: Path) -> None:
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
    except ImportError:  # pragma: no cover
        sys.exit("需要 onnxruntime：python -m pip install onnxruntime")

    for name in ("image_encoder.onnx", "text_encoder.onnx"):
        src = out_dir / name
        if not src.exists():
            print(f"skip {name}: not found")
            continue
        dst = src.with_name(src.stem + ".int8.onnx")
        print(f"quantising {name} -> {dst.name}")
        quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8)
        print(f"  {dst.stat().st_size / 1e6:.1f} MB")


def self_test(out_dir: Path, max_text_length: int) -> int:
    """One real forward pass per tower: catches shape/name/opset mistakes."""
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:  # pragma: no cover
        sys.exit("需要 numpy + onnxruntime")

    failures = 0
    for name, feeds in (
        (
            "image_encoder.onnx",
            {"pixel_values": np.random.rand(1, 3, 224, 224).astype(np.float32)},
        ),
        (
            "text_encoder.onnx",
            {
                "input_ids": np.ones((1, max_text_length), dtype=np.int64),
                "attention_mask": np.ones((1, max_text_length), dtype=np.int64),
                "token_type_ids": np.zeros((1, max_text_length), dtype=np.int64),
            },
        ),
    ):
        path = out_dir / name
        if not path.exists():
            print(f"  [--] {name}: missing")
            failures += 1
            continue
        try:
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            available = {i.name: i for i in session.get_inputs()}
            feeds = {k: v for k, v in feeds.items() if k in available}
            outputs = session.run(None, feeds)
            vector = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(vector))
            print(f"  [ok] {name}: dim={vector.shape[0]}, |v|={norm:.3f}")
            if vector.shape[0] != 512:
                print(f"       expected 512 dims, got {vector.shape[0]}")
                failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [!!] {name}: {type(exc).__name__}: {exc}")
            failures += 1

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Chinese-CLIP to ONNX")
    parser.add_argument("--model", default="OFA-Sys/chinese-clip-vit-base-patch16")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--max-text-length", type=int, default=52)
    parser.add_argument("--quantize", action="store_true", help="int8 dynamic quantisation")
    parser.add_argument("--self-test", action="store_true", help="only run the forward-pass check")
    parser.add_argument("--skip-export", action="store_true", help="reuse existing .onnx files")
    args = parser.parse_args()

    if args.self_test:
        return self_test(args.out, args.max_text_length)

    if not args.skip_export:
        export(args.model, args.out, args.opset, args.max_text_length)
    if args.quantize:
        quantize(args.out)

    print("\nrun a forward pass to verify:")
    return self_test(args.out, args.max_text_length)


if __name__ == "__main__":
    raise SystemExit(main())
