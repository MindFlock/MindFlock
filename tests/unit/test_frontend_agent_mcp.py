"""MindFlock MCP, frontend wiring: structural contract checks on the shipped
bundle (the wording itself is unit-tested in frontend/src/__tests__/
agentMessages.test.ts; the rendering was checked with the screenshot harness).

Pins: the rail row's lineage sub-line, the ``session.message`` toast + bell
curation, and the Settings → General attach switch + scope select bound to
``general.agent_mcp`` / ``general.agent_mcp_scope``.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle

client = TestClient(server.app)

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _js() -> str:
    return client.get("/app.js").text


def _css() -> str:
    return client.get("/style.css").text


def test_rail_row_renders_the_lineage_sub_line():
    js = _js()
    assert "function lineageMark(" in js
    assert '"↳ "' in js and '"↳ agent"' in js
    assert '"lineage"' in js and '" spawned"' in js
    assert '" has-lineage"' in js
    css = _css()
    assert in_bundle(".inst .meta.has-lineage { flex-direction: column;", css)
    assert ".inst .lineage.spawned" in css


def test_session_message_toasts_and_only_results_reach_the_bell():
    js = _js()
    assert 'subscribe("session.message"' in js
    assert "function messageToastText(" in js
    assert '"✉ "' in js and '" reported"' in js
    # Bell: routed through messageNotif, which drops plain messages.
    assert "function messageNotif(" in js
    assert in_bundle('case "session.message": return messageNotif(', js)


def test_session_message_toast_respects_replay():
    src = (_SRC / "components" / "EventToasts.tsx").read_text(encoding="utf-8")
    block = src[src.index('ev.subscribe("session.message"') :]
    block = block[: block.index("unsubs.push(")]
    assert "isReplay(env)" in block
    assert "notifyOnce(" in block  # throttled, never a bare toast()


def test_settings_general_binds_agent_mcp_and_scope():
    js = _js()
    assert (
        "Give agents the MindFlock MCP (agent-to-agent messaging and orchestration)"
        in js
    )
    assert '"agent_mcp"' in js
    assert '"agent_mcp_scope"' in js
    for value in ('"children"', '"readonly"', '"all"'):
        assert value in js
    assert "MINDFLOCK_AGENT_MCP=0" in js
    assert "next launch" in js


def test_agent_mcp_settings_never_use_native_dialogs():
    # Electron implements no window.prompt — and alert/confirm are as bad in a
    # settings row. Pin the absence in the component's source.
    src = (
        _SRC / "components" / "settings" / "screens" / "AgentOrchestration.tsx"
    ).read_text(encoding="utf-8")
    # The whole Agent orchestration screen (the MCP rows and the cap rows).
    assert "function AgentMcpRows(" in src
    assert not re.search(r"\b(prompt|alert|confirm)\(", src)


def test_row_types_carry_lineage_fields():
    src = (_SRC / "api" / "types.ts").read_text(encoding="utf-8")
    inst = src[src.index("export interface Instance {") :]
    inst = inst[: inst.index("\n}\n")]
    assert "parent?: string;" in inst
    assert "spawned?: boolean;" in inst
    assert "agent_mcp?: { enabled: boolean; providers: string[] };" in src
