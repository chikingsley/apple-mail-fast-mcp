"""Enforce the repository's small, evidence-backed test policy."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parents[1] / "tests"
REGRESSION_MARKER = re.compile(r"#\d+|\b(?:bug|regression)\b", re.IGNORECASE)


def _is_test(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
        "test_"
    )


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Attribute) and decorator.attr == "fixture"
        for decorator in node.decorator_list
    )


def main() -> int:
    failures: list[str] = []
    counts = {"regressions": 0, "live": 0}

    for path in sorted(TEST_ROOT.rglob("test_*.py")):
        category = path.relative_to(TEST_ROOT).parts[0]
        if category not in counts:
            failures.append(f"{path}: tests must live in tests/regressions or tests/live")
            continue

        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not _is_test(node) or _is_fixture(node):
                continue
            counts[category] += 1
            if category == "regressions":
                evidence = f"{node.name} {ast.get_docstring(node) or ''}"
                if not REGRESSION_MARKER.search(evidence):
                    failures.append(
                        f"{path}:{node.lineno}: {node.name} needs an issue number, "
                        "bug reference, or regression explanation"
                    )

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1

    print(f"test policy passed: {counts['regressions']} regressions, {counts['live']} live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
