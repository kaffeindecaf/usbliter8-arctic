"""Every module that uses `X | Y` annotations must opt into PEP 563 (py3.9).

`tuple | None` in a signature is evaluated at def time on 3.9 and dies with
"unsupported operand type(s) for |". Only python 3.13 stands behind the
required `tests` job, so a module that no test imports can carry that
annotation for months: `hardware_guide.py:244` (`tuple | None`) and
`contribute.py:260` (`list[str] | None`) only surfaced when a new test file
imported hardware_guide, and CI went red on the `python 3.9` job.

`python -m compileall` does not catch it (annotations are not evaluated when
compiling) and neither does importing the module on 3.12+, so the check is
static: a module without `from __future__ import annotations` may not use `|`
inside an annotation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FUTURE = "from __future__ import annotations"


def _modules() -> list[Path]:
    return sorted(ROOT.glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))


def _annotations(node: ast.AST) -> list[ast.expr]:
    """Every annotation attached to a function or an annotated assignment."""
    found: list[ast.expr | None] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        found += [arg.annotation for arg in node.args.args + node.args.kwonlyargs]
        found += [node.args.vararg.annotation if node.args.vararg else None,
                  node.args.kwarg.annotation if node.args.kwarg else None,
                  node.returns]
    elif isinstance(node, ast.AnnAssign):
        found.append(node.annotation)
    return [ann for ann in found if ann is not None]


def _union_annotations(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    hits = []
    for node in ast.walk(tree):
        for ann in _annotations(node):
            if any(isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
                   for sub in ast.walk(ann)):
                hits.append((getattr(node, "lineno", 0), ast.unparse(ann)))
    return hits


def test_modules_are_discovered():
    modules = _modules()
    assert len(modules) > 25, "the scan lost its file list"
    assert ROOT / "hardware_guide.py" in modules


def test_union_annotations_find_a_known_hit(tmp_path):
    # the detector itself has to work, or every module passes silently
    probe = tmp_path / "probe.py"
    probe.write_text("def f(x: int | None = None) -> list[str] | None:\n    return None\n")
    assert len(_union_annotations(probe)) == 2


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_union_annotation_implies_future_annotations(path: Path):
    hits = _union_annotations(path)
    if not hits:
        return
    assert FUTURE in path.read_text(), (
        f"{path.name} uses a `|` annotation without `{FUTURE}`, "
        f"which breaks the python 3.9 CI job: {hits}")
