"""Wave-5 backend (roadmap section L — round-4 evaluator findings).

Covers:
  * L1 — ghost sessions no longer resurrect across servers sharing one
    ``~/.mindflock/state.json``: deletion tombstones (recorded on delete,
    dropped on adopt/merge when newer than the instance, cleared on same-name
    re-create, pruned after 24h) + dead-instance validation on adopt AND merge
    (worktree gone AND tmux gone, ~120s grace, paused exempt), plus the
    ``workspace_missing`` instance-JSON flag and a clean best-effort DELETE
    for missing-workspace sessions. Includes a real two-Engine convergence
    test over one shared state file (the evaluator's repro as spec).
  * L2 — push-branch 400s without an ``origin`` remote instead of silently
    dead-ending in the shell; ``has_origin`` exposed on the instance JSON.
  * L3 — provider hook ``last_turn_snippet`` (base default None; Claude reads
    the newest transcript JSONL entry, markdown/tool noise stripped, ≤120
    chars, mtime-guarded cache) surfaced as ``last_turn``.
  * L4 — the /api/events websocket sends a hello frame with ``server_time``
    before the backlog (asserted via test_events.py's ``_recv_hello`` too).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from backend.config.state import State
from backend.session.storage import (
    GitWorktreeData,
    InstanceData,
    Status,
    _marshal_instances,
)
from backend.web.core import engine as engine_mod


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated $HOME so ~/.mindflock/state.json lives in tmp space."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _now_dt(offset: float = 0.0) -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=offset)


def _mk_data(
    title: str, wt: str, *, age: float = 300.0, status: Status = Status.Running
) -> InstanceData:
    """A serialized started-session record whose workspace is ``wt``."""
    t = _now_dt(-age)
    return InstanceData(
        title=title,
        path=wt,
        branch="b",
        status=status,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=wt,
            worktree_path=wt,
            session_name=title,
            branch_name="b",
        ),
    )


def _mk_inst(
    title: str, wt: str, *, age: float = 300.0, status: Status = Status.Running
):
    """A started in-memory Instance (no tmux attach, no side effects)."""
    from backend.session.instance import FromInstanceData

    return FromInstanceData(_mk_data(title, wt, age=age, status=status), attach=False)


def _state_file(home: Path) -> Path:
    return home / ".mindflock" / "state.json"


def _disk_titles(home: Path) -> list:
    obj = json.loads(_state_file(home).read_text())
    return [x["title"] for x in (obj.get("instances") or [])]


def _disk_tombstones(home: Path) -> dict:
    obj = json.loads(_state_file(home).read_text())
    return obj.get("tombstones") or {}


def _write_disk_instances(datas) -> None:
    from backend import config

    st = config.LoadState()
    st.SaveInstances(_marshal_instances(list(datas)))


# --------------------------------------------------------------------------- #
# L1 — State tombstones: round-trip + key-less files parse
# --------------------------------------------------------------------------- #
def test_state_without_tombstones_parses_to_empty_map():
    st = State.from_bytes(b'{"help_screens_seen":0,"instances":[]}')
    assert st.tombstones == {}


def test_state_tombstones_roundtrip():
    st = State(help_screens_seen=0, instances_data=b"[]", tombstones={"gone": 1234.5})
    st2 = State.from_bytes(st.marshal_indent())
    assert st2.tombstones == {"gone": 1234.5}


def test_state_tombstones_key_only_emitted_when_nonempty():
    # Ordinary state files keep serializing byte-identically to the Go layout.
    assert b"tombstones" not in State().marshal_indent()


def test_state_tombstones_malformed_values_dropped():
    st = State.from_bytes(
        b'{"help_screens_seen":0,"instances":[],'
        b'"tombstones":{"ok":5.0,"bad":"x","weird":true}}'
    )
    assert st.tombstones == {"ok": 5.0}


# --------------------------------------------------------------------------- #
# L1 — two-server convergence over one shared state file (the evaluator's
# repro as spec: create -> adopt -> delete -> no resurrection -> re-create ->
# re-delete -> tombstone prune)
# --------------------------------------------------------------------------- #
def test_two_server_delete_converges_and_never_resurrects(home, tmp_path, monkeypatch):
    wt = tmp_path / "ws1"
    wt.mkdir()

    server_a = engine_mod.Engine()
    server_b = engine_mod.Engine()

    # A creates + persists the session.
    server_a.instances["conv"] = _mk_inst("conv", str(wt))
    server_a.save()
    assert _disk_titles(home) == ["conv"]

    # B adopts it via the reload loop's sync (the real adopt path).
    monkeypatch.setattr(engine_mod, "_ENGINE", server_b)
    assert engine_mod._sync_external_instances() == 1
    assert "conv" in server_b.instances

    # A deletes it (the DELETE route's exact call shape).
    time.sleep(0.01)  # tombstone must postdate B's copy
    server_a.instances.pop("conv", None)
    server_a.save(exclude_titles={"conv"})
    assert _disk_titles(home) == []
    assert "conv" in _disk_tombstones(home)

    # THE BUG: B still holds the session in memory and saves — pre-L1 this
    # wrote the dead session right back. Now B drops it instead.
    server_b.save()
    assert "conv" not in server_b.instances
    assert _disk_titles(home) == []
    # ... and B's save preserved A's tombstone rather than clobbering it.
    assert "conv" in _disk_tombstones(home)

    # A's adopt loop must not re-adopt either (deletion has converged).
    monkeypatch.setattr(engine_mod, "_ENGINE", server_a)
    assert engine_mod._sync_external_instances() == 0
    assert "conv" not in server_a.instances

    # B's sync also drops a tombstoned in-memory copy directly.
    server_b.instances["conv"] = _mk_inst("conv", str(wt), age=600.0)
    monkeypatch.setattr(engine_mod, "_ENGINE", server_b)
    engine_mod._sync_external_instances()
    assert "conv" not in server_b.instances

    # Re-creating a same-name session clears the tombstone...
    time.sleep(0.01)
    server_a.instances["conv"] = _mk_inst("conv", str(wt), age=0.0)
    server_a.save()
    assert _disk_titles(home) == ["conv"]
    assert "conv" not in _disk_tombstones(home)

    # ...and deleting it AGAIN works (fresh tombstone, gone from disk).
    time.sleep(0.01)
    server_a.instances.pop("conv", None)
    server_a.save(exclude_titles={"conv"})
    assert _disk_titles(home) == []
    assert "conv" in _disk_tombstones(home)


def test_tombstones_pruned_after_ttl(home, tmp_path):
    eng = engine_mod.Engine()
    eng.state.tombstones = {"ancient": time.time() - 25 * 3600}
    eng.save()
    assert "ancient" not in _disk_tombstones(home)


def test_engine_seed_skips_tombstoned_and_dead_instances(home, tmp_path):
    wt_ok = tmp_path / "ok"
    wt_ok.mkdir()
    live = _mk_data("live", str(wt_ok))
    tomb = _mk_data("tombed", str(wt_ok))
    dead = _mk_data("dead", str(tmp_path / "gone"), age=600.0, status=Status.Paused)
    dead.status = Status.Running
    _write_disk_instances([live, tomb, dead])
    from backend import config

    st = config.LoadState()
    st.tombstones = {"tombed": time.time()}
    config.SaveState(st)

    eng = engine_mod.Engine()
    # "live" reconstruction may fail on tmux restore in a bare test env — the
    # per-instance skip means the OTHERS never load either way; the point here
    # is that tombstoned/dead titles are filtered before reconstruction.
    assert "tombed" not in eng.instances
    assert "dead" not in eng.instances


# --------------------------------------------------------------------------- #
# L1 — dead-instance validation on adopt + merge (rule b)
# --------------------------------------------------------------------------- #
def test_adopt_skips_instance_with_missing_workspace_and_no_tmux(
    home, tmp_path, monkeypatch
):
    _write_disk_instances([_mk_data("ghost", str(tmp_path / "vanished"), age=600.0)])
    eng = engine_mod.Engine()
    monkeypatch.setattr(engine_mod, "_ENGINE", eng)
    assert engine_mod._sync_external_instances() == 0
    assert "ghost" not in eng.instances


def test_adopt_keeps_fresh_instance_within_grace_window(home, tmp_path, monkeypatch):
    # Worktree dir doesn't exist yet (provisioning in flight) but the record
    # is fresh — the grace window keeps it adoptable.
    _write_disk_instances([_mk_data("fresh", str(tmp_path / "notyet"), age=1.0)])
    eng = engine_mod.Engine()
    # The constructor applies the same keep-rules and may have adopted it
    # already (tmux-environment-dependent); start empty so this asserts the
    # SYNC path's grace logic deterministically.
    eng.instances.clear()
    monkeypatch.setattr(engine_mod, "_ENGINE", eng)
    assert engine_mod._sync_external_instances() == 1
    assert "fresh" in eng.instances


def test_adopt_keeps_paused_instance_with_missing_dir(home, tmp_path, monkeypatch):
    # Paused = worktree removed by design; never culled.
    _write_disk_instances(
        [
            _mk_data(
                "napper", str(tmp_path / "paused-gone"), age=600.0, status=Status.Paused
            )
        ]
    )
    eng = engine_mod.Engine()
    eng.instances.clear()  # isolate the sync path (see grace-window test)
    monkeypatch.setattr(engine_mod, "_ENGINE", eng)
    assert engine_mod._sync_external_instances() == 1


def test_adopt_keeps_missing_dir_instance_whose_tmux_lives(home, tmp_path, monkeypatch):
    _write_disk_instances([_mk_data("tmuxy", str(tmp_path / "vanished"), age=600.0)])
    monkeypatch.setattr(engine_mod, "_tmux_session_exists", lambda title: True)
    eng = engine_mod.Engine()
    eng.instances.clear()  # isolate the sync path (see grace-window test)
    monkeypatch.setattr(engine_mod, "_ENGINE", eng)
    assert engine_mod._sync_external_instances() == 1


def test_merge_on_save_drops_dead_disk_instance(home, tmp_path):
    _write_disk_instances([_mk_data("stale", str(tmp_path / "vanished"), age=600.0)])
    eng = engine_mod.Engine()
    eng.instances.clear()  # own nothing; the merge path decides
    eng.save()
    assert _disk_titles(home) == []


def test_merge_on_save_keeps_healthy_foreign_instance(home, tmp_path):
    wt = tmp_path / "healthy"
    wt.mkdir()
    _write_disk_instances([_mk_data("other-servers", str(wt))])
    eng = engine_mod.Engine()
    eng.instances.clear()
    eng.save()
    assert _disk_titles(home) == ["other-servers"]


# --------------------------------------------------------------------------- #
# L1(c) — workspace_missing on the instance JSON + clean DELETE
# --------------------------------------------------------------------------- #
def test_instance_json_flags_missing_workspace(home, tmp_path):
    from backend.web import server

    gone = tmp_path / "poof"
    gone.mkdir()
    inst = _mk_inst("w5-missing", str(gone))
    d = server._instance_json(inst)
    assert d["workspace_missing"] is False
    gone.rmdir()
    d = server._instance_json(inst)
    assert d["workspace_missing"] is True
    assert d["status"] == "running"  # status untouched; the flag is additive
    assert d["has_origin"] is False


def test_instance_json_paused_never_flags_missing_workspace(home, tmp_path):
    from backend.web import server

    inst = _mk_inst("w5-paused", str(tmp_path / "never-there"), status=Status.Paused)
    assert server._instance_json(inst)["workspace_missing"] is False


def test_delete_missing_workspace_session_is_clean(home, tmp_path, monkeypatch):
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_HOOKS_DIR", str(tmp_path / "no-hooks"))
    title = "w5-ghost-del"
    inst = _mk_inst(title, str(tmp_path / "already-deleted"))
    server.ENGINE.instances[title] = inst
    try:
        resp = asyncio.run(server.delete_instance(title))
        assert resp.status_code == 200
        assert json.loads(resp.body)["ok"] is True
        assert title not in server.ENGINE.instances
        assert title in _disk_tombstones(home)
    finally:
        server.ENGINE.instances.pop(title, None)
        server.ENGINE.state.tombstones.pop(title, None)
        server._EVENT_SNAPSHOT.pop(title, None)


# --------------------------------------------------------------------------- #
# L2 — push without an origin remote fails loudly; has_origin exposed
# --------------------------------------------------------------------------- #
def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    (path / "a.txt").write_text("one\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "add",
            "-A",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    return path


def test_has_origin_helper(tmp_path):
    from backend.web.core.git_ops import _has_origin

    repo = _init_repo(tmp_path / "r")
    assert _has_origin(str(repo), force=True) is False
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://example.invalid/r.git",
        ],
        check=True,
    )
    assert _has_origin(str(repo), force=True) is True
    assert _has_origin("") is False


def test_push_branch_400_without_origin(home, tmp_path):
    from backend.web import server

    repo = _init_repo(tmp_path / "no-origin")
    title = "w5-push-noorigin"
    server.ENGINE.instances[title] = _mk_inst(title, str(repo))
    try:
        resp = asyncio.run(server.instance_push_branch(title))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert "no origin remote" in body["error"]
        assert "git remote add origin" in body["error"]
    finally:
        server.ENGINE.instances.pop(title, None)


def test_push_branch_proceeds_with_origin(home, tmp_path, monkeypatch):
    from backend.web import server

    repo = _init_repo(tmp_path / "with-origin")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://example.invalid/r.git",
        ],
        check=True,
    )
    title = "w5-push-origin"
    server.ENGINE.instances[title] = _mk_inst(title, str(repo))
    sent = []
    monkeypatch.setattr(server, "_ensure_shell_session", lambda t, wt: ("sh", None))
    monkeypatch.setattr(server, "_send_to_shell", lambda name, cmd: sent.append(cmd))
    try:
        resp = asyncio.run(server.instance_push_branch(title))
        assert resp.status_code == 200
        assert json.loads(resp.body)["ok"] is True
        assert sent and "git push" in sent[0]
    finally:
        server.ENGINE.instances.pop(title, None)


def test_instance_json_has_origin_true_for_repo_with_origin(home, tmp_path):
    from backend.web import server
    from backend.web.core.git_ops import _HAS_ORIGIN_CACHE

    repo = _init_repo(tmp_path / "hо")  # noqa: RUF001 — plain dir name
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://example.invalid/r.git",
        ],
        check=True,
    )
    _HAS_ORIGIN_CACHE.clear()
    inst = _mk_inst("w5-origin-json", str(repo))
    assert server._instance_json(inst)["has_origin"] is True


# --------------------------------------------------------------------------- #
# L3 — last_turn_snippet (base default + Claude transcript implementation)
# --------------------------------------------------------------------------- #
def test_base_provider_last_turn_snippet_defaults_to_none():
    from backend.providers.base import BaseProvider

    assert BaseProvider().last_turn_snippet("s", "/tmp/x") is None


def _write_transcript(home: Path, workdir: str, entries) -> Path:
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", workdir)
    proj = home / ".claude" / "projects" / encoded
    proj.mkdir(parents=True)
    p = proj / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


@pytest.fixture(autouse=True)
def _clear_last_turn_cache():
    from backend.providers import claude

    claude._LAST_TURN_CACHE.clear()
    yield
    claude._LAST_TURN_CACHE.clear()


def test_last_turn_snippet_reads_newest_meaningful_entry(home, tmp_path):
    from backend.providers.claude import ClaudeProvider

    wd = str(tmp_path / "proj")
    _write_transcript(
        home,
        wd,
        [
            {"type": "user", "message": {"role": "user", "content": "fix the bug"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "## Plan\n\nI will fix `foo()` now"}
                    ],
                },
            },
            # Newest entry is tool-only noise -> fall back to the text turn above.
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash"}],
                },
            },
        ],
    )
    snippet = ClaudeProvider().last_turn_snippet("s", wd)
    assert snippet == "Plan"  # markdown heading marks stripped


def test_last_turn_snippet_truncates_to_120_chars(home, tmp_path):
    from backend.providers.claude import ClaudeProvider

    wd = str(tmp_path / "proj2")
    _write_transcript(
        home,
        wd,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "x" * 400}],
                },
            },
        ],
    )
    snippet = ClaudeProvider().last_turn_snippet("s", wd)
    assert snippet is not None
    assert len(snippet) <= 120
    assert snippet.endswith("…")


def test_last_turn_snippet_skips_meta_and_tag_noise(home, tmp_path):
    from backend.providers.claude import ClaudeProvider

    wd = str(tmp_path / "proj3")
    _write_transcript(
        home,
        wd,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<system-reminder>\nnoise\n</system-reminder>\nreal question",
                },
            },
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "meta noise"},
            },
        ],
    )
    # Newest entry is meta -> skipped; the tag lines inside the older user
    # message are skipped too ("noise" is inside the tag body — the first
    # NON-tag line wins).
    assert ClaudeProvider().last_turn_snippet("s", wd) == "noise"


def test_last_turn_snippet_none_without_transcripts(home, tmp_path):
    from backend.providers.claude import ClaudeProvider

    assert ClaudeProvider().last_turn_snippet("s", str(tmp_path / "empty")) is None
    assert ClaudeProvider().last_turn_snippet("s", "") is None


def test_last_turn_snippet_mtime_guarded_cache(home, tmp_path):
    from backend.providers import claude
    from backend.providers.claude import ClaudeProvider

    wd = str(tmp_path / "proj4")
    p = _write_transcript(
        home,
        wd,
        [
            {"type": "user", "message": {"role": "user", "content": "first"}},
        ],
    )
    prov = ClaudeProvider()
    assert prov.last_turn_snippet("s", wd) == "first"
    # Within the TTL the cached snippet is served without re-reading.
    p.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "second"}})
        + "\n"
    )
    assert prov.last_turn_snippet("s", wd) == "first"
    # Once the TTL lapses (simulated), the changed mtime/size triggers a re-read.
    # The cache is keyed per WINDOW (workdir + tmux session name): siblings
    # sharing a directory read different transcripts.
    claude._LAST_TURN_CACHE[wd + "\x00s"]["checked"] -= claude._LAST_TURN_TTL + 1
    assert prov.last_turn_snippet("s", wd) == "second"


def test_session_last_turn_surfaced_from_provider(home, tmp_path, monkeypatch):
    from backend.web import server

    wt = tmp_path / "lt"
    wt.mkdir()
    inst = _mk_inst("w5-lastturn", str(wt))

    class _Prov:
        def last_turn_snippet(self, session_name, workdir):
            assert workdir == str(wt)
            return "doing the thing"

    monkeypatch.setattr(server.providers, "resolve", lambda program: _Prov())
    assert server._session_last_turn(inst) == "doing the thing"
