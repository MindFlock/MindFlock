"""The neutral thinking-effort ladder and its per-CLI translation.

Every assertion about a flag here was read off the installed CLI, not guessed:

* ``claude --effort bogus`` prints "Valid values: low, medium, high, xhigh, max"
  and then runs at the DEFAULT effort — so forwarding an unsupported level would
  silently ignore the pick, which is why the ladder clamps. ``claude --effort
  ultracode`` prints no such warning: the flag takes it (xhigh effort plus
  standing dynamic-workflow orchestration) even though the help text lists only
  the five ordinary rungs.
* ``codex -c model_reasoning_effort=<x>`` echoes "reasoning effort: <x>" at
  launch and forwards it to the API, which rejects an unknown value (a 400) and
  refuses ``minimal`` outright while codex's default web_search tool is on.
* ``agy --help``: "--effort  Reasoning effort for the current CLI session
  (low|medium|high)" — three rungs, so xhigh/max/ultra all land on high.
"""

import pytest

from backend import providers
from backend.providers import effort


class TestLadder:
    def test_the_rungs_are_ordered_cheapest_first(self):
        assert effort.EFFORTS == ("low", "medium", "high", "xhigh", "max", "ultra")

    def test_normalize_accepts_the_ladder_and_nothing_else(self):
        assert effort.normalize("ULTRA ") == "ultra"
        assert effort.normalize("teleport") == ""
        assert effort.normalize(None) == ""

    def test_validate_refuses_junk_but_allows_absent(self):
        """A typo that quietly ran at the CLI's default is worse than a refusal —
        the same rule the agent and depth overrides follow."""
        assert effort.validate("") == ""
        assert effort.validate(None) == ""
        assert effort.validate("max") == "max"
        with pytest.raises(ValueError):
            effort.validate("teleport")


class TestClaude:
    def test_a_supported_rung_is_passed_through(self):
        p = effort.plan("claude", "xhigh")
        assert p.args == ("--effort", "xhigh")
        assert p.note == ""

    def test_the_top_rung_asks_the_flag_for_ultracode(self):
        """The bug this pins: ``ultra`` used to clamp to ``max``, so picking the
        top rung started an ordinary max session. ``ultracode`` is a value the
        flag takes, so it is asked for by name — for the whole session."""
        p = effort.plan("claude", "ultra")
        assert p.args == ("--effort", "ultracode")
        assert p.applied == "ultracode"
        assert p.note == "", "asked for ultra, got ultra — nothing to explain"

    def test_the_flag_says_it_so_the_prompt_does_not(self):
        """The keyword opts in ONE turn; the flag opts in the session. Adding
        both would put a stray word in the ticket body for no extra effect."""
        assert effort.plan("claude", "ultra").prompt_keyword == ""
        assert effort.decorate_prompt("Fix the crash.\n", "claude", "ultra") == (
            "Fix the crash.\n"
        )

    def test_a_lower_rung_leaves_the_prompt_alone(self):
        assert effort.decorate_prompt("Body", "claude", "high") == "Body"

    def test_max_is_still_max(self):
        """The rung below the top is an ordinary level, untouched by any of
        this — ``ultra`` is a mode beside the ladder, not a sixth rung."""
        assert effort.plan("claude", "max").args == ("--effort", "max")


class TestCodex:
    def test_effort_is_a_config_key_not_a_flag(self):
        assert effort.plan("codex", "high").args == (
            "-c",
            "model_reasoning_effort=high",
        )

    def test_a_rung_above_its_ceiling_clamps_and_says_so(self):
        p = effort.plan("codex", "max")
        assert p.args == ("-c", "model_reasoning_effort=xhigh")
        assert p.applied == "xhigh"
        assert "tops out at xhigh" in p.note

    def test_minimal_is_not_on_the_ladder(self):
        """codex accepts it, but the API refuses it alongside the web_search tool
        codex enables by default — offering it would offer a start that 400s."""
        cfg = [c for c in providers.config.BUILTIN_CONFIGS if c.name == "codex"][0]
        assert "minimal" not in cfg.effort_levels

    def test_ultra_without_a_keyword_is_just_its_top_level(self):
        p = effort.plan("codex", "ultra")
        assert p.prompt_keyword == ""
        assert p.applied == "xhigh"
        assert effort.decorate_prompt("Body", "codex", "ultra") == "Body"


class TestAntigravity:
    def test_three_rungs_and_everything_above_lands_on_high(self):
        for rung in ("xhigh", "max", "ultra"):
            p = effort.plan("agy", rung)
            assert p.args == ("--effort", "high"), rung
            assert "tops out at high" in p.note


class TestUnsupported:
    def test_a_cli_without_effort_control_adds_nothing_and_explains(self):
        p = effort.plan("aider", "max")
        assert p.args == ()
        assert p.prompt_keyword == ""
        assert "no effort setting" in p.note
        assert p.changes_launch is False

    def test_a_custom_program_is_named_by_its_command_not_by_generic(self):
        """ "generic has no effort setting" would name a provider the user never
        chose; the note has to name the thing they typed."""
        p = effort.plan("/opt/bin/weirdcli --flag", "high")
        assert "weirdcli" in p.note
        assert "generic" not in p.note

    def test_asking_for_nothing_is_never_a_note(self):
        for program in ("claude", "aider"):
            p = effort.plan(program, "")
            assert p == effort.EffortPlan()


class TestCapability:
    def test_only_rungs_the_cli_can_distinguish_are_advertised(self):
        assert effort.capability(providers.get("claude"))["levels"] == list(
            effort.EFFORTS
        )
        assert effort.capability(providers.get("codex"))["levels"] == [
            "low",
            "medium",
            "high",
            "xhigh",
        ]
        assert effort.capability(providers.get("antigravity"))["levels"] == [
            "low",
            "medium",
            "high",
        ]
        assert effort.capability(providers.get("aider")) == {
            "levels": [],
            "ultra_level": "",
            "keyword": "",
        }

    def test_the_top_rungs_own_name_is_reported_so_the_ui_can_show_it(self):
        """The picker says "Ultra (ultracode)" — it can only do that if the CLI's
        own name for the rung comes down with the capability."""
        cap = effort.capability(providers.get("claude"))
        assert cap["ultra_level"] == "ultracode"
        assert cap["keyword"] == "", "the flag carries it; the prompt does not"


class TestUserTomlProvider:
    def test_a_custom_cli_declares_its_own_effort_vocabulary(
        self, tmp_path, monkeypatch
    ):
        """Zero-Python parity: the whole point of the config-driven providers."""
        (tmp_path / "mycli.toml").write_text(
            "\n".join(
                [
                    "[provider]",
                    'name = "mycli"',
                    "[launch]",
                    'effort_args = ["--brain", "{level}"]',
                    'effort_levels = ["low", "high"]',
                    'effort_keyword = "megathink"',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path))
        providers.rebuild_registry()
        try:
            p = effort.plan("mycli", "medium")
            # Its ladder has a hole where medium would be: clamp DOWN to low.
            assert p.args == ("--brain", "low")
            # No flag value for the top rung, so the keyword path still applies:
            # appended after the item body, alone on its line (it is a token the
            # CLI matches, not a sentence).
            assert effort.plan("mycli", "ultra").prompt_keyword == "megathink"
            out = effort.decorate_prompt("Fix the crash.\n", "mycli", "ultra")
            assert out.startswith("Fix the crash.")
            assert out.endswith("megathink\n")
            assert "\nmegathink\n" in out
            assert effort.decorate_prompt("", "mycli", "ultra") == ""
            assert effort.capability(providers.get("mycli")) == {
                "levels": ["low", "high", "ultra"],
                "ultra_level": "",
                "keyword": "megathink",
            }
        finally:
            monkeypatch.delenv("MINDFLOCK_PROVIDERS_DIR", raising=False)
            providers.rebuild_registry()

    def test_a_custom_cli_can_name_the_top_rung_for_its_flag(
        self, tmp_path, monkeypatch
    ):
        """The claude shape, from TOML: a flag value for the top rung. It is NOT
        on the ordinary ladder, so it is never reached by clamping — and it wins
        over a keyword rather than doing both."""
        (tmp_path / "mycli.toml").write_text(
            "\n".join(
                [
                    "[provider]",
                    'name = "mycli"',
                    "[launch]",
                    'effort_args = ["--brain", "{level}"]',
                    'effort_levels = ["low", "high"]',
                    'effort_ultra_level = "galaxybrain"',
                    'effort_keyword = "megathink"',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path))
        providers.rebuild_registry()
        try:
            p = effort.plan("mycli", "ultra")
            assert p.args == ("--brain", "galaxybrain")
            assert p.prompt_keyword == ""
            assert p.note == ""
            assert effort.decorate_prompt("Body", "mycli", "ultra") == "Body"
            assert effort.plan("mycli", "max").args == ("--brain", "high")
            assert effort.capability(providers.get("mycli")) == {
                "levels": ["low", "high", "ultra"],
                "ultra_level": "galaxybrain",
                "keyword": "",
            }
        finally:
            monkeypatch.delenv("MINDFLOCK_PROVIDERS_DIR", raising=False)
            providers.rebuild_registry()
