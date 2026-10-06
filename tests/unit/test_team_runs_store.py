"""The team-run store and the goal parser (``backend.web.core.team_runs``).

The store is one JSON file per run under ``$MINDFLOCK_RUNS_DIR`` (conftest
points it at tmp), written atomically, with a lease file so two servers never
drive one run. The parser turns a pasted goal into ticket refs and task lines
the same way every time, so the preview shows exactly what Start does.
"""

from __future__ import annotations

import json
import os

import pytest

from backend.web.core import team_runs as tr


def _fields(**kw):
    base = {
        "name": "Q4 payments",
        "policy": {"lane": "pr"},
        "tasks": [
            {"kind": "task", "text": "Per-user rate limit", "title": "rate-limit"},
            {
                "kind": "ticket",
                "source": "jira",
                "ticket_id": "PAY-1",
                "title": "jira-PAY-1",
            },
        ],
    }
    base.update(kw)
    return base


class TestStore:
    def test_create_assigns_ids_and_round_trips(self):
        run = tr.create(_fields(), now=1000.0)
        assert tr.valid_id(run["id"])
        assert [t["id"] for t in run["tasks"]] == ["t1", "t2"]
        assert all(t["state"] == "queued" for t in run["tasks"])
        again = tr.load(run["id"])
        assert again["name"] == "Q4 payments" and again["rev"] == 1
        assert again["policy"] == {
            "lane": "pr",
            "ask_first": False,
            "grouping": "each",
            "release": "ask",
        }

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        run = tr.create(_fields())
        for _ in range(3):
            with tr.edit(run["id"]) as r:
                r["name"] = "renamed"
        names = os.listdir(tr.runs_dir())
        assert not [n for n in names if n.endswith(".tmp")]
        with open(os.path.join(tr.runs_dir(), run["id"] + ".json")) as f:
            on_disk = json.load(f)
        # Only the first edit changed anything, so only it was written.
        assert on_disk["name"] == "renamed" and on_disk["rev"] == 2

    def test_a_mangled_file_cannot_crash_a_pass(self):
        run = tr.create(_fields())
        path = os.path.join(tr.runs_dir(), run["id"] + ".json")
        with open(path, "w") as f:
            json.dump(
                {
                    "id": run["id"],
                    "concurrency": "lots",
                    "tasks": [{"state": "teleporting", "attempts": "x"}],
                    "policy": {"lane": "yolo"},
                },
                f,
            )
        got = tr.load(run["id"])
        assert got["concurrency"] == 3 and got["policy"]["lane"] == "pr"
        assert got["tasks"][0]["state"] == "queued"
        assert got["tasks"][0]["attempts"] == {"create": 0, "ship": 0}

    def test_an_unknown_or_malformed_id_loads_nothing(self):
        assert tr.load("r_nothere") is None
        assert tr.load("../../etc/passwd") is None

    def test_the_lease_keeps_a_second_server_reading_only(self):
        run = tr.create(_fields())
        assert tr.claim_lease(run["id"], "server-a", now=1000.0)
        assert not tr.claim_lease(run["id"], "server-b", now=1010.0)
        assert tr.claim_lease(run["id"], "server-a", now=1020.0)  # refresh

    def test_a_stale_lease_is_taken_over(self):
        run = tr.create(_fields())
        assert tr.claim_lease(run["id"], "crashed", now=1000.0)
        assert tr.claim_lease(run["id"], "server-b", now=1000.0 + tr.LEASE_STALE_S + 1)
        assert tr.lease_holder(run["id"]) == "server-b"

    def test_title_index_lists_members_not_queued_or_detached(self):
        run = tr.create(_fields())
        with tr.edit(run["id"]) as r:
            r["tasks"][0]["state"] = "working"
            r["tasks"][1]["state"] = "skipped"
        idx = tr.title_index(now=10**10)
        assert idx["rate-limit"] == {
            "id": run["id"],
            "name": "Q4 payments",
            "task": "t1",
            "role": "task",
            "grouping": "each",
            "lane": "pr",
            "ask_first": False,
            # The member's incarnation: a later namesake is not this member.
            "incarnation": 0.0,
        }
        assert "jira-PAY-1" not in idx

    def test_owner_of_title_ignores_terminal_tasks(self):
        run = tr.create(_fields())
        with tr.edit(run["id"]) as r:
            r["tasks"][0]["state"] = "working"
        assert tr.owner_of_title("rate-limit")[0]["id"] == run["id"]
        assert tr.owner_of_title("rate-limit", exclude_run=run["id"]) is None
        with tr.edit(run["id"]) as r:
            r["tasks"][0]["state"] = "shipped"
        assert tr.owner_of_title("rate-limit") is None

    def test_finished_runs_age_into_the_archive(self):
        run = tr.create(_fields())
        with tr.edit(run["id"]) as r:
            r["state"] = "done"
            r["finished_at"] = 1000.0
        assert tr.archive_old(now=1000.0 + 3600) == 0
        assert tr.archive_old(now=1000.0 + tr.KEEP_FINISHED_S + 1) == 1
        assert tr.load(run["id"]) is None
        assert os.path.exists(
            os.path.join(tr.runs_dir(), "archive", run["id"] + ".json")
        )

    def test_dto_hides_bookkeeping_and_adds_counts(self):
        run = tr.create(_fields())
        dto = tr.run_dto(tr.load(run["id"]), live_titles={"rate-limit"})
        assert "v" not in dto and "announced" not in dto
        assert dto["counts"] == {
            "queued": 2,
            "active": 0,
            "needs_you": 0,
            "shipped": 0,
            "failed": 0,
            "total": 2,
        }
        assert dto["tasks"][0]["row_present"] is True
        assert "progress" not in dto["tasks"][0] and "nudge_id" not in dto["tasks"][0]
        summary = tr.summary_dto(tr.load(run["id"]))
        assert set(summary) == {
            "id",
            "name",
            "state",
            "paused",
            "pause_reason",
            "policy",
            "counts",
            "cost_usd",
            "created_at",
        }


class TestParse:
    def test_a_line_of_ticket_ids_is_that_many_tickets(self):
        items = tr.parse_items("PAY-412 PAY-415, sc-9;  ENG-1\n")
        assert items == [
            {"kind": "ticket", "ref": "PAY-412"},
            {"kind": "ticket", "ref": "PAY-415"},
            {"kind": "ticket", "ref": "sc-9"},
            {"kind": "ticket", "ref": "ENG-1"},
        ]

    def test_a_sentence_mentioning_a_ticket_stays_a_task(self):
        assert tr.parse_items("Fix PAY-412 rounding") == [
            {"kind": "task", "text": "Fix PAY-412 rounding"}
        ]

    def test_bullets_numbers_blank_lines_and_repeats(self):
        text = "- Per-user rate limit on /webhooks\n\n2) Dark mode\n* PAY-1\nPAY-1\n"
        assert tr.parse_items(text) == [
            {"kind": "task", "text": "Per-user rate limit on /webhooks"},
            {"kind": "task", "text": "Dark mode"},
            {"kind": "ticket", "ref": "PAY-1"},
        ]

    def test_urls_and_github_issue_refs(self):
        items = tr.parse_items("https://x.atlassian.net/browse/PAY-3 org/repo#12 #7")
        assert [i["ref"] for i in items] == [
            "https://x.atlassian.net/browse/PAY-3",
            "org/repo#12",
            "#7",
        ]

    def test_match_ticket_by_slug_id_session_or_url(self):
        rows = [
            {
                "source": "jira",
                "id": "PAY-1",
                "slug": "jira-PAY-1",
                "session": "jira-PAY-1",
                "url": "https://j/PAY-1",
            },
            {
                "source": "sc",
                "id": "9",
                "slug": "sc-9",
                "session": "sc-9",
                "url": "https://s/9",
            },
        ]
        for ref in ("PAY-1", "jira-pay-1", "https://j/PAY-1/"):
            row, err = tr.match_ticket(ref, rows)
            assert row["source"] == "jira" and err == "", ref
        assert tr.match_ticket("sc-9", rows)[0]["id"] == "9"
        assert tr.match_ticket("NOPE-1", rows) == (None, "")

    def test_an_ambiguous_ref_names_the_sources(self):
        rows = [
            {"source": "jira-a", "id": "X-1", "slug": "a-X-1"},
            {"source": "jira-b", "id": "X-1", "slug": "b-X-1"},
        ]
        row, err = tr.match_ticket("X-1", rows)
        assert row is None and "jira-a, jira-b" in err

    def test_task_titles_are_short_readable_and_unique(self):
        assert (
            tr.task_title("Per-user rate limit on /webhooks")
            == "per-user-rate-limit-webhooks"
        )
        assert tr.task_title("Dark mode", taken={"dark-mode"}) == "dark-mode-2"
        assert tr.task_title("!!!") == "task"
        assert len(tr.task_title("word " * 40)) <= 32

    @pytest.mark.parametrize(
        "items, name",
        [
            (
                [
                    {"kind": "ticket", "ref": "PAY-1"},
                    {"kind": "ticket", "ref": "PAY-2"},
                ],
                "PAY tickets",
            ),
            (
                [{"kind": "task", "text": "Dark mode for settings page please"}],
                "Dark mode for settings…",
            ),
            (
                [
                    {"kind": "task", "text": "Dark mode"},
                    {"kind": "ticket", "ref": "PAY-1"},
                ],
                "Dark mode + 1 more",
            ),
        ],
    )
    def test_name_suggestion(self, items, name):
        assert tr.name_suggestion(items) == name

    def test_the_brief_fits_and_names_the_lane(self):
        run = tr._normalize(_fields(tasks=[{}, {}, {}]))
        brief = tr.run_brief(run)
        assert len(brief) <= 600
        assert '"Q4 payments"' in brief and "one of 3" in brief
        assert "opens a pull request" in brief
        assert "Do not push" in brief


def test_an_edit_that_changes_nothing_writes_nothing():
    """``rev`` is what a long-poll for "change" waits on: an idle pass must
    not move it."""
    run = tr.create(_fields())
    with tr.edit(run["id"]) as r:
        pass
    with tr.edit(run["id"]) as r:
        r["name"] = r["name"]
    assert tr.load(run["id"])["rev"] == 1
    with tr.edit(run["id"]) as r:
        r["name"] = "other"
    assert tr.load(run["id"])["rev"] == 2
