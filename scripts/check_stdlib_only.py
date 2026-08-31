#!/usr/bin/env python3
"""Assert the shipped package imports nothing but the standard library.

Zero runtime dependencies is a contract: papercut/ installs with the plugin
and runs on a bare Python 3.10+ install, so `pip install` is never a
prerequisite. A third-party import fails loudly the first time someone runs
the tool on a machine without it, not at review time, so this walks the AST
rather than trusting the diff.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "papercut"
SIBLINGS = {p.stem for p in PKG.glob("*.py")} | {PKG.name}


def top_level_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return found


def main() -> int:
    stdlib = set(sys.stdlib_module_names)
    bad: list[str] = []
    for path in sorted(PKG.glob("*.py")):
        names = top_level_imports(ast.parse(path.read_text(encoding="utf-8")))
        for name in sorted(names - stdlib - SIBLINGS):
            bad.append(f"{path.relative_to(ROOT)}: {name}")
    if bad:
        print("non-stdlib imports in the shipped package:")
        for line in bad:
            print("  " + line)
        return 1
    print("stdlib-only: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
