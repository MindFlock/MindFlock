"""Calm start: a first-time user sees one welcome at a time and one box in New.

Source-level (frontend/src), not bundle-level: every check here is about which
code path exists at all, and Vite's rewrites of the bundle would only add noise
to that. Comments are stripped first where a check bans a call, because the
modules document the retired calls in prose so nobody reinvents them.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _code(text: str) -> str:
    """Source with block, JSX and line comments removed (the ``[^:"'`]`` guard
    keeps a ``https://`` inside a string from reading as a comment)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(^|[^:\"'`\\])//.*$", r"\1", text, flags=re.M)


# NewSessionDialog.tsx and the modules it was split into, the dialog first and
# the rest in the order the form draws them, so the ordering checks below still
# read top-down.
_NEW_FILES = (
    "components/dialogs/NewSessionDialog.tsx",
    "components/dialogs/newSession.ts",
    "components/dialogs/newSession/DescribeSection.tsx",
    "components/dialogs/newSession/TemplatesStrip.tsx",
    "components/dialogs/newSession/FolderPicker.tsx",
    "components/dialogs/newSession/FolderBrowser.tsx",
    "components/dialogs/newSession/AgentAccount.tsx",
    "components/dialogs/newSession/WorkspaceFold.tsx",
    "components/dialogs/newSession/PromptFold.tsx",
    "components/dialogs/newSession/LaunchFold.tsx",
    "components/dialogs/newSession/FooterActions.tsx",
    "lib/newSessionSubmit.ts",
)


def _new() -> str:
    return "\n".join(_read(rel) for rel in _NEW_FILES)


class TestNewDialog:
    def test_saving_a_prompt_never_calls_window_prompt(self):
        # Electron implements no prompt(): the old Save… asked for a name with
        # window.prompt and silently did nothing in the desktop app.
        code = _code(_new())
        assert "window.prompt" not in code
        assert not re.search(r"(?<![\w.])prompt\(", code)
        # The form's one save is "Save as template…", an inline name row
        # (never a prompt()); prompts themselves are added in the Prompts
        # dialog, which New's Manage… opens and which hands back to New.
        assert 'id="new-save-tpl-name"' in code
        assert 'id="new-save-tpl-save"' in code
        assert "Insert a prompt…" in code
        assert 'openDialogFor("prompts", "new")' in code

    def test_the_tab_strip_waits_for_a_tracker(self):
        code = _code(_new())
        assert "const ticketingOk = !!config?.caps?.ticketing;" in code
        assert 'const shownTab: NewTab = ticketingOk ? tab : "session";' in code
        assert "{tabs && (" in code
        assert "tabs={ticketingOk}" in code

    def test_review_details_is_not_drawn_for_a_list_or_a_split(self):
        code = _code(_new())
        i = code.index('id="new-describe-go"')
        assert "{!runMode && (" in code[i - 400 : i]

    def test_the_help_line_sits_under_the_box_not_under_options(self):
        # "Your coding CLI reads this…" explains the box; with the Options fold
        # between them, "this" read as the fold.
        code = _code(_new())
        assert code.index('className="nf-describe-help"') < code.index("<RunOptions")

    def test_page_one_is_only_the_prompt(self):
        # The owner: "too much on that new first page — I should be able to
        # just prompt and it fills out the second menu if I want more detail,
        # or I just create session". Options (Fast-track, "Starts in") draw on
        # page 1 for an Intake batch only; Auto-split lives on the form.
        code = _code(_new())
        i = code.index("<RunOptions")
        assert "{batch && (" in code[i - 400 : i]
        # Fast-track and Auto-split sit under the form's More options.
        assert code.index('id="new-advanced"') < code.index('id="new-ft-row"')
        assert code.index('id="new-ft-row"') < code.index('id="new-split-row"')
        # Review details first is back for every single-session prompt.
        assert "const runMode = (page === 1 && draft.listMode) || splitOn;" in code

    def test_one_session_folds_its_options_with_the_rung_in_the_summary(self):
        code = _code(_read("components/dialogs/NewList.tsx"))
        assert 'id="new-options"' in code
        assert 'className="rt-fold"' in code
        assert '"Options · Fast-track: " + LANE_LABEL[lane]' in code
        assert '"mf_new_options_open"' in code
        # A list keeps every row on screen.
        assert 'if (many) return <div className="rt-opts">{body}</div>;' in code

    def test_split_box_is_absent_on_a_server_that_cannot_split(self):
        code = _code(_read("components/dialogs/SplitCheck.tsx"))
        assert "if (!gate.ok && gate.reason === SERVER_NO_SPLIT) return null;" in code

    def test_copy_points_at_the_bell_and_the_row_not_the_outbox(self):
        for rel in (*_NEW_FILES, "lib/runStart.ts", "lib/laneActions.ts"):
            code = _code(_read(rel))
            assert "in the Outbox" not in code, rel
            assert "Outbox first" not in code, rel


class TestFirstRun:
    def test_the_welcome_is_one_card_that_hands_over_to_the_coach_tour(self):
        """Round 5: the five-slide tour is one welcome card — a title, two
        plain lines and "Let's go", which starts the Welcome coach tour over
        the checklist. No slide ever sends a 0-session user into Intake's
        tracker wizard again ("Set up now"), and no exit stacks a second
        checklist modal ("Open Setup")."""
        src = _read("components/onboarding/WelcomeTour.tsx")
        assert "WELCOME_TITLE" in src and "WELCOME_LINES" in src
        assert "Let's go" in src
        assert "Set up now" not in src and "Open Setup" not in src
        assert "const SLIDES" not in src and "LEGACY_SCREEN_TABS" not in src
        assert 's.openDialog === "setup"' in src

    def test_setup_never_opens_itself(self):
        """The Setup modal opens from the chip, the doctor chip, the palette and
        `?setup=1` — never from a failing doctor probe (the old rule, a third
        first-run surface on top of the tour and the grid card)."""
        for rel in (
            "components/dialogs/SetupDialog.tsx",
            "components/setup/DoctorList.tsx",
        ):
            src = _code(_read(rel))
            assert "shouldAutoShowSetup" not in src
            assert "sessions > 0" not in src
        doctor = _code(_read("components/setup/DoctorList.tsx"))
        assert "consumeSetupIntent()" in doctor
        model = _read("components/setup/setupModel.ts")
        assert 'v !== "1" && v !== "install"' in model

    def test_app_never_opens_the_tour(self):
        """App never opens it; the one automatic opening is WelcomeTour's
        first-launch hook on the synced `welcome_tour` pref (pinned in
        onboarding.test.ts), never a per-browser flag."""
        app = _code(_read("App.tsx"))
        assert "tourDecision" not in app and "openTour" not in app
        store = _code(_read("state/store.ts"))
        assert "tourDone" not in store and "mf_tour_done" not in store
        palette = _read("components/palette/CommandPalette.tsx")
        assert "replayTour(id)" in palette
        assert "shortcuts-tour" in _read("components/palette/ShortcutsSheet.tsx")

    def test_setup_has_no_account_test_forms(self):
        src = _read("components/setup/SetupChecklist.tsx")
        # GitHub is the Connect GitHub dialog (its own Test), not a fold test;
        # a tracker's token goes in its Intake source, not a second form here.
        assert "setup-test-github" not in src
        assert "openConnectGitHub()" in src
        assert "setup-test-shortcut" not in src
        assert "setup-shortcut-token" not in src
        assert "Tokens live in" not in src

    def test_checklist_is_one_component_in_three_groups(self):
        src = _read("components/setup/SetupChecklist.tsx")
        for step in ("tools", "agent_login", "repo", "github", "tracker", "phone"):
            assert 'step("%s")' % step in src, step
        for group in ("start", "connect", "later"):
            assert 'data-group="%s"' % group in src, group
        # The phone is folded under "Optional, later", never a getting-started step.
        later = src.index('className="setup-later"')
        assert src.index('data-step="phone"') > later
        # Both surfaces render the same component.
        assert "<SetupChecklist />" in _read("components/grid/TerminalGrid.tsx")
        assert "<SetupChecklist />" in _read("components/dialogs/SetupDialog.tsx")
        # The phone extra: Install Tailscale (or start it, when it's installed
        # but not answering) OR the QR.
        assert '"install-tailscale"' in src and '"fix-tailscale"' in src
        assert 'data-phone="qr"' in src

    def test_grid_card_says_get_set_up(self):
        src = _read("components/grid/TerminalGrid.tsx")
        assert "<h2>Get set up</h2>" in src
        assert "Three steps to your first agent" in src
        assert "⋯ menu" not in src
        assert "Click a session's row in the sidebar to show it." in src
