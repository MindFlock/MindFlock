"""After-change effects for settings that arrive underneath the server
(:mod:`backend.web.core.settings_hooks`) and the "is this agent installed"
probe settings sync defers on."""

from __future__ import annotations

import threading

import pytest

from backend import providers
from backend.config import settings as store
from backend.web.core import events, settings_hooks, terminal


@pytest.fixture
def bus():
    """Every event emitted on the process bus during the test."""
    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    yield seen
    unsubscribe()


def _names(seen):
    return [e["event"] for e in seen]


@pytest.fixture(autouse=True)
def _no_pending_timer(monkeypatch):
    monkeypatch.setattr(settings_hooks, "_last_sig", None)
    yield
    with settings_hooks._TIMER_LOCK:
        if settings_hooks._pipeline_timer is not None:
            settings_hooks._pipeline_timer.cancel()
            settings_hooks._pipeline_timer = None


# --------------------------------------------------------------------------- #
# provider_installed
# --------------------------------------------------------------------------- #
def test_an_unknown_provider_is_not_installed_even_when_claude_is(monkeypatch):
    """providers.resolve falls back to claude for a name it doesn't know — the
    probe must not report "claude is installed" for a custom provider that
    only exists on another device."""
    monkeypatch.setattr(settings_hooks.shutil, "which", lambda b: "/usr/bin/" + b)
    assert settings_hooks.provider_installed("claude") is True
    assert settings_hooks.provider_installed("only-on-the-rig") is False
    assert settings_hooks.provider_installed("generic") is False
    assert settings_hooks.provider_installed("") is False
    assert settings_hooks.provider_installed("   ") is False


def test_a_known_provider_is_installed_only_with_its_binary(monkeypatch):
    monkeypatch.setattr(settings_hooks.shutil, "which", lambda b: None)
    assert settings_hooks.provider_installed("codex") is False
    monkeypatch.setattr(
        settings_hooks.shutil, "which", lambda b: "/x/codex" if b == "codex" else None
    )
    assert settings_hooks.provider_installed("codex") is True


def test_provider_installed_never_raises(monkeypatch):
    def boom(_name):
        raise RuntimeError("registry blew up")

    monkeypatch.setattr(providers, "resolve", boom)
    assert settings_hooks.provider_installed("codex") is False


def test_the_settings_addon_names_are_aliases():
    from backend.web.addons import settings as settings_addon

    assert settings_addon._provider_installed is settings_hooks.provider_installed
    assert settings_addon._installed_path is settings_hooks.installed_path


# --------------------------------------------------------------------------- #
# after_settings_change
# --------------------------------------------------------------------------- #
def test_every_change_is_announced_as_settings_synced(bus):
    settings_hooks.after_settings_change(["ui.accent", "ui.accent"], source="rig")
    synced = [e for e in bus if e["event"] == "settings.synced"]
    assert len(synced) == 1
    assert synced[0]["data"] == {"paths": ["ui.accent"], "from": "rig"}


def test_nothing_changed_means_nothing_happens(bus):
    settings_hooks.after_settings_change([], source="rig")
    assert bus == []


def test_a_synced_github_token_drops_the_cached_one(monkeypatch):
    from backend.ticket_ingestion import github_auth

    calls = []
    monkeypatch.setattr(github_auth, "invalidate", lambda: calls.append(1))
    settings_hooks.after_settings_change(["ui.accent"])
    assert calls == []
    settings_hooks.after_settings_change(["github.token"])
    assert calls == [1]


def test_a_burst_of_ticketing_changes_reconciles_the_pipeline_once(bus, monkeypatch):
    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.05)
    fired = threading.Event()
    seen = []

    def on(env):
        if env["event"] == settings_hooks.PIPELINE_EVENT:
            seen.append(env)
            fired.set()

    unsubscribe = events.BUS.subscribe(on)
    try:
        before = settings_hooks.pipeline_signature()
        store.set_ticketing_sources([{"id": "a", "provider": "jira"}])
        settings_hooks.after_settings_change(
            ["ticketing.sources#a"], pipeline_before=before
        )
        settings_hooks.after_settings_change(["github.repos"])
        settings_hooks.after_settings_change(["github.enabled"])
        assert seen == []  # debounced, not immediate
        assert fired.wait(2.0)
        threading.Event().wait(0.15)  # would a second one follow?
    finally:
        unsubscribe()
    assert len(seen) == 1


def test_unrelated_changes_leave_the_pipeline_alone(bus, monkeypatch):
    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.01)
    settings_hooks.after_settings_change(["ui.accent", "prefs.keymap"])
    threading.Event().wait(0.1)
    assert settings_hooks.PIPELINE_EVENT not in _names(bus)


def test_a_synced_scroll_speed_reaches_the_live_file(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    applied = []
    monkeypatch.setattr(terminal, "apply_scroll_speed", applied.append)
    store.update_settings(ui={"scroll_speed": 0.6667})
    settings_hooks.after_settings_change(["ui.scroll_speed"])
    assert terminal.load_scroll_speed() == 0.6667
    assert applied == [0.6667]
    store.update_settings(ui={"scroll_speed": None})  # cleared elsewhere
    settings_hooks.after_settings_change(["ui.scroll_speed"])
    assert terminal.load_scroll_speed() == 1


def test_synced_providers_rebuild_the_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(providers, "rebuild_registry", lambda: calls.append(1))
    settings_hooks.after_settings_change(["store:templates#x"])
    assert calls == []
    settings_hooks.after_settings_change(["store:providers#mine", "store:providers#b"])
    assert calls == [1]


def test_one_failing_effect_does_not_stop_the_rest(bus, monkeypatch):
    from backend.ticket_ingestion import github_auth

    def boom(*_a, **_k):
        raise RuntimeError("nope")

    monkeypatch.setattr(github_auth, "invalidate", boom)
    monkeypatch.setattr(providers, "rebuild_registry", boom)
    monkeypatch.setattr(settings_hooks, "_apply_scroll_speed", boom)
    settings_hooks.after_settings_change(
        ["github.token", "ui.scroll_speed", "store:providers#x"], source="rig"
    )
    assert "settings.synced" in _names(bus)


def test_a_bad_paths_argument_never_raises(bus):
    settings_hooks.after_settings_change(None)  # type: ignore[arg-type]
    settings_hooks.after_settings_change(object())  # type: ignore[arg-type]
    assert bus == []


# --------------------------------------------------------------------------- #
# which device runs PR review / issue handling
# --------------------------------------------------------------------------- #
def _fleet_of(monkeypatch, n):
    from backend.web.core import fleet

    monkeypatch.setattr(fleet, "in_fleet", lambda: n > 0)
    monkeypatch.setattr(
        fleet, "live_members", lambda: {"d%d" % i: {} for i in range(n)}
    )


@pytest.mark.parametrize(
    "run_here,members,want",
    [
        (None, 0, True),  # a lone device: as before
        (None, 1, True),  # a group of one
        (None, 2, False),  # one of several: not unless chosen
        (True, 3, True),
        (False, 0, False),
    ],
)
def test_automation_here(monkeypatch, run_here, members, want):
    _fleet_of(monkeypatch, members)
    store.update_settings(github={"run_here": run_here})
    assert settings_hooks.automation_here() is want


def test_run_here_is_device_local_and_round_trips():
    from backend.web.core import settings_sync

    assert "run_here" in settings_sync.LOCAL["github"]
    assert "run_here" not in settings_sync.SYNCED["github"]
    g = store.GithubSettings.from_dict({"run_here": False})
    assert g.run_here is False and g.to_dict() == {"run_here": False}
    assert store.GithubSettings().to_dict() == {}


def test_only_a_change_the_pipeline_cares_about_reconciles_it(bus, monkeypatch):
    """A synced label or grace period used to restart the pipeline on every
    device; only what the pipeline is wired with at boot does now."""
    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.01)
    store.update_settings(github={"repos": ["me/app"]})
    before = settings_hooks.pipeline_signature()
    store.update_settings(github={"min_age_minutes": 30, "skip_authors": ["bot"]})
    settings_hooks.after_settings_change(
        ["github.min_age_minutes", "github.skip_authors"], pipeline_before=before
    )
    threading.Event().wait(0.1)
    assert settings_hooks.PIPELINE_EVENT not in _names(bus)
    before = settings_hooks.pipeline_signature()
    store.update_settings(github={"repos": ["me/app", "me/other"]})
    settings_hooks.after_settings_change(["github.repos"], pipeline_before=before)
    # The timer thread can take a while to get scheduled late in a full run.
    for _ in range(100):
        if settings_hooks.PIPELINE_EVENT in _names(bus):
            break
        threading.Event().wait(0.05)
    assert settings_hooks.PIPELINE_EVENT in _names(bus)


def test_the_pipeline_debounce_is_five_seconds():
    assert settings_hooks.PIPELINE_DEBOUNCE == 5.0
