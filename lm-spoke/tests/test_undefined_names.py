"""Static check: no cs module uses a name it never defines or imports.

Why this exists. pyflakes found latent NameErrors in all three Python
components here: the spoke pool retrofit read ``data`` instead of its ``d``
argument, the hub terminal relay used ``contextlib`` without importing it, the
T3 routes called tenant guards that were never defined, and the legacy spoke
web terminal and reclone-clear ack used a helper and two variables that did
not exist. Every one imported and compiled fine (a NameError is raised only
when the line runs) and no test reached it, so each would have failed for the
first time in front of a user.

pyflakes resolves names without running anything, which is exactly that gap.
Only undefined names count; style, unused imports and shadowing are ignored, so
this is not a lint gate. Modelled on lm's core/tests/test_routes_undefined_names.py.

Limitation: in a module using ``from x import *`` (webui-local's aggregate
routers) pyflakes cannot tell which names the star import supplies, so it
reports them as star usage instead of undefined, and they are not checked here.

pyflakes comes from lm-spoke/requirements-dev.txt, which CI and smoke.sh
install. Its import is not optional: a run without it fails here instead of
skipping the guard, since a skipped guard reads as a pass.
"""
import ast
import functools
import pathlib

import pytest
from pyflakes import checker
from pyflakes import messages as pyflakes_messages

# Repo root (resolve() also follows the root-level ``tests`` symlink).
_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Components whose Python is scanned; tests and virtualenvs are skipped.
_COMPONENTS = ("lm-spoke", "webui-local", "webui-spoke")
_SKIP_DIRS = {"tests", "venv", "__pycache__", "node_modules"}

#: pyflakes messages that mean "this line raises NameError when it runs".
_FATAL = (
    pyflakes_messages.UndefinedName,
    pyflakes_messages.UndefinedLocal,
    pyflakes_messages.UndefinedExport,
)

#: Known findings as ``(repo-relative path, name)``. Empty: fix a new finding
#: rather than adding it. Self-cleaning: an entry that no longer matches a live
#: finding fails test_baseline_has_no_stale_entries.
_BASELINE: set[tuple[str, str]] = set()


def _python_files() -> list[str]:
    """Repo-relative paths of every scanned module (file names repeat across components)."""
    found = []
    for component in _COMPONENTS:
        for path in (_ROOT / component).rglob("*.py"):
            rel = path.relative_to(_ROOT)
            if not any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                found.append(rel.as_posix())
    return sorted(found)


def _has_future_annotations(tree: ast.AST) -> bool:
    """True when the module has ``from __future__ import annotations``."""
    return any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
               and any(a.name == "annotations" for a in n.names)
               for n in ast.walk(tree))


def _annotation_spots(tree: ast.AST) -> set[tuple[int, int]]:
    """``(line, column)`` of every node inside a parameter, return or variable annotation.

    Under ``from __future__ import annotations`` annotations are never
    evaluated, so a hint whose name is not imported is harmless; pyflakes still
    reports it. pyflakes reports a name at its own node (a quoted hint at its
    string), so a report is matched to a hint by where it is, never by name:
    the same name anywhere else, ``__all__`` included, is still checked.
    """
    spots: set[tuple[int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            params = a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
            annotations = [p.annotation for p in params if p is not None] + [node.returns]
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        else:
            continue
        for ann in annotations:
            if ann is not None:
                spots.update((n.lineno, n.col_offset) for n in ast.walk(ann) if hasattr(n, "lineno"))
    return spots


@functools.cache
def _undefined_names(path: pathlib.Path) -> list[tuple[int, str, str]]:
    """``(line, name, message)`` for each name in ``path`` that would raise NameError."""
    # A syntax error is a different, louder failure; report it as is.
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, "", f"SyntaxError: {exc}")]
    # Keep the fatal messages, minus undefined names inside hints that are never
    # evaluated. UndefinedExport and UndefinedLocal are not UndefinedName
    # subclasses, so they are always kept.
    hints = _annotation_spots(tree) if _has_future_annotations(tree) else set()
    found = []
    for m in checker.Checker(tree, filename=str(path)).messages:
        if not isinstance(m, _FATAL):
            continue
        if isinstance(m, pyflakes_messages.UndefinedName) and (m.lineno, m.col) in hints:
            continue
        name = m.message_args[0] if m.message_args else ""
        found.append((m.lineno, name, m.message % m.message_args))
    return found


@pytest.mark.parametrize("rel", _python_files())
def test_no_undefined_names(rel):
    """Every name a module uses must be defined or imported in that module."""
    bad = [f"{rel}:{line}: {msg}" for line, name, msg in _undefined_names(_ROOT / rel)
           if (rel, name) not in _BASELINE]
    assert not bad, ("undefined name(s), each a NameError waiting for a user:\n"
                     + "\n".join(bad))


def test_baseline_has_no_stale_entries():
    """A fixed finding must also leave _BASELINE, so the baseline cannot hide a new bug."""
    live = {(rel, name) for rel in _python_files()
            for _line, name, _msg in _undefined_names(_ROOT / rel)}
    stale = sorted(_BASELINE - live)
    assert not stale, f"these baselined findings are fixed; delete them from _BASELINE: {stale}"


def test_the_check_actually_catches_the_regression(tmp_path):
    """Guard the guard: the shipped bug shapes are caught; a bare or quoted hint is not."""
    bug = tmp_path / "regression.py"
    bug.write_text("from __future__ import annotations\n\n"
                   'async def handler(d: Mapping, e: "Quoted") -> None:\n'
                   "    with contextlib.suppress(Exception):\n"
                   "        return data\n")
    assert {name for _line, name, _msg in _undefined_names(bug)} == {"contextlib", "data"}
    # A hint does not hide the same name elsewhere: exporting it from __all__
    # still fails (``from module import *`` raises AttributeError).
    export = tmp_path / "export.py"
    export.write_text('from __future__ import annotations\n\n__all__ = ["Missing"]\n\n'
                      "def f(x: Missing) -> None:\n    pass\n")
    assert [(line, name) for line, name, _msg in _undefined_names(export)] == [(3, "Missing")]
