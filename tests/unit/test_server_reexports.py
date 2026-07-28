"""Guard the ``core/`` → ``server`` re-export surface.

The large server.py refactor extracted ~12 focused ``core/`` modules but kept
every private helper re-imported back into the ``backend.web.server``
namespace, because the whole test suite (and the routes/tick loops) reference
them through ``server`` — ``monkeypatch.setattr(server, "_foo", …)`` is the
canonical seam. A future extraction that forgets to re-import one of those
names would silently break every test that patches ``server._foo`` with a
confusing AttributeError far from the cause.

This test reads server.py's own ``from backend.web.core.X import (...)``
statements (via AST, so it auto-tracks new extractions) and asserts each
imported name is actually bound on the ``server`` module — and, where the
source object is resolvable, that ``server``'s binding *is* that same object.
"""

from __future__ import annotations

import ast
import importlib

import pytest

from backend.web import server


def _iter_module_level(nodes):
    """Walk statements without descending into function/class bodies, so only
    imports bound at module import time are seen (a ``from core import _x``
    nested inside a route handler doesn't bind ``server._x``)."""
    for node in nodes:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        children = (
            getattr(node, "body", [])
            + getattr(node, "orelse", [])
            + getattr(node, "finalbody", [])
        )
        for handler in getattr(node, "handlers", []):
            children += getattr(handler, "body", [])
        if children:
            yield from _iter_module_level(children)


def _core_reexports():
    """Yield ``(source_module, bound_name, imported_name)`` for every
    module-level ``from backend.web.core[.X] import ...`` in server.py."""
    with open(server.__file__) as f:
        tree = ast.parse(f.read(), filename=server.__file__)
    out = []
    for node in _iter_module_level(tree.body):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = node.module or ""
        if not (mod == "backend.web.core" or mod.startswith("backend.web.core.")):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            out.append((mod, bound, alias.name))
    return out


_REEXPORTS = _core_reexports()


def test_reexport_surface_is_nonempty():
    # Sanity: the refactor pulled a substantial surface out into core/.
    assert len(_REEXPORTS) > 30


@pytest.mark.parametrize(
    "mod,bound,name",
    _REEXPORTS,
    ids=[f"{m.split('.')[-1]}.{b}" for m, b, _ in _REEXPORTS],
)
def test_core_name_is_bound_on_server(mod, bound, name):
    # (1) The monkeypatch seam: the name must exist on the server facade.
    assert hasattr(server, bound), (
        f"server is missing re-exported name '{bound}' from {mod}; a "
        f"monkeypatch.setattr(server, '{bound}', ...) seam would break"
    )
    # (2) Where the source object resolves, server must hold the SAME object,
    # not a shadowing redefinition.
    try:
        src = importlib.import_module(mod)
        src_obj = getattr(src, name)
    except (ImportError, AttributeError):
        return  # submodule alias or dynamically-provided name: (1) is enough
    assert getattr(server, bound) is src_obj
