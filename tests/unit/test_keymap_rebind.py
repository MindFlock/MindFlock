"""User-rebindable keyboard shortcuts: the docs.

The "?" cheat-sheet doubles as the editor — click a rebindable row, press the
new combo — with per-row reset (↺) and a Restore-defaults button. Overrides
live per-device in localStorage ("mf_keymap"). The JS/CSS hooks are pinned
against their source modules in
frontend/src/__tests__/keymapRebindWiring.test.ts; this checks the user docs.
"""

from __future__ import annotations


def test_docs_describe_rebinding():
    with open("docs/web-ui.md", encoding="utf-8") as f:
        text = f.read()
    assert "Restore defaults" in text
    assert "localStorage" in text
