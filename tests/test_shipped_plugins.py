"""The classifiers that ship in ``plugins/`` must agree with their own manifests.

Every check here is one an installer would otherwise discover by hand: the id has
to match the folder, ``labels.json`` has to match the manifest, the sha256 has to
match the file, and the ONNX head has to have exactly the input width and output
width the manifest advertises.  A mismatch here means the phone silently gets no
label (or a label pointing at the wrong class), which is impossible to notice
from the app side.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"


def _manifests() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in sorted(PLUGINS.glob("*/plugin.json")):
        found[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return found


SHIPPED = _manifests()
HEADS = sorted(name for name, data in SHIPPED.items() if data.get("kind") == "feature")


def test_the_shipped_set_is_what_the_app_expects() -> None:
    # Pins packaging: a classifier that disappears from here silently stops
    # producing tags on every phone already running.
    assert set(SHIPPED) == {"example-flat-image", "kind"}


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_a_manifest_is_named_after_its_folder(name: str) -> None:
    data = SHIPPED[name]
    assert data["id"] == name
    assert data["kind"] in {"feature", "python", "redraw", "embedding"}
    assert data["target"] in {"tag", "character", "rating"}
    assert 0.0 < float(data["threshold"]) < 1.0
    assert data.get("name"), "a label-less plugin shows up as a bare id in the app"


@pytest.mark.parametrize("name", HEADS)
def test_the_head_has_the_shape_the_labels_promise(name: str) -> None:
    data = SHIPPED[name]
    folder = PLUGINS / name
    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    assert data["labels"] == labels, "labels.json and plugin.json drifted apart"

    model = folder / data["model"]
    assert model.is_file()
    assert hashlib.sha256(model.read_bytes()).hexdigest() == data["sha256"]

    ort = pytest.importorskip("onnxruntime")
    session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
    spec = session.get_inputs()[0]
    width = spec.shape[-1]
    assert width == data["input_dim"], f"{spec.name} is {spec.shape}, manifest says {data['input_dim']}"

    import numpy as np

    out = session.run(None, {spec.name: np.zeros((2, width), dtype=np.float32)})[0]
    assert out.shape == (2, len(labels))
    if data.get("activation") == "softmax":
        assert np.allclose(out.sum(axis=1), 1.0, atol=1e-5)
    else:
        assert np.all((out >= 0.0) & (out <= 1.0))


def test_the_kind_head_is_exclusive_and_names_chinese_labels() -> None:
    # The four classes are shown verbatim as tag tiles in Aves, so they have to
    # be Chinese and only one of them may be emitted per image.
    data = SHIPPED["kind"]
    assert data["exclusive"] is True
    assert data["activation"] == "softmax"
    assert data["labels"] == ["截图", "插画", "梗图", "照片"]
