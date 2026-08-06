"""The Intake: per-repo cards, source-grouped work lists, and the two
provider bugs the unification exposed.

Three things are under test here, and they are related by one idea — "which CLI
runs this, and which filters apply to it" must be answerable per source rather
than per screen:

1. **Per-repo overrides.** ``github.repo_settings`` / ``issue_repo_settings``
   make each watched repository's card mean something: its own agent CLI, base
   branch, grace period and skip list, inherited from the tab-wide value when
   blank. Covered from the store's normalizer all the way into the monitors that
   actually apply the filters — a card that saved a value the monitor never read
   would be the worst possible outcome.
2. **Fresh per-source agents.** The pipeline reads its config once at boot, so
   stamping a ticket with the scanner's snapshot meant switching a source's Agent
   CLI in the UI kept launching the OLD CLI until a restart.
3. **Per-start agent overrides.** Every force-start row can run on a different
   CLI for that one launch, without re-configuring the queue.
"""

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion.config import (
    EngineConfig,
    GithubConfig,
    PipelineConfig,
    _parse_github,
)
from backend.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store):
    """Delegate to the shared settings-store isolation (tests/conftest.py)."""


def _gh(**kw) -> GithubConfig:
    """A GithubConfig with only the fields the resolvers read."""
    base = dict(
        base_branch="",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
    )
    base.update(kw)
    return GithubConfig(**base)


# --------------------------------------------------------------------------- #
# 1a. The store's normalizer
# --------------------------------------------------------------------------- #
class TestRepoOverrideNormalizer:
    def test_keeps_only_the_known_keys(self):
        out = S._repo_overrides(
            {"org/a": {"agent": "codex", "poll_interval_seconds": 5, "nonsense": 1}}
        )
        assert out == {"org/a": {"agent": "codex"}}

    def test_blank_values_are_dropped_so_a_cleared_field_means_inherit(self):
        # This is the whole contract of a card field: emptying it must fall back
        # to the tab default, not pin an empty value.
        assert S._repo_overrides({"org/a": {"agent": "", "base_branch": "  "}}) == {}

    def test_skip_authors_accepts_the_comma_string_the_form_sends(self):
        out = S._repo_overrides({"org/a": {"skip_authors": "dependabot, renovate"}})
        assert out == {"org/a": {"skip_authors": ["dependabot", "renovate"]}}

    def test_min_age_is_coerced_from_the_number_input_s_string(self):
        assert S._repo_overrides({"org/a": {"min_age_minutes": "30"}}) == {
            "org/a": {"min_age_minutes": 30}
        }

    def test_a_garbage_min_age_is_dropped_rather_than_raising(self):
        # The store is read on every settings load; a hand-edited file must not
        # be able to take the app down.
        assert S._repo_overrides({"org/a": {"min_age_minutes": "soon"}}) == {}

    @pytest.mark.parametrize("bad", ["not a dict", {"": {"agent": "codex"}}, None, 7])
    def test_malformed_input_yields_an_empty_map(self, bad):
        assert S._repo_overrides(bad) == {}


# --------------------------------------------------------------------------- #
# 1b. Round-tripping through the settings store
# --------------------------------------------------------------------------- #
class TestGithubSettingsCarriesTheOverrides:
    def test_roundtrips_both_maps(self):
        gs = S.GithubSettings(
            repos=["org/a"],
            issue_repos=["org/b"],
            repo_settings={"org/a": {"agent": "codex", "min_age_minutes": 5}},
            issue_repo_settings={"org/b": {"skip_authors": ["dependabot"]}},
        )
        back = S.GithubSettings.from_dict(gs.to_dict())
        assert back.repo_settings == {"org/a": {"agent": "codex", "min_age_minutes": 5}}
        assert back.issue_repo_settings == {"org/b": {"skip_authors": ["dependabot"]}}

    def test_empty_maps_are_omitted_from_the_file(self):
        assert "repo_settings" not in S.GithubSettings(repos=["org/a"]).to_dict()

    def test_base_branch_is_not_carried_for_issue_repos(self):
        """Issue work branches off the repo's own default, so a base-branch
        override there would be a field that silently does nothing."""
        back = S.GithubSettings.from_dict(
            {"issue_repo_settings": {"org/b": {"base_branch": "develop"}}}
        )
        assert back.issue_repo_settings == {}

    def test_the_settings_layer_reaches_the_pipeline_config(self):
        S.update_settings(
            github={
                "repos": ["org/a"],
                "repo_settings": {"org/a": {"min_age_minutes": 45}},
                "issue_repos": ["org/b"],
                "issue_repo_settings": {"org/b": {"agent": "aider"}},
            }
        )
        from backend.ticket_ingestion.config import load_config

        gh = load_config().github
        assert gh.min_age_for("org/a") == 45
        assert gh.issue_agent_for_repo("org/b") == "aider"


# --------------------------------------------------------------------------- #
# 1c. The resolvers every filter goes through
# --------------------------------------------------------------------------- #
class TestPerRepoResolution:
    def test_a_card_value_wins_and_an_absent_one_inherits(self):
        gh = _gh(
            base_branch="main",
            min_age_minutes=15,
            skip_authors=["bot"],
            repo_settings={"org/a": {"min_age_minutes": 60, "base_branch": "develop"}},
        )
        assert (gh.min_age_for("org/a"), gh.base_branch_for("org/a")) == (60, "develop")
        assert (gh.min_age_for("org/b"), gh.base_branch_for("org/b")) == (15, "main")
        # skip_authors was not overridden on either, so both inherit.
        assert gh.skip_authors_for("org/a") == ["bot"]

    def test_an_unknown_repo_inherits_everything(self):
        gh = _gh(min_age_minutes=15, repo_settings={"org/a": {"min_age_minutes": 60}})
        assert gh.min_age_for("who/knows") == 15

    def test_the_two_maps_do_not_leak_into_each_other(self):
        gh = _gh(
            min_age_minutes=15,
            issue_min_age_minutes=20,
            repo_settings={"org/a": {"min_age_minutes": 60}},
        )
        assert gh.min_age_for("org/a") == 60
        assert gh.issue_min_age_for("org/a") == 20

    def test_overrides_survive_the_toml_parse(self):
        gh = _parse_github(
            {
                "github": {
                    "base_branch": "main",
                    "repo_settings": {"org/a": {"agent": "codex"}},
                }
            },
            Path("config.toml"),
        )
        assert gh.agent_for_repo("org/a") == "codex"


class TestPerRepoAgentChain:
    """The card, then the tab-wide value, then the pipeline-wide one."""

    def _cfg(self, **gh) -> PipelineConfig:
        cfg = PipelineConfig()
        cfg.github = _gh(**gh)
        cfg.engine = EngineConfig(agent="goose")
        return cfg

    def test_card_outranks_the_tab_default(self):
        cfg = self._cfg(agent="claude", repo_settings={"org/a": {"agent": "codex"}})
        assert cfg.pr_agent("org/a") == "codex"
        assert cfg.pr_agent("org/b") == "claude"

    def test_no_repo_argument_still_answers_the_tab_default(self):
        """Every existing caller passes nothing; it must behave as before."""
        assert self._cfg(agent="claude").pr_agent() == "claude"

    def test_falls_through_to_the_pipeline_agent(self):
        assert self._cfg().pr_agent("org/a") == "goose"

    def test_issues_have_their_own_chain(self):
        cfg = self._cfg(
            agent="claude",
            issue_agent="aider",
            issue_repo_settings={"org/b": {"agent": "codex"}},
        )
        assert cfg.issue_agent("org/b") == "codex"
        assert cfg.issue_agent("org/c") == "aider"
        # Never inherits PR review's — separate features, separate lists.
        cfg2 = self._cfg(agent="claude")
        assert cfg2.issue_agent("org/b") == "goose"


# --------------------------------------------------------------------------- #
# 1d. The monitors actually apply them
# --------------------------------------------------------------------------- #
def _pr(repo, number, *, age_min, author="me", base="main"):
    from backend.ticket_ingestion.models import PullRequest

    return PullRequest(
        number=number,
        head_ref="feat",
        head_sha="deadbeef",
        base_ref=base,
        title="t",
        url="https://example.invalid",
        author=author,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
        repo=repo,
        clone_url="",
    )


class TestPRMonitorHonoursPerRepoFilters:
    def _scan(self, monkeypatch, gh, prs):
        from backend.ticket_ingestion import pr_monitor

        monkeypatch.setattr(pr_monitor, "load_processed_prs", lambda _d: set())
        monitor = pr_monitor.PRMonitor(gh)

        async def _list(repo, *, all_bases=False):
            return [p for p in prs if p.repo == repo]

        monkeypatch.setattr(monitor, "_list_prs", _list)
        monkeypatch.setattr(
            monitor, "_authenticated_user_login", lambda: _async_value("me")
        )
        return _run(monitor.scan())

    def test_the_grace_period_is_resolved_per_repo(self, monkeypatch):
        gh = _gh(
            repos=["org/slow", "org/fast"],
            min_age_minutes=30,
            repo_settings={"org/fast": {"min_age_minutes": 1}},
        )
        prs = [_pr("org/slow", 1, age_min=5), _pr("org/fast", 2, age_min=5)]
        # Same age, opposite verdicts — only the per-repo value differs.
        assert [p.number for p in self._scan(monkeypatch, gh, prs)] == [2]

    def test_the_base_filter_is_asked_of_github_per_repo(self, monkeypatch):
        """The monitor narrows server-side, so what it ASKS for is the contract."""
        from backend.ticket_ingestion import pr_monitor

        gh = _gh(
            base_branch="main",
            repo_settings={"org/b": {"base_branch": "develop"}},
        )
        asked: dict = {}

        class _Resp:
            status = 200

            async def json(self):
                return []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def get(self, url, params=None, headers=None):
                asked[url.rsplit("/repos/", 1)[1]] = params.get("base")
                return _Resp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(pr_monitor.aiohttp, "ClientSession", lambda **k: _Session())
        monkeypatch.setattr(pr_monitor, "resolve_token", lambda _c: _async_value("t"))
        monitor = pr_monitor.PRMonitor(gh)
        _run(monitor._list_prs("org/a"))
        _run(monitor._list_prs("org/b"))
        assert asked == {"org/a/pulls": "main", "org/b/pulls": "develop"}

    def test_all_bases_drops_the_filter_for_the_ui(self, monkeypatch):
        """The panel promises every open PR with a reason, so it must not have
        rows removed server-side — it explains them in a chip instead."""
        from backend.ticket_ingestion import pr_monitor

        seen: list = []

        class _Resp:
            status = 200

            async def json(self):
                return []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Session:
            def get(self, url, params=None, headers=None):
                seen.append(params.get("base"))
                return _Resp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(pr_monitor.aiohttp, "ClientSession", lambda **k: _Session())
        monkeypatch.setattr(pr_monitor, "resolve_token", lambda _c: _async_value("t"))
        monitor = pr_monitor.PRMonitor(_gh(base_branch="main"))
        _run(monitor._list_prs("org/a", all_bases=True))
        assert seen == [None]


def _issue(repo, number, *, age_min, author="someone"):
    from backend.ticket_ingestion.models import Issue

    return Issue(
        number=number,
        title="t",
        body="b",
        url="https://example.invalid",
        author=author,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
        repo=repo,
        clone_url="",
    )


class TestIssueMonitorHonoursPerRepoFilters:
    def _scan(self, monkeypatch, gh, issues):
        from backend.ticket_ingestion import issue_monitor

        monkeypatch.setattr(issue_monitor, "load_processed_issues", lambda _d: set())
        monitor = issue_monitor.IssueMonitor(gh)

        async def _list(repo):
            return [i for i in issues if i.repo == repo]

        monkeypatch.setattr(monitor, "_list_issues", _list)
        return _run(monitor.scan())

    def test_the_grace_period_is_resolved_per_repo(self, monkeypatch):
        gh = _gh(
            issue_repos=["org/slow", "org/fast"],
            issue_min_age_minutes=30,
            issue_repo_settings={"org/fast": {"min_age_minutes": 1}},
        )
        issues = [_issue("org/slow", 1, age_min=5), _issue("org/fast", 2, age_min=5)]
        assert [i.number for i in self._scan(monkeypatch, gh, issues)] == [2]

    def test_the_skip_list_is_resolved_per_repo(self, monkeypatch):
        gh = _gh(
            issue_repos=["org/a", "org/b"],
            issue_min_age_minutes=0,
            issue_skip_authors=["dependabot"],
            issue_repo_settings={"org/b": {"skip_authors": ["renovate"]}},
        )
        issues = [
            _issue("org/a", 1, age_min=5, author="dependabot"),
            _issue("org/a", 2, age_min=5, author="renovate"),
            _issue("org/b", 3, age_min=5, author="dependabot"),
            _issue("org/b", 4, age_min=5, author="renovate"),
        ]
        # a skips dependabot (inherited), b skips renovate (its own) — and b's
        # override REPLACES the inherited list rather than adding to it.
        assert sorted(i.number for i in self._scan(monkeypatch, gh, issues)) == [2, 3]


def test_pr_comment_skip_list_is_resolved_per_repo():
    """PR review only ever takes your own PRs, so skip_authors drops review
    COMMENTS. Per-repo because a bot that reviews one repo often doesn't touch
    the next."""
    import inspect

    from backend.ticket_ingestion import pr_comments

    src = inspect.getsource(pr_comments.fetch_actionable_comments)
    assert "config.skip_authors_for(pr.repo)" in src


# --------------------------------------------------------------------------- #
# 1e. The panel's chips mirror the monitor exactly
# --------------------------------------------------------------------------- #
class TestPanelReasonsMatchThePerRepoFilters:
    def test_the_grace_period_reason_uses_the_repo_s_own_value(self):
        from backend.web.core import pr_review

        gh = _gh(min_age_minutes=1, repo_settings={"org/a": {"min_age_minutes": 90}})
        reasons = pr_review.skip_reasons(_pr("org/a", 1, age_min=10), set(), "me", gh)
        assert any("grace period" in r for r in reasons)
        # …and the repo that inherits the 1-minute default is eligible.
        assert (
            pr_review.skip_reasons(_pr("org/b", 2, age_min=10), set(), "me", gh) == []
        )

    def test_a_pr_into_an_unwatched_base_says_so_instead_of_looking_queued(self):
        """The panel lists every open PR now (all_bases), so a PR the monitor
        will never see has to carry the reason — otherwise a chipless row reads
        as "queued for auto review"."""
        from backend.web.core import pr_review

        gh = _gh(min_age_minutes=0, base_branch="main")
        reasons = pr_review.skip_reasons(
            _pr("org/a", 1, age_min=99, base="release/2"), set(), "me", gh
        )
        assert reasons == ["targets release/2, not the watched base (main)"]

    def test_issue_reasons_use_the_repo_s_own_skip_list(self):
        from backend.web.core import issue_start

        gh = _gh(
            issue_min_age_minutes=0,
            issue_skip_authors=[],
            issue_repo_settings={"org/a": {"skip_authors": ["bot"]}},
        )
        reasons = issue_start.skip_reasons(
            _issue("org/a", 1, age_min=99, author="bot"), set(), gh
        )
        assert reasons == ["authored by bot (in the skip list)"]


# --------------------------------------------------------------------------- #
# 2. Switching a source's Agent CLI applies to the NEXT ticket
# --------------------------------------------------------------------------- #
class TestSourceAgentIsReadFresh:
    """The pipeline loads its config once at boot and the scanners hold that
    snapshot, so stamping ``source.agent`` from it meant a provider switched in
    the UI kept launching the old CLI until the pipeline was restarted."""

    def test_the_on_disk_value_wins_over_the_boot_snapshot(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        fresh = PipelineConfig()
        fresh.github = None
        fresh.engine = EngineConfig(agent="")
        fresh.ticketing_sources = [
            C.TicketProviderConfig(id="jira", provider="jira", agent="claude")
        ]
        monkeypatch.setattr(C, "config_for_launch", lambda _p: fresh)
        assert C.source_agent_now("jira", "codex") == "claude"

    def test_clearing_the_field_clears_it_rather_than_reverting(self, monkeypatch):
        """The half of the bug that a fresh_agent()-style fallback can't fix: an
        on-disk config with no opinion is an ANSWER, not a reason to reuse the
        snapshot."""
        from backend.ticket_ingestion import config as C

        fresh = PipelineConfig()
        fresh.github = None
        fresh.engine = EngineConfig(agent="")
        fresh.ticketing_sources = [
            C.TicketProviderConfig(id="jira", provider="jira", agent="")
        ]
        monkeypatch.setattr(C, "config_for_launch", lambda _p: fresh)
        assert C.source_agent_now("jira", "codex") == ""

    def test_an_unreadable_config_keeps_the_snapshot(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        monkeypatch.setattr(C, "config_for_launch", lambda _p: None)
        assert C.source_agent_now("jira", "codex") == "codex"

    def test_the_scanner_and_the_pending_replay_both_use_it(self):
        import inspect

        from backend.ticket_ingestion import backfill, orchestrator

        assert "source_agent_now(" in inspect.getsource(backfill.BackfillScanner.scan)
        assert "source_agent_now(" in inspect.getsource(
            orchestrator.PipelineOrchestrator
        )

    def test_pr_and_issue_launches_get_the_same_treatment(self):
        """The same staleness, one layer up: `fresh_agent` falls back to the
        construction snapshot when the on-disk chain answers "", which re-applied
        the boot-time provider — so clearing PR review's or issue handling's
        Agent CLI did nothing on a running pipeline either."""
        import inspect

        from backend.ticket_ingestion import orchestrator, session_runner

        for src in (
            inspect.getsource(session_runner.SessionRunner._create_pr_instance),
            inspect.getsource(orchestrator.PipelineOrchestrator._process_issue),
            inspect.getsource(orchestrator.PipelineOrchestrator._process_pr),
        ):
            assert "agent_now(" in src
            assert "fresh_agent(" not in src

    def test_agent_now_is_the_shared_shape(self, monkeypatch):
        from backend.ticket_ingestion import config as C

        fresh = PipelineConfig()
        fresh.github = _gh(agent="")
        fresh.engine = EngineConfig(agent="")
        monkeypatch.setattr(C, "config_for_launch", lambda _p: fresh)
        # An on-disk chain that resolves to "" is an ANSWER, so the snapshot's
        # stale "codex" does not come back.
        assert C.agent_now(lambda c: c.pr_agent("org/a"), "codex") == ""
        # …but an unreadable config still honours it.
        monkeypatch.setattr(C, "config_for_launch", lambda _p: None)
        assert C.agent_now(lambda c: c.pr_agent("org/a"), "codex") == "codex"


# --------------------------------------------------------------------------- #
# 3. Per-start agent override
# --------------------------------------------------------------------------- #
class TestStartAgentOverride:
    def test_absent_means_use_the_configured_chain(self):
        assert server._start_agent_override({}) == ""
        assert server._start_agent_override({"agent": "  "}) == ""

    def test_a_known_provider_is_accepted(self):
        assert server._start_agent_override({"agent": "claude"}) == "claude"

    def test_generic_is_not_a_choice(self):
        """It is the fallback for an arbitrary typed-in program, and
        /api/providers — which fills the picker — omits it too."""
        with pytest.raises(ValueError) as err:
            server._start_agent_override({"agent": "generic"})
        assert "generic" not in str(err.value).split("pick one of:")[1]

    def test_an_unknown_name_is_rejected_rather_than_silently_defaulted(self):
        with pytest.raises(ValueError) as err:
            server._start_agent_override({"agent": "definitely-not-a-cli"})
        assert "unknown agent" in str(err.value)

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/api/tickets/start", {"source": "jira", "id": "1"}),
            ("/api/github/prs/review", {"repo": "org/a", "number": 1}),
            ("/api/github/issues/start", {"repo": "org/a", "number": 1}),
        ],
    )
    def test_every_start_route_rejects_an_unknown_agent(self, path, body):
        r = client.post(path, json={**body, "agent": "definitely-not-a-cli"})
        assert r.status_code == 400, r.text
        assert "unknown agent" in r.json()["error"]

    def test_the_override_outranks_the_repo_card_on_both_github_routes(self):
        import inspect

        src = inspect.getsource(server.github_force_review)
        assert "agent_override" in src
        src = inspect.getsource(server.github_issue_force_start)
        assert "agent_override" in src

    def test_the_ticket_route_stamps_it_onto_the_story(self):
        """`story.agent` is the field every launch path consults first, so
        stamping there is what makes the pick reach the CLI."""
        import inspect

        src = inspect.getsource(server.ticket_force_start)
        assert "story.agent = agent_override" in src


# --------------------------------------------------------------------------- #
# Per-repo GitHub access test endpoint
# --------------------------------------------------------------------------- #
class TestGithubRepoAccessTest:
    def test_a_bad_slug_is_rejected_without_a_network_call(self):
        r = client.post("/api/settings/test/github-repo", json={"repo": "nope"})
        assert r.status_code == 200  # a test result, not a transport failure
        assert r.json() == {"ok": False, "error": "repo must be owner/name"}

    def test_no_token_names_this_screen_not_config_toml(self, monkeypatch):
        """GithubAuthError's own message is a five-line config.toml walkthrough;
        on a repo card, one sentence naming the field above it is more use."""
        from backend.ticket_ingestion import github_auth

        async def _no_token(_cfg):
            raise github_auth.GithubAuthError("No GitHub token available. Either:\n…")

        monkeypatch.setattr(github_auth, "resolve_token", _no_token)
        r = client.post("/api/settings/test/github-repo", json={"repo": "org/a"})
        assert r.json() == {
            "ok": False,
            "error": "no GitHub token available — set one under Advanced options, "
            "or sign in with the gh CLI",
        }

    def test_it_resolves_the_token_afresh(self, monkeypatch):
        """github_auth caches the resolved token for the life of the process, so
        a cached answer would make this button report on the credential the user
        just replaced."""
        from backend.ticket_ingestion import github_auth

        github_auth._cached_token = "stale-token"
        seen: list = []

        async def _resolve(_cfg):
            seen.append(github_auth._cached_token)
            return "fresh-token"

        monkeypatch.setattr(github_auth, "resolve_token", _resolve)
        client.post("/api/settings/test/github-repo", json={"repo": "org/a"})
        assert seen == [None], "the cache must be cleared before resolving"


def test_saving_a_github_token_invalidates_the_cached_one():
    """Every consumer (review, issues, Make PR, the per-repo test) resolves
    through the same process-lifetime cache, so pasting a new token has to clear
    it — otherwise the paste reads as not having been saved until a restart."""
    from backend.ticket_ingestion import github_auth
    from backend.web.addons import settings as settings_addon

    github_auth._cached_token = "old"
    settings_addon._apply_post({"github": {"token": "ghp_new"}})
    assert github_auth._cached_token is None
    # An unrelated github field must not pay for it.
    github_auth._cached_token = "old"
    settings_addon._apply_post({"github": {"min_age_minutes": 5}})
    assert github_auth._cached_token == "old"
    github_auth._cached_token = None


# --------------------------------------------------------------------------- #
# Assigned tickets: a label for every source, including the ones that failed
# --------------------------------------------------------------------------- #
def test_every_configured_source_gets_a_label_even_when_it_fails(monkeypatch):
    """The panel groups by source, so a source that returned nothing (or blew
    up) still needs a heading to say so under — deriving labels from the ticket
    rows alone would make exactly those sources vanish."""
    from backend.web.core import ticket_start

    src_ok = types.SimpleNamespace(
        id="jira", provider="jira", label="Jira – EU", repo_url="", member_id=""
    )
    src_bad = types.SimpleNamespace(
        id="linear", provider="linear", label="", repo_url="", member_id=""
    )
    cfg = types.SimpleNamespace(ticketing_sources=[src_ok, src_bad], repo_url="")
    monkeypatch.setattr(ticket_start, "_load_config", lambda: cfg)

    class _Provider:
        label = "Linear"

        def __init__(self, src):
            self._src = src

        async def search_assigned_all(self):
            if self._src.id == "linear":
                raise RuntimeError("Linear rejected the token (HTTP 401)")
            return []

        async def list_states(self):
            return []

    monkeypatch.setattr(
        "backend.ticket_ingestion.providers.get_provider", lambda s: _Provider(s)
    )
    monkeypatch.setattr(
        "backend.ticket_ingestion.state.load_processed_story_statuses", lambda _r: {}
    )
    monkeypatch.setattr(
        "backend.ticket_ingestion.state.load_processed_story_failures", lambda _r: {}
    )
    monkeypatch.setattr(
        "backend.ticket_ingestion.state.load_pending_stories", lambda _r: []
    )
    out = _run(ticket_start.list_assigned_tickets())
    # The user's own label wins; a blank one falls back to the provider's, and a
    # failed source is still named.
    assert out["source_labels"]["jira"] == "Jira – EU"
    assert out["source_labels"]["linear"] == "linear"
    assert [e["source"] for e in out["errors"]] == ["linear"]


# --------------------------------------------------------------------------- #
# The Intake dialog itself (asserted on the built bundle, like the other frontend
# contracts in this suite)
# --------------------------------------------------------------------------- #
class TestIntakeDialogShell:
    def test_it_is_a_top_bar_surface_of_its_own(self):
        js = client.get("/app.js").text
        assert '"intake-btn"' in js  # the top-bar entry
        assert '"intake-panel"' in js
        assert 'openDialogFor("intake"' in js
        # A blocking modal: its cards carry Remove buttons, so a stray Ctrl+W /
        # Delete must not kill the session behind it instead.
        assert '"intake-dialog"' in js

    def test_the_tab_strip_names_the_three_queues_and_counts_them(self):
        js = client.get("/app.js").text
        for label in ('"Tickets"', '"Pull requests"', '"Issues"'):
            assert label in js, label
        assert '"ik-tab-count"' in js

    def test_the_legacy_settings_screen_keys_still_route(self):
        """Deep links recorded elsewhere — the sidebar bars, the welcome tour,
        and the server's own `settings_screen` on a Connections card — must keep
        working, so the retired keys map to the tab that replaced them."""
        js = client.get("/app.js").text
        assert "LEGACY_SCREEN_TABS" in js
        assert 'repo: "prs"' in js
        assert 'ticketing: "tickets"' in js
        assert 'issues: "issues"' in js

    def test_the_three_screens_left_the_settings_nav(self):
        js = client.get("/app.js").text
        for gone in (
            '{ key: "ticketing", label: "Ticketing"',
            '{ key: "repo", label: "PR review"',
            '{ key: "issues", label: "Git issues"',
        ):
            assert gone not in js, gone

    def test_the_github_tabs_keep_their_capability_gate(self):
        js = client.get("/app.js").text
        css = client.get("/style.css").text
        # Same literal the Settings screens used, now on the work panels.
        assert '"git ticketing"' in js
        assert 'body.no-ticketing .ik-panel[data-caps-need~="ticketing"]' in css

    def test_inbox_lists_group_by_source(self):
        js = client.get("/app.js").text
        assert "ik-source-group" in js
        assert "ik-groups" in js
        # Tickets nest workflow-state buckets INSIDE their source, keyed by the
        # pair — so opening one source's "In Progress" doesn't open another's.
        assert 'src + "::" + b' in js

    def test_states_nest_under_their_workflow_instead_of_repeating_it(self):
        """A Shortcut source with several workflows qualifies every state name
        ("Product Development · Deferred"), so a flat list wrote the workflow onto
        all seven of its headings. The workflow becomes a level of its own and the
        qualifier is written once — which is what scales to many providers."""
        js = client.get("/app.js").text
        assert "ik-workflow-group" in js
        assert "bucket_meta" in js
        # The bucket KEY stays qualified (it has to be unique across workflows);
        # only the heading inside a workflow group drops the qualifier.
        assert "wf === null ? b : labelOf(b)" in js
        # …and the level only appears when there is more than one workflow.
        assert "workflows.filter(Boolean).length > 1" in js

    def test_the_tab_badge_counts_what_the_tab_will_show(self):
        """It counted every ticket the provider ever assigned you — Completed and
        Won't-do included — so it read `1221` over a list of 52. The badge and the
        panel now go through the same bucket filter."""
        js = client.get("/app.js").text
        assert "function countInBuckets(" in js
        assert "function visibleBuckets(" in js
        badge = js.split("function TabCount(", 1)[1][:900]
        assert "countInBuckets(" in badge
        assert "visibleBuckets(" in badge
        assert "loadShownBuckets()" in badge

    def test_done_buckets_are_what_that_filter_excludes(self):
        """The server flags done-type states; the client parks them behind the
        Add menu by default, which is why the two counts diverged at all."""
        js = client.get("/app.js").text
        vis = js.split("function visibleBuckets(", 1)[1][:400]
        assert "doneBuckets.includes(b)" in vis
        assert "shown === null" in vis

    def test_every_list_states_its_source_in_a_heading(self):
        """ "Which provider is this?" is answered once, by the group heading — on
        every tab, however many sources there are. Not by a per-row label repeated
        down the column, which is the thing that does not scale to many
        providers."""
        js = client.get("/app.js").text
        assert "ik-source-group" in js
        # Neither leftover from the intermediate designs: the per-row source
        # label, and the PR/issue tabs' conditional grouping.
        assert "ik-item-source" not in js
        assert "groupOrder.length > 1" not in js
        # `sourceOrder.length > 1` DOES survive, but only to pick the hint's
        # wording (one source names its ingest states; several point at their own
        # headings) — never to decide whether to group.
        assert "ingestStates[sourceOrder[0]]" in js

    def test_the_hint_names_this_flock_s_own_states(self):
        """ "done states like Completed" was a guess about someone else's
        workflow — a Shortcut flock has "Product Development · Won't do", a Jira
        one has "Closed". Both halves of the sentence are rendered from data."""
        js = client.get("/app.js").text
        # The hidden done-type states, by name, and only when some are hidden.
        assert "doneBuckets.filter((b) => !visible.includes(b))" in js
        assert "parkedDone" in js
        assert "like Completed" not in js
        # …and a plural/singular that matches however many there are.
        assert 'parkedDone.length === 1 ? "The done state " : "Done states "' in js

    def test_ingest_states_are_never_merged_across_sources(self):
        """One source watching "Ready" and another watching "Todo" was reported as
        "watches Ready, Todo" — false for both, since neither watched both. With
        several sources the per-source headings carry it instead."""
        js = client.get("/app.js").text
        assert "Object.values(ingestStates).flat()" not in js
        assert "Each source heading says which states" in js
        # The single-source case still names that source's own states.
        assert "ingestStates[sourceOrder[0]]" in js

    def test_group_headings_look_like_something_you_can_press(self):
        """As plain text beside a small caret — under a *bordered* source card —
        the top-level heading read as a stray line of copy, and people did not
        find the tickets under it. Every level now has a hit area, a hover fill
        and a caret that turns; the source additionally gets a filled band."""
        css = client.get("/style.css").text
        assert ".ik-groups .tk-bucket-head:hover" in css
        assert ".ik-source-group > .tk-bucket-head {" in css
        # One glyph rotated by state, not two glyphs swapped.
        assert '.ik-groups [aria-expanded="true"] .tk-caret' in css
        assert "rotate(90deg)" in css
        js = client.get("/app.js").text
        assert 'className: "tk-caret", children: "▸"' in js
        # Keyboard users get the same target.
        assert ".ik-groups .tk-bucket-toggle:focus-visible" in css

    def test_the_tabs_carry_no_redundant_heading(self):
        """The tab strip names the surface; an in-panel "TICKETING" title on top
        of a tab called Tickets was pure repetition."""
        js = client.get("/app.js").text
        for gone in (
            '"set-section-title", children: "Ticketing"',
            '"set-section-title", children: "Automated PR review"',
            '"set-section-title", children: "Automated issue handling"',
        ):
            assert gone not in js, gone

    def test_each_row_can_be_started_on_a_different_cli(self):
        js = client.get("/app.js").text
        assert '"ik-item-agent"' in js
        # Sent only when picked, so an unpicked row keeps the old payload shape.
        assert "agent ? { agent } : {}" in js

    def test_each_row_can_choose_how_far_it_goes(self):
        """The intake half of the autopilot: a per-item depth override that rides
        the same way the per-item CLI override does."""
        js = client.get("/app.js").text
        assert '"ik-item-depth"' in js
        # Same emit-when-picked shape, so an unpicked row's payload is unchanged.
        assert "depth ? { depth } : {}" in js
        # The two per-launch pickers share one line — a third stacked control
        # would make every row in the list taller (see IntakeDialog.css).
        assert '"ik-item-picks"' in js
        css = client.get("/style.css").text
        assert ".ik-item-depth" in css
        assert ".ik-item-picks" in css

    def test_a_long_failure_reason_cannot_widen_the_row(self):
        """A recorded failure reason is a sentence of git output carrying a branch
        name and an absolute worktree path. The chip capped itself at
        `max-width: 100%`, which was circular: the row's `1fr` track had already
        grown to the chip's min-content width (nothing about a nowrap chip can be
        narrower), so 100% *was* the oversized width and every row's button got
        dragged off the right edge.

        Three things have to hold together, so assert all three: the track may be
        narrower than its content, the meta line may be too, and the chip breaks
        rather than setting a floor."""
        css = client.get("/style.css").text
        item = css.split(".pr-open-item {")[1].split("}")[0]
        assert "minmax(0, 1fr) auto" in item

        meta = css.split(".pr-open-meta {")[1].split("}")[0]
        assert "min-width: 0" in meta

        chip = css.split(".pr-open-chip {")[1].split("}")[0]
        assert "overflow-wrap: anywhere" in chip
        # Wrapping, not a one-line clip: the remedy is at the END of the reason.
        assert "white-space: normal" in chip
        assert "text-overflow" not in chip


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _async_value(v):
    async def _inner():
        return v

    return _inner()
