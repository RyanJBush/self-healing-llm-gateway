"""Clean-environment check: build a fresh venv, install the declared test dependencies
from pyproject.toml (non-editable), and run the suite against the *installed* package.

Run:  python scripts/clean_install_check.py [--python PATH_TO_PYTHON] [--json out.json]

The tests run from a temporary directory that holds only tests/, openapi.yaml and
pyproject.toml (no gateway/ source), so an import that works only because of the source
tree, or a dependency missing from the declared extras, fails here. Needs network access
to the package index.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd, cwd=None, timeout=900):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--json")
    args = ap.parse_args()

    result: dict = {"base_python": args.python}
    with tempfile.TemporaryDirectory(prefix="clean-") as tmp:
        tmp = Path(tmp)
        src = tmp / "src"          # package source copy (so the project tree stays untouched)
        shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(
            "review", "__pycache__", ".pytest_cache", "*.egg-info", "build", ".git"))
        venv = tmp / "venv"
        code, out = run([args.python, "-m", "venv", str(venv)])
        if code:
            result.update(stage="venv", ok=False, output=out[-800:])
            return finish(result, args)
        py = str(venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
        result["python_version"] = run([py, "--version"])[1].strip()

        code, out = run([py, "-m", "pip", "install", "--disable-pip-version-check", f"{src}[test]"])
        result["install_exit"] = code
        if code:
            result.update(stage="pip install", ok=False, output=out[-1500:])
            return finish(result, args)
        result["installed"] = sorted(
            l for l in run([py, "-m", "pip", "list", "--disable-pip-version-check",
                            "--format=freeze"])[1].splitlines()
            if re.match(r"(?i)(uvicorn|httpx|pytest|pyyaml|jsonschema|self-healing)", l))

        work = tmp / "work"
        work.mkdir()
        shutil.copytree(src / "tests", work / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("openapi.yaml", "pyproject.toml"):
            shutil.copy(src / f, work / f)
        code, where = run([py, "-c", "import gateway,sys;print(gateway.__file__)"], cwd=work)
        result["gateway_imported_from"] = where.strip()
        result["using_installed_package"] = "site-packages" in where

        code, out = run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=work)
        result["pytest_exit"] = code
        result["pytest_summary"] = next(
            (l for l in reversed(out.splitlines()) if re.search(r"\d+ (passed|failed|error)", l)), out[-300:])
        code, out = run([py, "-m", "gateway.acceptance"], cwd=work)
        result["acceptance_exit"] = code
        result["acceptance_assertions"] = next(
            (l for l in out.splitlines() if l.startswith("assertions:")), "")
        result["ok"] = (result["pytest_exit"] == 0 and result["acceptance_exit"] == 0
                        and result["using_installed_package"])
    return finish(result, args)


def finish(result: dict, args) -> int:
    print(json.dumps(result, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
