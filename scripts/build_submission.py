"""Package only the official root-level entrypoint and shared ML modules.

No source corpus, cached index, environment, database, tests or API application
is included. Import rewriting uses the Python AST, not text substitution.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
# Both versions are in the organizer's recovered allowed requirements.txt.
PLATFORM_REQUIREMENTS = "pydantic==2.14.2\npypdf==6.21.4\n"


class RootImports(ast.NodeTransformer):
    def __init__(self, modules: set[str], *, source_is_ml: bool):
        self.modules = modules
        self.source_is_ml = source_is_ml

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if node.level:
            if not self.source_is_ml or node.level != 1 or module not in self.modules:
                raise ValueError(f"Unsupported relative import in packaged source: {module}")
            node.module, node.level = "energytool_ml_" + module, 0
        elif module.startswith("app.ml."):
            suffix = module.removeprefix("app.ml.")
            if suffix not in self.modules:
                raise ValueError(f"Unknown ML module: {suffix}")
            node.module = "energytool_ml_" + suffix
        elif module == "app" or module.startswith("app."):
            raise ValueError("Submission must not depend on the web application")
        return node

    def visit_Import(self, node):
        if any(alias.name == "app" or alias.name.startswith("app.") for alias in node.names):
            raise ValueError("Package-style app imports need explicit rewriting before submission")
        return node


def flattened_source(path: Path, modules: set[str], *, source_is_ml: bool) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tree = RootImports(modules, source_is_ml=source_is_ml).visit(tree)
    ast.fix_missing_locations(tree)
    source = ast.unparse(tree) + "\n"
    compile(source, path.name, "exec")
    return source


def build_submission(output: Path, *, root: Path = ROOT) -> list[str]:
    sources = sorted((root / "app" / "ml").glob("*.py"))
    sources = [path for path in sources if path.name != "__init__.py"]
    modules = {path.stem for path in sources}
    if not {"protocol", "core", "adapter", "corpus"}.issubset(modules):
        raise ValueError("Shared consultation core is incomplete")
    files = {
        "solution.py": flattened_source(root / "solution.py", modules, source_is_ml=False),
        "requirements.txt": PLATFORM_REQUIREMENTS,
    }
    for path in sources:
        files["energytool_ml_" + path.name] = flattened_source(path, modules, source_is_ml=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".zip.tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, source in sorted(files.items()):
                info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, source.encode("utf-8"))
        if temporary.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("Submission exceeds the platform 128 MiB archive limit")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return sorted(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "submission.zip")
    args = parser.parse_args(argv)
    names = build_submission(args.output)
    print(f"Built {args.output.name}: {len(names)} root files; no corpus or credentials.")


if __name__ == "__main__":
    main()
