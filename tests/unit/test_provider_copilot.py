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
    flag, raw, instructions = _p().mcp_launch_args(spec)
    assert flag == "--additional-mcp-config"
    # MindFlock's server instructions (its tool map) reach the prompt: without
    # them Copilot's own tool search missed start_flight & co.
    assert instructions == "--allow-all-mcp-server-instructions"
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


# --------------------------------------------------------------------------- #
# Sign-in evidence: the account `copilot login` records, not the file's
# existence (Copilot writes config.json on its very first run).
# --------------------------------------------------------------------------- #
def _no_token_env(monkeypatch, tmp_path):
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))


def test_a_first_run_config_is_no_sign_in(monkeypatch, tmp_path):
    _no_token_env(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(
        "// User settings belong in settings.json.\n"
        "// This file is managed automatically.\n"
        '{\n  "firstLaunchAt": "2026-11-03T22:31:30.122Z",\n  "appTipShown": true\n}\n'
    )
    assert _p().auth_evidence() == ""
    from backend import doctor

    # It still declares where to look, so a missing login is a warning with
    # Copilot's own login command (not "can't be checked").
    assert doctor._declares_auth_sources(_p()) is True
    assert doctor._declared_login_command(_p()) == "copilot login"


def test_copilot_login_records_the_account(monkeypatch, tmp_path):
    _no_token_env(monkeypatch, tmp_path)
    # The shape `copilot login` 1.0.105 writes (token in the keychain, or
    # plaintext under authTokens when there is none — never read here).
    (tmp_path / "config.json").write_text(
        "// This file is managed automatically.\n"
        + json.dumps(
            {
                "firstLaunchAt": "2026-11-03T22:31:30.122Z",
                "loggedInUsers": [{"host": "https://github.com", "login": "octo"}],
                "lastLoggedInUser": {"host": "https://github.com", "login": "octo"},
            }
        )
    )
    assert _p().auth_evidence() == "signed in as octo"


def test_a_token_in_the_environment_is_evidence(monkeypatch, tmp_path):
    _no_token_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "x")
    assert _p().auth_evidence() == "GH_TOKEN is set"


def test_an_unreadable_config_is_no_evidence(monkeypatch, tmp_path):
    _no_token_env(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{not json")
    assert _p().auth_evidence() == ""


# --------------------------------------------------------------------------- #
# Dialogs: approvals and the question form read as data (golden screens in
# test_dialogs_cli.py); the waiting patterns flag "Needs you".
# --------------------------------------------------------------------------- #
def test_it_reads_its_dialogs_and_flags_waiting():
    import re

    p = _p()
    assert p.parses_dialogs() is True
    pats = p.waiting_prompt_patterns()
    assert any(
        re.search(x, "│ ↑/↓ to navigate · enter to select · esc to cancel │")
        for x in pats
    )
    assert any(re.search(x, " Copilot needs information.") for x in pats)
    # Its idle footer is not a dialog.
    assert not any(
        re.search(x, " ← open sidebar · Interactive · Manual Approval · / commands")
        for x in pats
    )
    # Never auto-confirmed: an approval is the human's.
    assert p.cfg.idle_pattern == ""
