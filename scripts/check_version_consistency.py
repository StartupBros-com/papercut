#!/usr/bin/env python3
"""Version-drift guard: four files claim the package version; one is real.

VERSION at the repo root is the single authority. plugin.json, pyproject.toml,
and papercut/__init__.py each carry a literal copy rather than reading VERSION
at build/import time, because the package's actual distribution path is the
Claude Code plugin marketplace (`/plugin install papercut@hov`, README
"Installation"): the whole repo directory travels as a unit and is invoked in
place (`python3 -m papercut ...`), not built into a wheel via `pip install`.
A `pip install .` build is a secondary, best-effort path, and setuptools'
dynamic version support (`[tool.setuptools.dynamic]`) only resolves cleanly
when the version source ships inside the built artifact -- VERSION sits
outside the `papercut` package directory, so a wheel built without extra
package-data wiring would import fine yet carry no version file to read,
turning a metadata question into a packaging one. A literal in each file
is legible without tracing an indirect read; this script is what makes a
stale literal loud instead of silent, because a literal nothing verifies is
exactly how pyproject.toml drifted to 0.1.6 and __init__.py drifted to 0.1.0
while VERSION and plugin.json moved on to 0.1.18.

Usage:
  check_version_consistency.py
Exit 1 when any file's version disagrees with VERSION, naming every offender.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYPROJECT_VERSION_RE = re.compile(
    r'^\s*version\s*=\s*"([^"]+)"\s*$', re.MULTILINE
)
INIT_VERSION_RE = re.compile(
    r'^\s*__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE
)


def read_version_file() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def read_plugin_json() -> str:
    data = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    return str(data["version"])


def read_pyproject() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Only the [project] table's version line matters here; the file has no
    # other top-level `version = "..."` assignment, so a plain regex avoids
    # depending on tomllib (stdlib only from 3.11, but pyproject.toml declares
    # requires-python = ">=3.10").
    match = PYPROJECT_VERSION_RE.search(text)
    if not match:
        raise ValueError("pyproject.toml: no `version = \"...\"` line found under [project]")
    return match.group(1)


def read_init() -> str:
    text = (ROOT / "papercut/__init__.py").read_text(encoding="utf-8")
    match = INIT_VERSION_RE.search(text)
    if not match:
        raise ValueError("papercut/__init__.py: no `__version__ = \"...\"` line found")
    return match.group(1)


def main() -> int:
    authority = read_version_file()
    sources = [
        ("VERSION", authority),
        (".claude-plugin/plugin.json", read_plugin_json()),
        ("pyproject.toml", read_pyproject()),
        ("papercut/__init__.py", read_init()),
    ]

    mismatches = [(name, value) for name, value in sources if value != authority]

    if not mismatches:
        print(f"version consistency: ok (VERSION={authority})")
        return 0

    print(f"version drift: VERSION={authority!r} is the authority, but:")
    for name, value in mismatches:
        print(f"  {name}: {value!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
