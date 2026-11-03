"""The bundled GitHub Copilot CLI provider (npm @github/copilot): its launch
shapes (read off `copilot --help` 1.0.105), the MindFlock MCP attach as
`--additional-mcp-config` JSON in the shape `copilot mcp add` writes, and the
one-shot that answers without touching files."""

import json

from backend import providers as P
from backend.providers import mcp_attach
from backend.providers.base import LaunchContext
from backend.providers.copilot import CopilotProvider


def _p():
    p = P.resolve("copilot")
    assert isinstance(p, CopilotProvider)
    return p


def test_it_is_bundled_and_named():
    assert "copilot" in P.BUILTIN_NAMES
    from backend.web.addons.connections import AGENT_NAMES

    assert AGENT_NAMES["copilot"] == "GitHub Copilot"


def test_launch_shapes():
    p = _p()
    ctx = lambda **kw: LaunchContext(
        program="copilot", workdir="/tmp", **kw
    )  # noqa: E731
    assert p.build_launch_command(ctx()) == "copilot"
    assert p.build_launch_command(ctx(skip_permissions=True)) == "copilot --yolo"
    resumed = p.build_launch_command(ctx(resume=True, skip_permissions=True))
    assert resumed.startswith("copilot --yolo --continue || ")
    seeded = p.build_launch_command(ctx(prompt="fix it", skip_permissions=True))
    assert seeded.startswith("copilot --yolo -i ")


def test_oneshot_denies_shell_and_write():
    argv = _p().oneshot_argv("hello")
    assert argv[:4] == ["copilot", "-p", "hello", "-s"]
    assert "--allow-all-tools" in argv
    assert argv[argv.index("--deny-tool") + 1] == "shell"
    assert "write" in argv


def test_mcp_attach_is_one_additional_config_json():
    spec = mcp_attach.McpSpec(title="t", tmux_name="mindflock_t")
    flag, raw = _p().mcp_launch_args(spec)
    assert flag == "--additional-mcp-config"
    cfg = json.loads(raw)
    server = cfg["mcpServers"][mcp_attach.SERVER_NAME]
    assert server["type"] == "local" and server["tools"] == ["*"]
    assert server["command"] == spec.command()
    assert server["args"] == list(spec.args())
    assert all(isinstance(v, str) for v in server["env"].values())


def test_trust_gate_and_working_footer():
    p = _p()
    cfg = p.cfg
    assert "Do you trust the files in this folder?" in cfg.trust_patterns
    import re

    assert any(
        re.search(pat, " ○ Working esc esc edit prompt ")
        for pat in cfg.working_patterns
    )
    assert not any(
        re.search(pat, " ← open sidebar · Interactive · / commands · ? help")
        for pat in cfg.working_patterns
    )
    assert p.clear_context_command() == "/clear"
