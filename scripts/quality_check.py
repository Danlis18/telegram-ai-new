from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
MINIAPP = ROOT / "miniapp"


def check_python() -> list[str]:
    errors: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
    return errors


def check_internal_imports() -> list[str]:
    """Catch broken app.* imports before Railway sees them."""
    errors: list[str] = []
    module_names = {p.stem for p in APP.glob("*.py")}
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "app":
                    for alias in node.names:
                        if alias.name not in module_names:
                            errors.append(
                                f"{path.relative_to(ROOT)} imports missing app.{alias.name}"
                            )
                elif node.module and node.module.startswith("app."):
                    name = node.module.split(".", 1)[1].split(".", 1)[0]
                    if name not in module_names:
                        errors.append(
                            f"{path.relative_to(ROOT)} imports missing app.{name}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app."):
                        name = alias.name.split(".", 1)[1].split(".", 1)[0]
                        if name not in module_names:
                            errors.append(
                                f"{path.relative_to(ROOT)} imports missing app.{name}"
                            )
    return sorted(set(errors))


def check_frontend_assets() -> list[str]:
    errors: list[str] = []
    index = (MINIAPP / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="\.\/([^"?]+)', index)
    for ref in refs:
        target = MINIAPP / ref
        if not target.exists():
            errors.append(f"miniapp/index.html references missing asset: {ref}")

    expected = {"index.html", "app.css", "app.js"}
    actual = {p.name for p in MINIAPP.iterdir() if p.is_file()}
    stale = sorted(actual - expected)
    if stale:
        errors.append("Unexpected Mini App production fragments: " + ", ".join(stale))

    app_js = (MINIAPP / "app.js").read_text(encoding="utf-8")
    dynamic_fragments = re.findall(
        r"['\"]\.\/[^'\"]+\.(?:css|js)(?:\?[^'\"]*)?['\"]",
        app_js,
    )
    if dynamic_fragments:
        errors.append(
            "Bundled Mini App must not dynamically load deleted JS/CSS fragments: "
            + ", ".join(sorted(set(dynamic_fragments)))
        )
    return errors


def check_runtime_contract() -> list[str]:
    errors: list[str] = []
    entry = (APP / "safe_entrypoint.py").read_text(encoding="utf-8")
    if "run_production" not in entry:
        errors.append("safe_entrypoint.py must delegate to runtime_bootstrap.run_production")

    runtime = (APP / "runtime_bootstrap.py").read_text(encoding="utf-8")
    required = [
        "prepare_persistence",
        "install_user_ai_runtime",
        "install_premium_emoji_support",
        "install_user_publisher",
        "start_miniapp_server",
        "start_match_schedule_worker",
        "start_web_news_worker",
    ]
    for name in required:
        if name not in runtime:
            errors.append(f"runtime_bootstrap.py missing required production layer: {name}")

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    if 'CMD ["python", "-m", "app.safe_entrypoint"]' not in dockerfile:
        errors.append("Dockerfile must start app.safe_entrypoint")
    return errors


def check_repository_hygiene() -> list[str]:
    errors: list[str] = []
    forbidden_suffixes = {".session", ".sqlite", ".sqlite3", ".db"}
    forbidden_names = {".env"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if ".git" in rel.parts:
            continue
        if path.name in forbidden_names or path.suffix.lower() in forbidden_suffixes:
            errors.append(f"Sensitive/runtime artifact must not be committed: {rel}")

    if (APP / "sources.py").exists():
        errors.append("app/sources.py is obsolete; source catalog belongs in source_whitelist.py")

    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line_no, raw in enumerate(env_example.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.endswith(("TOKEN", "KEY", "HASH", "PASSWORD")) and value.strip():
            if value.strip().lower() not in {"true", "false"}:
                errors.append(f".env.example:{line_no} must not contain a real secret for {key}")
    return errors


def main() -> int:
    groups = {
        "Python syntax": check_python(),
        "Internal imports": check_internal_imports(),
        "Mini App assets": check_frontend_assets(),
        "Runtime contract": check_runtime_contract(),
        "Repository hygiene": check_repository_hygiene(),
    }
    errors = [item for values in groups.values() for item in values]
    if errors:
        print("QUALITY CHECK FAILED")
        for error in errors:
            print(f" - {error}")
        return 1

    print("QUALITY CHECK OK")
    for name in groups:
        print(f" - {name}: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
