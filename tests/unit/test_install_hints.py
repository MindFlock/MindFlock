"""Lint every provider's ``install_hint`` — the command the doctor RUNS when an
agent CLI is missing (in the one-shot install script, and from `doctor --fix`).

Each rule is a way a hint used to fail on a real machine:

* a bash-only vendor script piped to ``sh`` — dash (``/bin/sh`` on Debian,
  Ubuntu and every WSL distro) dies parsing it, which is how the DEFAULT agent
  could not be installed on the most common engine host;
* ``pip`` / ``python -m pip`` — PEP 668 refuses it on current distros, and
  ``python`` often doesn't exist;
* ``sudo npm`` / a bare ``npm install -g`` — EACCES against a distro Node's
  root-owned prefix; npm is allowed only into a user prefix, and only because
  the doctor then puts Node.js in the plan ahead of it (``check_node``).
"""

from __future__ import annotations

import re

import pytest

from backend import doctor, providers

#: Vendor installers verified to be POSIX sh (``#!/bin/sh``) — the only ones
#: allowed to be piped into ``sh``. Anything else gets ``| bash``.
POSIX_SH_INSTALLERS = (
    "https://chatgpt.com/codex/install.sh",
    "https://aider.chat/install.sh",
)

#: Vendor installers that are bash scripts (checked: ``#!/bin/bash`` /
#: ``#!/usr/bin/env bash``) and so must be piped into bash.
BASH_INSTALLERS = (
    "https://claude.ai/install.sh",
    "https://opencode.ai/install",
    "https://github.com/block/goose/releases/download/stable/download_cli.sh",
)

#: An npm hint must have exactly this shape: a user prefix whose bin dir is
#: ~/.local/bin (on PATH via pathenv), no sudo.
NPM_HINT = re.compile(r"^npm install -g --prefix ~/\.local [@a-z0-9/._-]+$")


def _hints():
    out = []
    for p in providers.all_providers():
        if p.name == "generic":
            continue
        hint = p.install_hint() or ""
        if hint:
            out.append((p.name, hint))
    return out


HINTS = _hints()


def test_every_default_cli_but_antigravity_has_an_installer():
    # agy ships inside the Antigravity app; there is no CLI installer to run.
    named = {n for n, _ in HINTS}
    assert {"claude", "codex", "aider", "opencode", "cline", "goose"} <= named


@pytest.mark.parametrize("name,hint", HINTS)
def test_no_pip(name, hint):
    assert not re.search(r"(^|\s)(pip3?|python3? -m pip)\s", hint), hint


@pytest.mark.parametrize("name,hint", HINTS)
def test_no_sudo(name, hint):
    assert "sudo" not in hint, hint


@pytest.mark.parametrize("name,hint", HINTS)
def test_piped_scripts_go_to_the_shell_they_are_written_for(name, hint):
    pipes = re.findall(r"curl [^|]*?(https://\S+)\s*\|\s*(?:\S+=\S+\s+)*(\S+)", hint)
    assert pipes or "curl" not in hint, f"unrecognized curl hint: {hint}"
    for url, shell in pipes:
        if shell == "sh":
            assert url in POSIX_SH_INSTALLERS, (
                f"{name}: {url} is piped to sh — verify it is POSIX sh and list "
                "it in POSIX_SH_INSTALLERS, or pipe it to bash"
            )
        else:
            assert shell == "bash", hint
        if url in BASH_INSTALLERS:
            assert shell == "bash", f"{name}: {url} is a bash script"


@pytest.mark.parametrize("name,hint", HINTS)
def test_npm_only_into_a_user_prefix_and_with_the_node_check(name, hint, monkeypatch):
    if not re.search(r"(^|[;&|(]\s*)npm\s", hint):
        return
    assert NPM_HINT.match(hint), hint
    # ...and the doctor puts Node.js in the plan when npm is missing.
    monkeypatch.setattr(doctor.osenv, "os_kind", lambda: "linux")
    monkeypatch.setattr(doctor, "_default_provider_name", lambda: name)
    monkeypatch.setattr(doctor, "_assistant_provider_name", lambda: "")
    monkeypatch.setattr(doctor, "_resolve_agent_binary", lambda n: n)
    monkeypatch.setattr(doctor.shutil, "which", lambda b: None)
    node = doctor.check_node()
    assert node is not None and node.install and node.pkg


def test_the_claude_hint_is_bash_even_when_npm_is_present(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/" + b)
    assert providers.get("claude").install_hint().endswith("| bash")
