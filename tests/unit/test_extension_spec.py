"""ExtensionSpec (Addon API v3): serialization + the validation matrix.

Locks the manifest half of the extension contract the SPA host builds against:
the spec serializes verbatim into ``/api/addons``, and
``validate_extension_spec`` is the one gate both registration paths share (a
built-in's problems raise at registration; a discovered extension's skip it).
"""

from __future__ import annotations

import json

from backend.web.addons.base import (
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
    validate_extension_spec,
)


def _spec(**overrides) -> ExtensionSpec:
    """A known-good spec — the dbclient shape in miniature."""
    base = dict(
        module="/extensions/demo/index.js",
        bar_label="Demo",
        buttons=[
            ExtensionButton(command="demo.open", label="Open", title="Open the demo")
        ],
        commands=[
            ExtensionCommand(id="demo.open", title="Demo: Open", surface="main"),
            ExtensionCommand(id="demo.run", title="Demo: Run"),
        ],
        surfaces=[
            ExtensionSurface(id="main", kind="dialog", title="Demo"),
            ExtensionSurface(
                id="work",
                kind="pane",
                title="Work",
                multi=True,
                back_command="demo.open",
            ),
        ],
    )
    base.update(overrides)
    return ExtensionSpec(**base)


class TestSerialization:
    def test_to_dict_nests_plain_dicts(self):
        d = _spec().to_dict()
        assert d["module"] == "/extensions/demo/index.js"
        assert d["bar_label"] == "Demo"
        assert d["buttons"][0] == {
            "command": "demo.open",
            "label": "Open",
            "title": "Open the demo",
        }
        assert d["commands"][0] == {
            "id": "demo.open",
            "title": "Demo: Open",
            "surface": "main",
            "ref": None,
        }
        assert d["surfaces"][1] == {
            "id": "work",
            "kind": "pane",
            "title": "Work",
            "multi": True,
            "back_command": "demo.open",
        }
        # Defaults ride along explicitly so the SPA host reads them guard-free.
        assert d["stylesheet"] is False
        assert d["api_version"] == 1
        json.dumps(d)  # JSON-safe end to end

    def test_good_spec_validates_clean(self):
        assert validate_extension_spec("demo", _spec()) == []


class TestValidation:
    def test_bad_extension_id(self):
        for bad in ("", "Demo", "-x", "a_b", "a.b"):
            spec = ExtensionSpec(module="/x.js", bar_label="X")
            assert validate_extension_spec(bad, spec), bad

    def test_bad_surface_id(self):
        spec = _spec(
            buttons=[],
            commands=[],
            surfaces=[ExtensionSurface(id="Main!", kind="dialog", title="T")],
        )
        assert any("surface id" in p for p in validate_extension_spec("demo", spec))

    def test_command_id_dot_rule(self):
        # Wrong prefix, capitalized verb, nested dots, empty verb, no dot.
        for bad in ("other.open", "demo.Open", "demo.open.x", "demo.", "demoopen"):
            spec = _spec(
                buttons=[], commands=[ExtensionCommand(id=bad, title="T")], surfaces=[]
            )
            probs = validate_extension_spec("demo", spec)
            assert any("command id" in p for p in probs), bad

    def test_command_prefix_is_escaped_not_a_regex(self):
        # An id with a regex metacharacter must not let "aXb.cmd" match "a.b".
        spec = ExtensionSpec(
            module="/x.js",
            bar_label="X",
            commands=[ExtensionCommand(id="aXb.cmd", title="T")],
        )
        assert any("command id" in p for p in validate_extension_spec("a.b", spec))

    def test_unknown_surface(self):
        spec = _spec(
            buttons=[],
            commands=[ExtensionCommand(id="demo.x", title="T", surface="nope")],
            surfaces=[],
        )
        assert any(
            "unknown surface" in p for p in validate_extension_spec("demo", spec)
        )

    def test_ref_without_surface(self):
        spec = _spec(
            buttons=[],
            commands=[ExtensionCommand(id="demo.x", title="T", ref="new")],
            surfaces=[],
        )
        assert any(
            "ref but no surface" in p for p in validate_extension_spec("demo", spec)
        )

    def test_multi_surface_declarative_command_requires_ref(self):
        surfaces = [ExtensionSurface(id="q", kind="pane", title="Q", multi=True)]
        no_ref = _spec(
            buttons=[],
            surfaces=surfaces,
            commands=[ExtensionCommand(id="demo.q", title="T", surface="q")],
        )
        assert any(
            "without a ref" in p for p in validate_extension_spec("demo", no_ref)
        )
        # An explicit ref names the instance, so the same shape passes.
        with_ref = _spec(
            buttons=[],
            surfaces=surfaces,
            commands=[ExtensionCommand(id="demo.q", title="T", surface="q", ref="one")],
        )
        assert validate_extension_spec("demo", with_ref) == []

    def test_bad_back_command(self):
        # back_command must name a declared command…
        spec = _spec(
            buttons=[],
            commands=[],
            surfaces=[
                ExtensionSurface(
                    id="p", kind="pane", title="P", back_command="demo.gone"
                )
            ],
        )
        assert any("back_command" in p for p in validate_extension_spec("demo", spec))
        # …and is refused on a dialog even when the command exists.
        spec = _spec(
            buttons=[],
            commands=[ExtensionCommand(id="demo.x", title="T")],
            surfaces=[
                ExtensionSurface(
                    id="d", kind="dialog", title="D", back_command="demo.x"
                )
            ],
        )
        assert any("pane-only" in p for p in validate_extension_spec("demo", spec))

    def test_button_must_reference_a_declared_command(self):
        spec = _spec(
            buttons=[ExtensionButton(command="demo.gone", label="X")],
            commands=[],
            surfaces=[],
        )
        assert any(
            "unknown command" in p for p in validate_extension_spec("demo", spec)
        )

    def test_surface_kind_vocabulary(self):
        # House words only — "popup" is not a kind.
        spec = _spec(
            buttons=[],
            commands=[],
            surfaces=[ExtensionSurface(id="s", kind="popup", title="S")],
        )
        assert any("kind" in p for p in validate_extension_spec("demo", spec))

    def test_multi_is_pane_only(self):
        spec = _spec(
            buttons=[],
            commands=[],
            surfaces=[ExtensionSurface(id="s", kind="dialog", title="S", multi=True)],
        )
        assert any(
            "multi is pane-only" in p for p in validate_extension_spec("demo", spec)
        )

    def test_duplicate_ids_are_problems(self):
        spec = _spec(
            buttons=[],
            commands=[
                ExtensionCommand(id="demo.x", title="A"),
                ExtensionCommand(id="demo.x", title="B"),
            ],
            surfaces=[
                ExtensionSurface(id="s", kind="pane", title="A"),
                ExtensionSurface(id="s", kind="pane", title="B"),
            ],
        )
        probs = validate_extension_spec("demo", spec)
        assert any("duplicate command id" in p for p in probs)
        assert any("duplicate surface id" in p for p in probs)
