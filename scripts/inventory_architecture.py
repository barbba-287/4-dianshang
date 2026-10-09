"""Generate a lightweight architecture inventory for review.

This tool reads source files and Git metadata only. It does not import the app,
connect to a database, or modify application data.
"""
from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.splitlines()


def local_imports(path: Path, tracked: set[str]) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        for module in modules:
            if not module.startswith("app."):
                continue
            base = module.replace(".", "/")
            for candidate in (f"{base}.py", f"{base}/__init__.py"):
                if (ROOT / candidate).exists():
                    dependencies.add(candidate)
                    break
    return sorted(dependencies)


def classify_untracked(path: str) -> str:
    if path.startswith(("面试/", "问题解决资产库/")):
        return "personal_or_recovery"
    if path in {"CLAUDE.md", "app/schemas.recovered.py"}:
        return "personal_or_recovery"
    if path.startswith(("app/", "migrations/versions/0016", "tests/", "docs/")):
        return "candidate_or_technical"
    return "review_required"


def main() -> None:
    tracked = set(git_lines("ls-files"))
    routes = []
    main_path = ROOT / "app/main.py"
    for line_number, line in enumerate(main_path.read_text(encoding="utf-8").splitlines(), 1):
        if "@app." in line:
            routes.append({"line": line_number, "declaration": line.strip()})

    models = []
    db_path = ROOT / "app/db.py"
    for line_number, line in enumerate(db_path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("class ") and "(Base)" in line:
            models.append({"line": line_number, "class": line.strip()})

    app_imports = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        app_imports.append(
            {
                "file": relative,
                "tracked": relative in tracked,
                "local_imports": local_imports(path, tracked),
            }
        )

    untracked = git_lines("ls-files", "--others", "--exclude-standard")
    inventory = {
        "generated_by": "scripts/inventory_architecture.py",
        "routes": routes,
        "orm_models": models,
        "app_imports": app_imports,
        "untracked_classification": [
            {"file": path, "kind": classify_untracked(path)} for path in untracked
        ],
    }
    output = ROOT / "docs/architecture-inventory.json"
    output.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {output}: routes={len(routes)}, models={len(models)}, "
        f"app_files={len(app_imports)}, untracked={len(untracked)}"
    )


if __name__ == "__main__":
    main()
