"""Repository hygiene: the checks a reviewer would otherwise have to remember.

Nothing here needs the model, a GPU, a phone or the network, so it runs in
under a second and in CI:

  * ``LICENSE`` is the MIT text we claim, and ``pyproject.toml`` describes the
    package and its console script
  * every ``*.cmd`` is pure ASCII -- cmd.exe reads those files with the OEM
    codepage (cp936 on a Chinese Windows), so one Chinese character inside a
    batch file becomes a parse error at *some later line*, which is a
    spectacularly confusing way to lose an afternoon
  * no secret-looking strings in the sources
  * relative links in ``README.md`` and ``docs/*.md`` resolve
  * ``.gitignore`` keeps artefacts (``dist/``, ``.tmp/``, ``models/``) out

Usage:
    python tools\\check_repo.py [--verbose]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Directories never worth walking (artefacts, caches, the vendored models).
SKIP_DIRS = {
    ".git", ".venv", ".tmp", "dist", "models", "build", "node_modules",
    "third_party", "docs/evidence", "aves/build", "aves/.dart_tool",
}

#: Binary-ish files: never secret-scanned, never link-checked.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".onnx", ".apk",
    ".zip", ".7z", ".gz", ".ttf", ".otf", ".woff", ".woff2", ".so", ".a",
    ".aar", ".jar", ".class", ".db", ".mp4", ".webm", ".pdf", ".bin", ".onnx_data",
}

#: Upstream ships its *public* Firebase config; we carry that file verbatim, so
#: its "API key" is not ours and cannot be rotated by us. GitHub's secret
#: scanning flags it as a false positive.
SECRET_ALLOWLIST = {"aves/android/app/google-services.json"}

SECRET_PATTERNS = [
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----")),
]

LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+?)\s*(?:\"[^\"]*\")?\s*\)")


class Report:
    def __init__(self, verbose: bool, limit: int = 6) -> None:
        self.verbose = verbose
        self.limit = 10_000 if verbose else limit
        self.failures = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"  [ OK ] {name}{('  ' + detail) if detail else ''}")

    def fail(self, name: str, problems: list[str]) -> None:
        self.failures += 1
        print(f"  [FAIL] {name}")
        for problem in problems[: self.limit]:
            print(f"         - {problem}")
        if len(problems) > self.limit:
            print(f"         … 还有 {len(problems) - self.limit} 条（--verbose 看全部）")


def walk(root: Path = ROOT):
    """Yield every interesting text file, skipping artefacts and binaries."""
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel == skip or rel.startswith(skip + "/") for skip in SKIP_DIRS):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path, rel


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------- #
# checks
# ---------------------------------------------------------------------- #
def check_license(rep: Report) -> None:
    license_path = ROOT / "LICENSE"
    if not license_path.exists():
        rep.fail("LICENSE 存在且是 MIT", ["没有 LICENSE 文件"])
        return
    text = read_text(license_path)
    if not text.lstrip().startswith("MIT License"):
        rep.fail("LICENSE 存在且是 MIT", ["文件开头不是 'MIT License'"])
        return
    rep.ok("LICENSE 存在且是 MIT")


def check_packaging(rep: Report) -> None:
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        rep.fail("pyproject.toml 可安装", ["没有 pyproject.toml"])
        return
    text = read_text(pyproject)
    problems = []
    for needle, why in [
        ('name = "albumcat"', "缺少 name"),
        ("[project.scripts]", "缺少 [project.scripts]（没有 console script）"),
        ("albumcat = \"albumcat.cli:main\"", "入口点不是 albumcat.cli:main"),
        ("[tool.setuptools.packages.find]", "没有 src 布局的包发现配置"),
        ("requires-python", "没有声明 requires-python"),
    ]:
        if needle not in text:
            problems.append(why)
    src = ROOT / "src" / "albumcat" / "cli.py"
    if not src.exists():
        problems.append("src/albumcat/cli.py 不在")
    tests = list((ROOT / "tests").glob("test_*.py")) if (ROOT / "tests").is_dir() else []
    if not tests:
        problems.append("tests/ 里没有 test_*.py")
    if problems:
        rep.fail("pyproject.toml 可安装", problems)
    else:
        rep.ok("pyproject.toml 可安装", f"{len(tests)} 个测试文件")


def check_no_bom(rep: Report) -> None:
    """UTF-8 with BOM breaks tools that read bytes, not text.

    ``tomllib.loads`` on a BOM-prefixed ``pyproject.toml`` raises
    ``TOMLDecodeError: Invalid statement (at line 1, column 1)``, which reads
    like a syntax error in a file that looks perfectly fine. Windows
    PowerShell 5.1's ``-Encoding utf8`` writes that BOM for you, so this is an
    easy accident to make and a cheap one to catch.
    """
    suffixes = {".py", ".toml", ".md", ".yml", ".yaml", ".cmd", ".ps1", ".json", ".txt"}
    problems = []
    checked = 0
    for path, rel in walk():
        if rel.startswith("aves/") or path.suffix.lower() not in suffixes:
            continue
        checked += 1
        if path.read_bytes().startswith(b"\xef\xbb\xbf"):
            problems.append(f"{rel} 开头有 UTF-8 BOM")
    if problems:
        rep.fail("文本文件没有 UTF-8 BOM", problems)
    else:
        rep.ok("文本文件没有 UTF-8 BOM", f"{checked} 个文件")


def check_cmd_ascii(rep: Report) -> None:
    cmds = [p for p, _rel in walk() if p.suffix.lower() == ".cmd"]
    problems = []
    for path in cmds:
        raw = path.read_bytes()
        bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
        if bad:
            offset, byte = bad[0]
            line = raw[:offset].count(b"\n") + 1
            problems.append(
                f"{path.relative_to(ROOT).as_posix()}:{line} 有非 ASCII 字节 0x{byte:02X}"
            )
    if problems:
        rep.fail("*.cmd 是纯 ASCII（cmd.exe 按 OEM 代码页读）", problems)
    else:
        rep.ok("*.cmd 是纯 ASCII（cmd.exe 按 OEM 代码页读）", f"{len(cmds)} 个文件")


def check_secrets(rep: Report) -> None:
    problems = []
    scanned = 0
    for path, rel in walk():
        if rel in SECRET_ALLOWLIST:
            continue
        text = read_text(path)
        scanned += 1
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{rel}:{line} 像 {label}")
    if problems:
        rep.fail("源码里没有疑似密钥", problems)
    else:
        rep.ok("源码里没有疑似密钥", f"扫了 {scanned} 个文件")


def check_markdown_links(rep: Report) -> None:
    docs = [ROOT / "README.md", ROOT / "CONTRIBUTING.md"] + sorted((ROOT / "docs").glob("*.md"))
    problems = []
    checked = 0
    for doc in docs:
        if not doc.exists():
            continue
        text = read_text(doc)
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if not target:
                continue
            checked += 1
            candidates = [
                (doc.parent / target).resolve(),
                (ROOT / target).resolve(),
            ]
            if not any(c.exists() for c in candidates):
                line = text[: text.find(raw_target)].count("\n") + 1
                problems.append(f"{doc.relative_to(ROOT).as_posix()}:{line} 链接指向不存在的 {target}")
    if problems:
        rep.fail("README/docs 的相对链接都能打开", problems)
    else:
        rep.ok("README/docs 的相对链接都能打开", f"{checked} 个链接")


def check_gitignore(rep: Report) -> None:
    path = ROOT / ".gitignore"
    if not path.exists():
        rep.fail(".gitignore 挡住产物", ["没有 .gitignore"])
        return
    lines = {line.strip() for line in read_text(path).splitlines()}
    problems = []
    for pattern, why in [
        ("dist/", "构建出来的 APK（dist/）会进仓库"),
        (".tmp/", "scratch 目录（.tmp/）会进仓库"),
        ("models/", "356 MB 的模型（models/）会进仓库"),
        (".venv/", "虚拟环境（.venv/）会进仓库"),
        ("samples/", "测试用的照片（samples/）会进仓库"),
    ]:
        if pattern not in lines:
            problems.append(f"{why}：.gitignore 里没有 {pattern}")
    if problems:
        rep.fail(".gitignore 挡住产物", problems)
    else:
        rep.ok(".gitignore 挡住产物")


# ---------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="列出每一条问题")
    args = parser.parse_args(argv)

    rep = Report(verbose=args.verbose)
    print("仓库卫生检查：")
    for check in (
        check_license,
        check_packaging,
        check_no_bom,
        check_cmd_ascii,
        check_secrets,
        check_markdown_links,
        check_gitignore,
    ):
        check(rep)

    print()
    if rep.failures:
        print(f"  {rep.failures} 项没通过。")
        return 1
    print("  全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
