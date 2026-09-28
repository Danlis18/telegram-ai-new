from __future__ import annotations

import ast
import re
import sys
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


def check_frontend_assets() -> list[str]:
    errors: list[str] = []
    index = (MINIAPP / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="\.\/([^"?]+)', index)
    for ref in refs:
        target = MINIAPP / ref
        if not target.exists():
            errors.append(f"miniapp/index.html references missing asset: {ref}")

    # Production Mini App is intentionally bundled to one CSS + one JS file.
    expected = {"index.html", "app.css", "app.js"}
    actual = {p.name for p in MINIAPP.iterdir() if p.is_file()}
    stale = sorted(actual - expected)
    if stale:
        errors.append("Unexpected Mini App production fragments: " + ", ".join(stale))
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
    return errors


def main() -> int:
    errors = check_python() + check_frontend_assets() + check_runtime_contract()
    if errors:
        print("QUALITY CHECK FAILED")
        for error in errors:
            print(f" - {error}")
        return 1
    print("QUALITY CHECK OK")
    print(" - Python syntax: OK")
    print(" - Mini App assets: OK")
    print(" - Production runtime contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
