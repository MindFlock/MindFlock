"""Calm surface, Intake / Verify / Settings half: source-level pins.

What these hold in place:

- ONE Fast-track ladder. The pane's ⏩, the row › menu and New's Options read
  Off / Commit / Push / Open a PR / Merge when green; Intake's pickers used to
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
        # The ladder's own constant still carries "agent"; the pickers must not
        # render it as an option. Every source/repo picker filters it out, and
        # the row picker draws from the session ladder, which never had it.
        assert not re.search(r"\b(?:SOURCE_)?DEPTHS\.map\(", code), name


def test_source_and_repo_pickers_drop_the_agent_rung():
    for name in ("TicketsTab.tsx", "RepoSources.tsx"):
        code = _code(_INTAKE / name)
        assert 'SOURCE_DEPTHS.filter((d) => d !== "agent")' in code, name
        # The empty choice is Off, in the ladder's own word.
        assert '<option value="">{DEPTH_LABELS.off}</option>' in code, name
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
    kit = _code(_INTAKE / "kit.tsx")
    assert 'depth === "agent" ? "off"' in kit
    assert 'source.depth === "agent" ? ""' in _code(_INTAKE / "TicketsTab.tsx")
    assert 'o?.depth === "agent" ? ""' in _code(_INTAKE / "RepoSources.tsx")


def test_ticket_fast_track_hint_is_one_sentence_pair():
    src = re.sub(r"\s+", " ", _read(_INTAKE / "TicketsTab.tsx"))
    assert (
        "How far each ticket goes after its agent finishes. Merge is per-ticket only — "
        "a source default runs with nobody watching." in src
    )


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
    assert '"Fast-track: " + (depth ? ladderLabel(depth) : defaultDepth)' in code
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


def test_github_tabs_put_the_work_before_the_repositories_once_any_exist():
    for name in ("PullRequestsTab.tsx", "IssuesTab.tsx"):
        code = re.sub(r"\s+", " ", _code(_INTAKE / name))
        assert "{n ? [workList, sourceList] : [sourceList, workList]}" in code, name
        # KEYED, so saving the first repo (which flips the order) moves the two
        # instead of remounting them — a remount collapsed the card mid-edit.
        assert '<RepoSourceList key="sources"' in code, name
        assert '<WorkListPanel key="work"' in code, name


def test_verify_swaps_its_two_halves_by_key():
    code = re.sub(
        r"\s+", " ", _code(_SRC / "components" / "dialogs" / "VerifyDialog.tsx")
    )
    assert "{workFirst ? [checklists, sources] : [sources, checklists]}" in code
    assert '<VerifySources key="sources"' in code
    assert '<WorkListPanel key="work"' in code


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
    """Settings → Devices: Remove / Leave ask with InlineConfirm and codes are
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
    assert "TailnetTrustRows" in sec


def _screens_block() -> str:
    src = _read(_SETTINGS / "SettingsDialog.tsx")
    return src.split("> = [", 1)[1].split("];", 1)[0]


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
        "devices",
        "security",
        "appearance",
        "mobile",
        "peer",
        "doctor",
        "logs",
        "advanced",
        "extensions",
        "traffic",
    ]
    for k in ("general", "connections", "notifications"):
        assert not groups[k], k
    for k in ("coding", "providers", "accounts", "localmodel", "orchestration"):
        assert groups[k] == "Agents", k
    for k in ("workspace", "ide"):
        assert groups[k] == "Code", k
    # "Devices" (your other computers, settings sync) leads "This device":
    # it is where a second computer becomes one of yours.
    for k in ("devices", "security", "appearance", "mobile", "peer"):
        assert groups[k] == "This device", k
    for k in ("doctor", "logs", "advanced", "extensions", "traffic"):
        assert groups[k] == "Troubleshooting", k
    # `group` comes AFTER label, so `{ key: "coding", label: "Agent CLI"` stays
    # one contiguous phrase.
    assert '{ key: "coding", label: "Agent CLI", group: "Agents"' in block


def test_settings_nav_draws_a_heading_per_group():
    src = _read(_SETTINGS / "SettingsDialog.tsx")
    assert '"set-nav-group"' in src
    assert "s.group !== screens[i - 1]?.group" in src
    assert ".set-nav-group {" in _read(_SETTINGS / "SettingsDialog.css")


def test_notifications_no_longer_points_at_the_sidebar_header():
    assert "sidebar header" not in _read(_SETTINGS / "screens" / "Notifications.tsx")


def test_general_leads_with_getting_started_and_the_mcp_has_its_own_screen():
    src = _read(_SETTINGS / "screens" / "General.tsx")
    body = src[src.index("export function General(") :]
    body = body[: body.index("\n}\n")]
    assert body.index("<GettingStarted />") < body.index('"set-section-title">General<')
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
    assert "ticket source (Intake → Tickets)" in ws
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
