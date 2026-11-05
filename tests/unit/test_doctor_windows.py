"""The doctor on native Windows (docs/windows.md): Git for Windows (git.exe AND
its bash.exe), the session host instead of tmux, Python, the agent CLIs'
Windows installers, and ``doctor --fix`` as winget + PowerShell — while
Linux/macOS rows stay exactly as they were (tests/unit/test_doctor.py)."""

from __future__ import annotations

import base64
import importlib.util

import pytest

from backend import doctor
from backend.doctor import Check

GIT_WINGET = (
    "winget install --id Git.Git -e --source winget"
    " --accept-package-agreements --accept-source-agreements"
)


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "windows")


def _which(mapping):
    return lambda name: mapping.get(name)


def _decode(script: str) -> str:
    head = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand "
    assert script.startswith(head)
    return base64.b64decode(script[len(head) :]).decode("utf-16-le")


class TestGitForWindows:
    def test_missing_is_a_required_fail_with_the_winget_fix(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(doctor, "_git_bash", lambda: "")
        c = doctor.check_git()
        assert c.status == "fail" and c.group == doctor.GROUP_RUN
        assert c.label == "Git for Windows"
        assert c.cmd == GIT_WINGET and c.fix == GIT_WINGET and c.install is True
        assert c.pkg == ""  # winget installs are steps, not one batched run

    def test_git_and_its_bash_is_ok(self, monkeypatch):
        monkeypatch.setattr(
            doctor.shutil, "which", _which({"git": r"C:\Git\cmd\git.exe"})
        )
        monkeypatch.setattr(doctor, "_git_bash", lambda: r"C:\Git\bin\bash.exe")
        monkeypatch.setattr(
            doctor, "_run", lambda argv: (0, "git version 2.47.1.windows.1")
        )
        c = doctor.check_git()
        assert c.status == "ok"
        assert "2.47.1" in c.detail and r"C:\Git\bin\bash.exe" in c.detail

    def test_a_git_without_bash_is_not_enough(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"git": r"C:\x\git.exe"}))
        monkeypatch.setattr(doctor, "_git_bash", lambda: "")
        c = doctor.check_git()
        assert c.status == "fail" and "bash.exe" in c.detail
        assert c.cmd == GIT_WINGET and "MINDFLOCK_GIT_BASH" in c.detail

    def test_installed_but_off_path_says_open_a_new_terminal(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        monkeypatch.setattr(doctor, "_git_bash", lambda: r"C:\Git\bin\bash.exe")
        c = doctor.check_git()
        assert c.status == "warn" and "not on PATH" in c.detail
        assert c.cmd == "" and c.install is False

    def test_too_old_is_fail(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({"git": "git.exe"}))
        monkeypatch.setattr(doctor, "_git_bash", lambda: "bash.exe")
        monkeypatch.setattr(doctor, "_run", lambda argv: (0, "git version 2.10.0"))
        c = doctor.check_git()
        assert c.status == "fail" and c.cmd == GIT_WINGET


class TestSessionHost:
    def test_no_tmux_row_on_windows(self):
        assert doctor.check_tmux() is None

    def test_ok_with_conpty_and_the_packages(self, monkeypatch):
        monkeypatch.setattr(doctor, "_windows_build", lambda: 26100)
        monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
        c = doctor.check_session_host()
        assert c.id == "session-host" and c.status == "ok"
        assert c.group == doctor.GROUP_RUN and "26100" in c.detail

    def test_windows_before_1809_has_no_conpty(self, monkeypatch):
        monkeypatch.setattr(doctor, "_windows_build", lambda: 17134)
        c = doctor.check_session_host()
        assert c.status == "fail" and "17763" in c.detail

    def test_missing_packages_fail_with_the_reinstall(self, monkeypatch):
        monkeypatch.setattr(doctor, "_windows_build", lambda: 22631)
        monkeypatch.setattr(
            importlib.util, "find_spec", lambda name: None if name == "winpty" else 1
        )
        c = doctor.check_session_host()
        assert c.status == "fail" and "pywinpty" in c.detail and "pyte" not in c.detail
        assert "install.ps1" in c.cmd

    def test_registered_for_run_and_fix(self):
        assert doctor.CHECKS_BY_ID["session-host"] is doctor.check_session_host
        assert doctor.CHECKS_BY_ID["python"] is doctor.check_python


class TestPython:
    def test_this_interpreter_is_reported(self):
        c = doctor.check_python()
        assert c.status == "ok" and c.detail.startswith("Python 3.")


class TestAgentInstallers:
    @pytest.mark.parametrize(
        "name,cmd",
        [
            ("claude", "irm https://claude.ai/install.ps1 | iex"),
            ("codex", "npm install -g @openai/codex"),
            ("aider", "irm https://aider.chat/install.ps1 | iex"),
            ("opencode", "npm install -g opencode-ai"),
            # npm hints carry over, minus the POSIX `--prefix ~/.local`.
            ("cline", "npm install -g cline"),
            ("copilot", "npm install -g @github/copilot"),
        ],
    )
    def test_each_cli_gets_its_own_windows_installer(self, monkeypatch, name, cmd):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: name)
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda n: n)
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail" and c.cmd == cmd and c.install is True
        assert '"' not in c.cmd  # PowerShell 5.1 would mangle it (see the plan)

    @pytest.mark.parametrize("name", ["antigravity", "goose"])
    def test_a_posix_only_installer_is_never_offered(self, monkeypatch, name):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: name)
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda n: n)
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_agent_cli()
        assert c.status == "fail" and c.cmd == "" and c.install is False
        assert "curl" not in c.fix

    def test_a_provider_declaring_a_windows_installer_wins(self, monkeypatch):
        class P:
            def install_hint_windows(self):
                return "winget install --id Vendor.Cli -e"

        assert doctor._windows_agent_install("claude", P()) == (
            "winget install --id Vendor.Cli -e"
        )

    def test_node_comes_from_winget_for_an_npm_cli(self, monkeypatch):
        monkeypatch.setattr(doctor, "_default_provider_name", lambda: "cline")
        monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "")
        monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda n: n)
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        node = doctor.check_node()
        assert node.status == "warn" and node.install is True
        assert node.cmd.startswith("winget install --id OpenJS.NodeJS.LTS -e")


class TestOtherWindowsFixes:
    def test_uv_is_winget(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_uv()
        assert c.cmd.startswith("winget install --id astral-sh.uv -e") and c.install

    def test_gh_is_winget_and_still_optional(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", _which({}))
        c = doctor.check_gh()
        assert c.status == "info" and c.fix.startswith("winget install --id GitHub.cli")

    def test_cloudflared_is_winget(self):
        cmd, pkg = doctor._cloudflared_install()
        assert cmd.startswith("winget install --id Cloudflare.cloudflared") and not pkg

    def test_no_sudo_or_wsl_advice_anywhere(self):
        for tool in ("git", "tmux", "xclip", "bubblewrap"):
            fix = doctor._pkg_fix(tool)
            assert "sudo" not in fix and "WSL" not in fix


class TestWindowsInstallPlan:
    def _checks(self):
        return [
            Check(
                "agent-cli",
                "agent CLI (cline)",
                "fail",
                cmd="npm install -g cline",
                install=True,
            ),
            Check("git", "Git for Windows", "fail", cmd=GIT_WINGET, install=True),
            Check(
                "node",
                "Node.js (npm)",
                "warn",
                cmd=doctor._winget("OpenJS.NodeJS.LTS"),
                install=True,
            ),
            Check("gh", "gh", "warn", cmd="gh auth login"),  # a login: stays out
        ]

    def test_winget_first_then_the_installers(self):
        plan = doctor.install_plan(self._checks())
        assert [s["id"] for s in plan["steps"]] == ["git", "node", "agent-cli"]
        assert plan["packages"] == []

    def test_the_script_is_one_encoded_powershell_line(self):
        plan = doctor.install_plan(self._checks())
        script = plan["script"]
        assert "\n" not in script and '"' not in script and "'" not in script
        assert len(script) < 8191  # cmd.exe's command-line limit
        ps1 = _decode(script)
        assert ps1 == plan["ps1"]
        assert f"S 'Git for Windows' '{GIT_WINGET}'" in ps1
        assert ps1.index("Git.Git") < ps1.index("npm install -g cline")
        assert "#!/bin/sh" not in ps1 and "sudo" not in ps1
        assert "exit 1" in ps1 and "everything installed" in ps1

    def test_labels_and_commands_are_quoted_for_powershell(self):
        plan = doctor.install_plan(
            [Check("x", "it's a tool", "fail", cmd="echo 'hi'", install=True)]
        )
        assert "S 'it''s a tool' 'echo ''hi'''" in plan["ps1"]

    def test_a_command_with_double_quotes_is_left_out(self):
        plan = doctor.install_plan(
            [Check("x", "x", "fail", cmd='powershell -c "x"', install=True)]
        )
        assert plan == {"steps": [], "packages": [], "script": ""}


class TestLinuxIsUnchanged:
    def test_windows_rows_do_not_exist_elsewhere(self, monkeypatch):
        for kind in ("linux", "wsl", "macos"):
            monkeypatch.setattr(doctor.osenv, "os_kind", lambda k=kind: k)
            assert doctor.check_session_host() is None
            assert doctor.check_python() is None
