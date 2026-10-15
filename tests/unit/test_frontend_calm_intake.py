"""Calm surface, Intake / Verify / Settings half: source-level pins.

What these hold in place:

- ONE Fast-track ladder. The pane's ⏩, the row › menu and New's Options read
  Off / Commit / Push / Open PR / Merge when green; Intake's pickers used to
  add a sixth word ("Agent only") for what Off already does. The intake rows
  offer an explicit ``off`` (the start routes accept it) and the source / repo
  defaults show a stored ``agent`` as Off.
- The per-row pickers fold behind an "Options" toggle, with a one-line summary
  of what Start will use.
- Settings → Security and → Accounts confirm inline: the desktop app has no
  native ``confirm()``, so the OLD call sites must stay gone, not merely gain a
  new row beside them.
- The Settings nav is grouped, without moving a key or a label.
- Auto-start rows say "will auto-start" only when their section is on.

Source-level on purpose: these are statements about which words and calls the
components contain, which a bundle-wide search would answer for the whole app
rather than for the file that owns them.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
_INTAKE = _SRC / "components" / "intake"
_SETTINGS = _SRC / "components" / "settings"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    """The file with its comments removed, so a sentence ABOUT a word (or a
    call) is not mistaken for the word being rendered (or the call made)."""
    text = _read(path)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


# --------------------------------------------------------------------------
# One ladder
# --------------------------------------------------------------------------


def test_no_intake_picker_renders_agent_only():
    for name in ("kit.tsx", "TicketsTab.tsx", "RepoSources.tsx"):
        code = _code(_INTAKE / name)
        assert "Agent only" not in code, name
        # ONE ladder: every picker draws its words from lib/autopilot's
        # LANE_LABEL, never a second table of its own.
        assert "DEPTH_LABELS" not in code, name


def test_the_ladder_has_no_agent_rung_to_render():
    src = _read(_INTAKE.parent.parent / "lib" / "autopilot.ts")
    assert (
        'export const SOURCE_DEPTHS: readonly Lane[] = ["commit", "push", "pr"];' in src
    )
    assert "Agent only" not in src


def test_source_and_repo_pickers_drop_the_agent_rung():
    for name in ("TicketsTab.tsx", "RepoSources.tsx"):
        code = _code(_INTAKE / name)
        assert "SOURCE_DEPTHS.map(" in code, name
        # The empty choice is Off, in the ladder's own word.
        assert '<option value="">{LANE_LABEL.leave}</option>' in code, name
        assert "Off — stop after the agent works" not in code, name


def test_row_picker_offers_an_explicit_off_and_names_its_default():
    code = _code(_INTAKE / "kit.tsx")
    assert '<option value="off">' in code
    # Offered only when the default is something other than Off.
    assert "!defaultIsOff &&" in code
    assert "SESSION_DEPTHS.map(" in code
    # "Default (…)" names what the empty choice resolves to — never the bare
    # "Configured (…)" jargon.
    assert '"Default (" + defaultDepth + ")"' in code
    assert '"Default (" + (configuredAgent || "app default") + ")"' in code
    assert "Configured (" not in code
    # The effort picker keeps its no-effort wording.
    assert "No effort (" in code


def test_a_stored_agent_rung_reads_as_off():
    # The one ladder folds every wire spelling of Off ("agent" included);
    # the row picker labels through it.
    ladder = _read(_INTAKE.parent.parent / "lib" / "autopilot.ts")
    assert 'if (s === "agent" || s === "off" || s === "none") return "leave";' in ladder
    assert "laneLabel(configuredDepth)" in _code(_INTAKE / "kit.tsx")
    assert 'source.depth === "agent" ? ""' in _code(_INTAKE / "TicketsTab.tsx")
    assert 'o.depth === "agent" ? ""' in _code(_INTAKE / "RepoSources.tsx")


def test_ticket_fast_track_hint_is_one_sentence_pair():
    src = re.sub(r"\s+", " ", _read(_INTAKE / "TicketsTab.tsx"))
    # Plain words (round 2): when it happens, and why Merge isn't offered here —
    # one line, because Fast-track now sits beside Agent in a two-column row
    # (edit in place), where a paragraph-length hint made the row ragged.
    assert "After the agent finishes. Merge is per ticket only." in src
    # The options themselves still say what it does (Commit / Push / Open PR).
    assert "SOURCE_DEPTHS.map((d) =>" in src


# --------------------------------------------------------------------------
# Row options fold
# --------------------------------------------------------------------------


def test_row_pickers_fold_behind_options():
    code = _code(_INTAKE / "kit.tsx")
    assert '"linklike ik-item-opts"' in code
    assert "aria-expanded={showPicks}" in code
    # Shown when opened OR when any picker holds a pick — a choice that will be
    # applied is never hidden.
    assert "const showPicks = optsOpen || picked;" in code
    assert "{showPicks && (" in code
    assert '"ik-item-uses"' in code
    # The collapsed line names Fast-track the way New's Options summary does:
    # a bare "Off" beside Start now read as "this item is off".
    assert '"Fast-track: " + (depth ? laneLabel(depth) : defaultDepth)' in code
    # The payload shapes the bundle pins rely on are untouched.
    for tab in ("PullRequestsTab.tsx", "IssuesTab.tsx", "TicketsTab.tsx"):
        assert "agent ? { agent } : {}" in _read(_INTAKE / tab), tab


# --------------------------------------------------------------------------
# Auto-start
# --------------------------------------------------------------------------


def test_auto_start_chip_depends_on_the_section_state():
    code = _code(_INTAKE / "QueueTab.tsx")
    assert 'eligible={s.state === "on"}' in code
    assert 'eligibleLabel={s.state === "on" ? "will auto-start" : ""}' in code
    # The unconditional chip is gone.
    assert not re.search(r"\beligible\s*\n\s*eligibleLabel=\"will auto-start\"", code)
    # ...and so is the toolbar's unconditional "N items will auto-start": it
    # counts only the sections that are on, and says "waiting" for the rest.
    assert '.filter((sec) => sec.state === "on")' in code
    assert 'total + " waiting · " + autoTotal + " will auto-start"' in code


# --------------------------------------------------------------------------
# Work first
# --------------------------------------------------------------------------


def test_github_tabs_have_no_repository_cards_under_the_work():
    # Add once, edit where it shows: no card list below the work. A watched
    # repository's heading carries Edit; "+ Add source" opens the add flow at
    # the top of the tab — which leads on its own while nothing is watched.
    for name in ("PullRequestsTab.tsx", "IssuesTab.tsx"):
        code = re.sub(r"\s+", " ", _code(_INTAKE / name))
        assert "RepoSourceList" not in code, name
        assert "const showAdd = !!adding || (!n && !dismissed);" in code, name
        assert '<WorkListPanel key="work"' in code, name
        # Order: the add flow, the switch, the GitHub line, the work, Advanced.
        order = [
            "{addFlow}",
            "<AutomationSwitch",
            "<GithubStatusLine",
            "{workList}",
            "<summary>Advanced options</summary>",
        ]
        idx = [code.index(o) for o in order]
        assert idx == sorted(idx), (name, idx)


def test_verify_puts_its_repositories_on_one_line_above_the_work():
    code = re.sub(
        r"\s+", " ", _code(_SRC / "components" / "dialogs" / "VerifyDialog.tsx")
    )
    assert "workFirst" not in code
    # Round 5 (B2): the add flow lives in the bottom fold, "Watch a GitHub
    # repository for every push", after New checklist — never above the work.
    order = [
        "<VerifySwitch",
        "{sources}",
        "{checklists}",
        "<VerifyNewChecklist",
        "Watch a GitHub repository for every push",
        "{showAdd ? ( addFlow )",
    ]
    idx = [code.index(o) for o in order]
    assert idx == sorted(idx), idx
    assert '<WorkListPanel key="work"' in code
    assert 'id="vf-repo-add-btn"' in code


def test_tickets_put_the_work_before_the_sources():
    code = _code(_INTAKE / "TicketsTab.tsx")
    body = code[code.index("export function TicketsTab(") :]
    assert body.index("<AssignedTickets") < body.index('id="ticketing-sources"')


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_security_and_accounts_never_call_a_native_confirm():
    for name in ("Security.tsx", "Accounts.tsx"):
        code = _code(_SETTINGS / "screens" / name)
        assert not re.search(r"\bconfirm\(", code), name
        assert "<InlineConfirm" in code, name
    sec = _read(_SETTINGS / "screens" / "Security.tsx")
    assert 'confirmLabel="Turn it off"' in sec
    assert "Regenerate" in sec
    # Same server calls as before.
    assert '"/api/settings/auth-token/rotate"' in sec
    assert 's.saveField("general", "auth_mode", "off")' in sec
    acc = _read(_SETTINGS / "screens" / "Accounts.tsx")
    assert 'confirmLabel="Remove anyway"' in acc
    assert "save(p.next, p.nextDefault, true)" in acc


def test_devices_screen_asks_inline_and_owns_settings_sync():
    """Settings → Your devices: Remove / Leave ask with InlineConfirm and codes are
    typed into inline inputs — the desktop app has no native confirm/prompt,
    so either would silently do nothing there. Settings sync moved here from
    Security (it only runs between your own devices)."""
    dev = _code(_SETTINGS / "screens" / "Devices.tsx")
    assert not re.search(r"\b(confirm|prompt|alert)\(", dev)
    assert "<InlineConfirm" in dev
    for route in (
        '"/api/fleet"',
        '"/api/fleet/invite"',
        '"/api/fleet/join"',
        '"/api/fleet/request"',
        '"/api/fleet/add-paired"',
        '"/api/fleet/leave"',
        '"/api/devices/refresh"',
        '"/api/settings/sync"',
        '"/api/settings/sync/now"',
        '"/api/settings/sync/pin"',
    ):
        assert route in dev, route
    sec = _code(_SETTINGS / "screens" / "Security.tsx")
    assert "SettingsSyncRows" not in sec
    assert "/api/settings/sync" not in sec
    # Trusted Tailscale accounts moved under Your devices → Advanced.
    assert "TailnetTrustRows" not in sec
    assert "TailnetTrustRows" in _code(_SETTINGS / "screens" / "DevicesAdvanced.tsx")


def _screens_block() -> str:
    src = _read(_SETTINGS / "SettingsDialog.tsx")
    return src.split("const SCREENS = [", 1)[1].split("] as const", 1)[0]


def test_settings_nav_order_and_groups():
    block = _screens_block()
    rows = re.findall(
        r'\{ key: "([^"]+)", label: "[^"]+"(?:, group: "([^"]+)")?', block
    )
    keys = [k for k, _ in rows]
    groups = {k: g for k, g in rows}
    assert keys == [
        "general",
        "connections",
        "notifications",
        "coding",
        "providers",
        "accounts",
        "localmodel",
        "orchestration",
        "workspace",
        "ide",
        "security",
        "appearance",
        "devices",
        "mobile",
        "peer",
        "extensions",
        "doctor",
        "logs",
        "advanced",
        "traffic",
    ]
    for k in ("general", "connections", "notifications"):
        assert not groups[k], k
    for k in ("coding", "providers", "accounts", "localmodel", "orchestration"):
        assert groups[k] == "Agents", k
    for k in ("workspace", "ide"):
        assert groups[k] == "Code", k
    for k in ("security", "appearance"):
        assert groups[k] == "This device", k
    # Your devices, Your phone and People: the machines and people you work with.
    for k in ("devices", "mobile", "peer"):
        assert groups[k] == "Devices and people", k
    # Extensions are something you add, not a fault: their own group.
    assert groups["extensions"] == "Extensions"
    for k in ("doctor", "logs", "advanced", "traffic"):
        assert groups[k] == "Troubleshooting", k
    # `group` comes AFTER label, so `{ key: "coding", label: "Agents"` stays
    # one contiguous phrase (round 2: "Agent CLI" became "Agents", GLOSSARY).
    assert '{ key: "coding", label: "Agents", group: "Agents"' in block


def test_settings_nav_draws_a_heading_per_group():
    src = _read(_SETTINGS / "SettingsDialog.tsx")
    assert '"set-nav-group"' in src
    # The heading rule lives in settingsMode.tsx (basic mode lists its few
    # screens flat; advanced mode heads each group).
    assert "navHeading(shown, i, advanced)" in src
    assert "s.group !== shown[i - 1]?.group" in _read(_SETTINGS / "settingsMode.tsx")
    assert ".set-nav-group {" in _read(_SETTINGS / "SettingsDialog.css")


def test_notifications_no_longer_points_at_the_sidebar_header():
    assert "sidebar header" not in _read(_SETTINGS / "screens" / "Notifications.tsx")


def test_general_leads_with_getting_started_and_the_mcp_has_its_own_screen():
    src = _read(_SETTINGS / "screens" / "General.tsx")
    body = src[src.index("export function General(") :]
    body = body[: body.index("\n}\n")]
    assert body.index("<Tours />") < body.index('<AdvancedFold id="general-advanced"')
    # The MCP rows moved out of General's fold to Agents → Agent orchestration.
    assert "Agent orchestration (MindFlock MCP)" not in src
    assert "AgentMcpRows" not in src
    orch = _read(_SETTINGS / "screens" / "AgentOrchestration.tsx")
    assert "function AgentMcpRows(" in orch and "<AgentMcpRows />" in orch
    assert "top bar" not in src
    assert "sidebar's Usage bar" in src


def test_settings_copy_fixes():
    screens = _SETTINGS / "screens"
    assert '"IDE auto-adopt on"' in _read(screens / "Ide.tsx")
    assert "Cursor auto-adopt on" not in _read(screens / "Ide.tsx")
    ws = re.sub(r"\s+", " ", _read(screens / "Workspace.tsx"))
    # Round 2: the intro says what the screen is for, not what isn't on it.
    assert "nothing to set here per repo" not in ws
    assert "Ticketing source" not in ws
    assert 'toast("Saved");' in _read(_SETTINGS / "useSettings.tsx")
    ext = _read(screens / "Extensions.tsx")
    assert "<summary>Create an extension</summary>" in ext


def test_platform_section_only_on_windows_or_when_set():
    src = _read(_SETTINGS / "screens" / "Advanced.tsx")
    assert "{showPlatform && (" in src
    assert "/Windows/.test(navigator.userAgent" in src
    assert '=== "win32"' in src
    assert 's.get("platform", "wsl_distro")' in src
    assert 's.get("platform", "wt_command")' in src
