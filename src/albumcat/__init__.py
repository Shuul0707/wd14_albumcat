"""albumcat -- tag a photo library with WD14 anime tags, on the phone.

The pipeline is deliberately split into small, swappable stages:

    scan  ->  inference  ->  plugins  ->  rules  ->  tag fields

* ``tagger``     runs the WD14 ONNX model (BGR, 448x448, no normalisation).
* ``plugins``    lets you bolt extra recognisers on -- most importantly a
                 768-d ONNX classification head trained on your own
                 corrections, which is how you get *precise* character
                 recognition for a specific franchise.  (768 is the ViT-B/16
                 global-average-pooled feature width; the upstream README's
                 "1024" is wrong.)
* ``rules``      turns raw scores into tag decisions (which root, review band,
                 rating isolation).
* ``tagfields``  flattens a plan into the two tag lists the phone stores --
                 the flat table and the hierarchical paths.
* ``server``     exposes the pipeline over the LAN so the phone app can post
                 a 448px thumbnail and get back a finished tag set.

Quick start::

    python tools/download_model.py
    tools\\install_deps.cmd
    tools\\albumcat.cmd doctor
    tools\\albumcat.cmd plan samples\\

Names are resolved lazily (PEP 562), so ``from albumcat.config import Config``
works with nothing but the standard library installed -- importing this
package does not drag in onnxruntime, opencv or pandas.
"""

from __future__ import annotations

from importlib import import_module

__version__ = "0.1.0"

# public name -> submodule it lives in
_EXPORTS = {
    # config / io
    "Config": "config",
    # inference
    "Wd14Tagger": "tagger",
    "TagTable": "tagger",
    "TagResult": "tagger",
    "load_tagger": "pipeline",
    # domain
    "CHARACTER": "types",
    "GENERAL": "types",
    "RATING": "types",
    "Prediction": "types",
    "TagSet": "types",
    "TagPlan": "types",
    "PlannedTag": "types",
    "prettify": "types",
    "split_series": "types",
    # rules
    "Rule": "rules",
    "RuleEngine": "rules",
    "load_rules": "rules",
    "save_rules": "rules",
    # plugins
    "PluginRegistry": "plugins",
    "PluginManifest": "plugins",
    # pipeline
    "Pipeline": "pipeline",
    # tag fields
    "plan_to_tag_fields": "tagfields",
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value  # cache so later lookups skip __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
