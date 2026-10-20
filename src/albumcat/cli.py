"""``albumcat`` command line.

Run through the wrapper so the package is importable without installing it::

    tools\\albumcat.cmd doctor
    tools\\albumcat.cmd plugins
    tools\\albumcat.cmd model
    tools\\albumcat.cmd plan  samples\\              # tag local files
    tools\\albumcat.cmd train --data data --out plugins\\my-ip --id my-ip
    tools\\albumcat.cmd serve                        # the LAN service the phone talks to
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Config

__all__ = ["main"]


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def _rule(char: str = "-", width: int = 78) -> str:
    return char * width


def _print_plan(path_label: str, plan, tagset, top: int = 8) -> None:
    chars = tagset.characters[:top]
    flags = []
    if plan.needs_review:
        flags.append("REVIEW")
    if plan.star_rating is not None:
        flags.append(f"stars={plan.star_rating}")
    print(f"\n{path_label}")
    if chars:
        rendered = "  ".join(f"{c.key} {c.score:.2f}" for c in chars)
        print(f"  characters : {rendered}")
    else:
        print("  characters : (none above threshold)")
    if plan.tags:
        print("  -> tags    : " + ", ".join(t.key for t in plan.tags))
    else:
        print("  -> tags    : (none)")
    rating = tagset.best_rating()
    print(f"  rating     : {rating[0]} {rating[1]:.2f}")
    if flags:
        print(f"  flags      : {' '.join(flags)}")
    for note in plan.notes:
        print(f"  note       : {note}")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    print(_rule("="))
    print("albumcat doctor")
    print(_rule("="))

    print(f"\nconfig root : {cfg.root}")
    model_dir = cfg.model.path
    print(f"model dir   : {model_dir}")

    ok = True
    model_path = model_dir / "model.onnx"
    tags_path = model_dir / "selected_tags.csv"
    for label, path in (("model.onnx", model_path), ("selected_tags.csv", tags_path)):
        if path.exists():
            print(f"  [ok]   {label:22s} {path.stat().st_size / 1e6:10.1f} MB")
        else:
            print(f"  [MISS] {label:22s} -- run python tools/download_model.py")
            ok = False
    features = model_dir / "model_features.onnx"
    if features.exists():
        print(
            f"  [ok]   model_features.onnx        "
            f"{features.stat().st_size / 1e6:10.1f} MB  (embedding plugins enabled)"
        )
    else:
        print(
            "  [--]   model_features.onnx          -- "
            "run python tools/add_feature_output.py "
            "(embedding plugins will be skipped)"
        )

    print("\npython packages")
    for module in ("onnxruntime", "cv2", "numpy", "pandas", "PIL", "requests"):
        try:
            imported = __import__(module)
            print(f"  [ok]   {module:14s} {getattr(imported, '__version__', '?')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [MISS] {module:14s} {exc}")
            ok = False

    try:
        import onnxruntime as ort

        providers = ort.get_available_providers()
        print(f"  providers: {providers}")
        preferred = [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider") if p in providers]
        if preferred:
            print(f"  -> will run on {preferred[0]}")
        else:
            print("  -> CPU only (slow but correct)")
    except Exception:  # noqa: BLE001
        pass

    print("\nplugins")
    if cfg.plugins.enabled:
        from .plugins import PluginRegistry

        registry = PluginRegistry(cfg.plugins.path, device=cfg.model.device, disabled=cfg.plugins.disabled)
        found = registry.discover()
        registry.load()
        if not found:
            print(f"  none in {cfg.plugins.path}")
        for row in registry.describe():
            status = "ready" if row["ready"] else f"NOT READY ({row['reason']})"
            print(
                f"  {row['id']:24s} kind={row['kind']:8s} "
                f"target={row['target']:9s} {status}"
            )
        for plugin_id, reason in registry.errors:
            print(f"  ERROR {plugin_id}: {reason}")
    else:
        print("  disabled in config")

    print("\nsemantic (optional)")
    if not cfg.semantic.enabled:
        print("  disabled in config ([semantic] enabled = false)")
    else:
        directory = cfg.semantic.path
        missing: list[str] = []
        for label, name in (
            ("image_encoder", cfg.semantic.image_model),
            ("text_encoder ", cfg.semantic.text_model),
            ("vocab        ", cfg.semantic.vocab_file),
        ):
            path = directory / name
            if path.exists():
                print(f"  [ok]   {label} {path.stat().st_size / 1e6:9.1f} MB")
            else:
                missing.append(name)
                print(f"  [--]   {label} missing   {path}")
        if not missing:
            from .semantic import SemanticEncoder

            # Presence is not the same as "it loads": an ONNX exported for a
            # different opset or a truncated download fails here, and doctor is
            # the right place to find that out instead of the first query.
            info = SemanticEncoder(cfg.semantic).info()
            if info.available:
                print(f"  -> {info.model}, {info.dim}-d vectors as {info.dtype}")
            else:
                print(f"  [WARN] files are present but the towers did not load: {info.reason}")
        else:
            print("  -> off: free-form search needs those three files;")
            print(r"     build them once with: python tools\export_chinese_clip_onnx.py")
            print("     (tagging is unaffected -- GET /semantic reports available: false)")

    print("\nphone")
    from .firewall import preflight
    from .server import _lan_ips

    port = 8899
    ips = _lan_ips()
    if ips:
        print("  phone should use: " + "   ".join(f"http://{ip}:{port}" for ip in ips))
    else:
        print("  [WARN] no LAN address found - is this machine on Wi-Fi/Ethernet?")
    verdict, message = preflight(port)
    # A blocked port is not a broken install: loopback still works, so this
    # must not fail `doctor`.  It just needs to be impossible to miss.
    print(("  [OK] " if verdict else "  [WARN] " if verdict is False else "  [?] ") + message)

    print()
    print("doctor: " + ("all good" if ok else "problems found (see MISS/FAIL above)"))
    return 0 if ok else 1


def cmd_plugins(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    from .plugins import PluginRegistry

    registry = PluginRegistry(cfg.plugins.path, device=cfg.model.device, disabled=cfg.plugins.disabled)
    registry.discover()
    registry.load()
    rows = registry.describe()
    if not rows:
        print(f"no plugins found in {cfg.plugins.path}")
        print("a plugin is a directory containing plugin.json -- see the module docstring")
        print("of src/albumcat/plugins.py for the four kinds and their payloads.")
        return 0
    print(f"{'id':24s} {'kind':8s} {'target':10s} {'prio':>5s} {'thr':>5s}  ready")
    for row in rows:
        print(
            f"{row['id']:24s} {row['kind']:8s} {row['target']:10s} "
            f"{row['priority']:5d} {row['threshold']:5.2f}  {row['ready']}"
            + (f"  ({row['reason']})" if not row["ready"] and row["reason"] else "")
        )
    for plugin_id, reason in registry.errors:
        print(f"  ERROR {plugin_id}: {reason}")
    return 0


def cmd_model(args: argparse.Namespace) -> int:
    """Print what the loaded ONNX model actually exposes.

    Chiefly a way to read the real embedding width, which plugin manifests must
    match. Upstream's README claims 1024-d; that holds only for ViT-L/14, and
    the widely used ``wd-v1-4-vit-tagger-v2`` is a ViT-B/16 at 768.
    """
    cfg = Config.load(args.config)
    from .pipeline import Pipeline

    tagger = Pipeline(cfg).tagger
    info = tagger.describe()
    print(json.dumps(info, indent=2, ensure_ascii=False))

    dim = info.get("embedding_dim")
    if dim is None:
        print(
            "\nNo embedding output -- `kind: feature` plugins cannot run.\n"
            "Build one with: python tools\\add_feature_output.py",
            file=sys.stderr,
        )
        return 1
    print(f'\nEmbedding width is {dim}: set "input_dim": {dim} in plugin.json.')
    return 0


def cmd_train(args: argparse.Namespace) -> int:
    """Train a `feature` plugin head from corrected samples.

    The work lives in ``tools/train_feature_plugin.py`` so it can also be run
    standalone; this just forwards the flags. Training is pure numpy and needs
    the WD14 model only to embed the samples, which is why it goes through the
    same config as everything else.
    """
    import importlib.util

    script = PROJECT_ROOT / "tools" / "train_feature_plugin.py"
    if not script.exists():
        print(f"missing {script}", file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("_albumcat_train", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    argv = ["--out", args.out]
    if args.data:
        argv += ["--data", args.data]
    if args.jsonl:
        argv += ["--jsonl", args.jsonl]
    if args.id:
        argv += ["--id", args.id]
    if args.name:
        argv += ["--name", args.name]
    if args.config:
        argv += ["--config", args.config]
    argv += [
        "--target", args.target,
        "--priority", str(args.priority),
        "--threshold", str(args.threshold),
        "--epochs", str(args.epochs),
        "--lr", str(args.lr),
        "--l2", str(args.l2),
        "--val-frac", str(args.val_frac),
        "--seed", str(args.seed),
    ]
    if args.force:
        argv.append("--force")
    return module.main(argv)


def cmd_plan(args: argparse.Namespace) -> int:
    """Tag local files. No network, no publishing -- the smoke test."""
    cfg = Config.load(args.config)
    if args.apply:
        print("refusing: `plan` never writes anything; it only prints.", file=sys.stderr)
        return 2

    from .pipeline import Pipeline

    pipeline = Pipeline(cfg)
    for warning in pipeline.warn_if_plugins_starved():
        print(f"warning: {warning}", file=sys.stderr)

    targets = [Path(p) for p in (args.paths or ["samples"])]
    missing = [p for p in targets if not p.exists()]
    if missing:
        print(f"no such path: {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2

    collected: list[dict] = []
    count = 0
    for file, plan, tagset in pipeline.plan_paths(targets):
        count += 1
        if args.json:
            collected.append({"file": str(file), **plan.to_dict()})
        else:
            _print_plan(str(file), plan, tagset, top=args.top)

    if args.json:
        payload = json.dumps(collected, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
            print(f"wrote {len(collected)} plans -> {args.out}")
        else:
            print(payload)
    else:
        print(f"\n{count} image(s) tagged")
    return 0


# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    """Run the tagging service the phone talks to.

    The phone sends a 448px thumbnail and gets back a *finished* tag set --
    flat keywords, hierarchical paths, star rating -- so the rules, thresholds
    and plugins all stay here and the phone just writes what it is told.
    """
    cfg = Config.load(args.config)

    from .server import serve

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("this serves the model to your LAN. no photo ever leaves the phone --")
        print("only the 448px thumbnail you send is read, and it is not written to disk.")
    serve(
        cfg,
        host=args.host,
        port=args.port,
        token=args.token or "",
        warmup=not args.no_warmup,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="albumcat", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check model, packages, plugins and the inference backend")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("plugins", help="list discovered plugins")
    p.set_defaults(func=cmd_plugins)

    p = sub.add_parser("model", help="print the model's I/O and embedding width")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser(
        "train",
        help="train a 'feature' plugin head from corrected samples (train_feature_plugin.py)",
    )
    p.add_argument("--data", help="directory of per-label subdirectories ('_' = background)")
    p.add_argument("--jsonl", help="JSONL of {path, labels} instead of --data")
    p.add_argument("--out", required=True, help="plugin directory to create")
    p.add_argument("--id", default=None, help="plugin id (default: --out directory name)")
    p.add_argument("--name", default=None, help="human-readable plugin name")
    p.add_argument(
        "--target",
        choices=("character", "tag", "rating"),
        default="character",
        help="which bucket the head's labels land in (default: character)",
    )
    p.add_argument("--priority", type=int, default=10, help="default 10: runs before WD14's own guesses")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--epochs", type=int, default=600)
    p.add_argument("--lr", type=float, default=0.05)
    p.add_argument("--l2", type=float, default=1e-3, help="L2 strength; raise if it overfits")
    p.add_argument("--val-frac", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--force", action="store_true", help="overwrite a non-empty --out")
    p.set_defaults(func=cmd_train)

    p = sub.add_parser("plan", help="tag local files and print what would be tagged")
    p.add_argument("paths", nargs="*", help="files or directories (default: samples/)")
    p.add_argument("--json", action="store_true", help="emit plans as JSON")
    p.add_argument("--out", help="write JSON to this file instead of stdout")
    p.add_argument("--top", type=int, default=8, help="characters shown per image")
    p.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser(
        "serve",
        help="run the LAN tagging service the phone app talks to",
    )
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    p.add_argument("--port", type=int, default=8899)
    p.add_argument(
        "--token",
        default="",
        help="require this value in X-Albumcat-Token (default: no auth, LAN only)",
    )
    p.add_argument("--no-warmup", action="store_true", help="skip the warmup inference")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
