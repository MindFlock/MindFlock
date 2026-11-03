"""The route eval's cases: what a person says, and the rubric the route an
agent builds from it is graded against.

A rubric pattern is ``kind[:lane][!on_fail]`` (``ship:pr``, ``test!agent``);
``a|b`` accepts either. ``must`` is an ordered subsequence the route's stops
must contain; ``must_not`` patterns may appear nowhere; ``saved`` = the route
should be a saved route (``save_flight_plan``) and ``trigger`` its trigger
kind (plus ``at`` / ``days`` for a schedule); ``ask`` = the right answer is a
question (no route at all), ``ask_ok`` = a question is as good as a route.
``reply`` is the person's second message when the agent read the route back
and waited for an OK.
"""

OK = "Yes, that's right — go ahead."

CASES = [
    {
        "id": "terse_pr",
        "style": "terse",
        "text": "fix the flaky login test, PR it",
        "must": ["test", "ship:pr"],
        "must_not": ["ship:merge", "you"],
    },
    {
        "id": "ticket_to_merge_after_ok",
        "style": "rambling",
        "text": (
            "ok so this session has ticket SC-142, honestly it's the CSV export "
            "thing everyone hates. what I want is: you do the work, make sure "
            "the tests actually pass (fix them if they don't), open a PR, wait "
            "for CI, and then I want to look at it myself before anything "
            "merges. once I say OK, merge it and move the ticket to Done."
        ),
        "must": ["test!agent", "ship:pr", "checks", "you", "ship:merge", "ticket"],
        "must_not": [],
    },
    {
        "id": "pr_review_coderabbit",
        "style": "terse",
        "text": (
            "this session is on PR #88. answer whatever coderabbit says until "
            "it goes quiet, and make sure CI ends up green"
        ),
        "must": ["reviews", "checks"],
        "must_not": ["ship:merge"],
    },
    {
        "id": "nightly_schedule",
        "style": "plain",
        "text": (
            "every weeknight at 1am, have an agent bump our dependencies, run "
            "the tests and open a PR. save it so it runs on its own."
        ),
        "must": ["agent", "test", "ship:pr"],
        "must_not": ["ship:merge"],
        "saved": True,
        "trigger": {"kind": "schedule", "at": "01:00", "days": "weekdays"},
    },
    {
        "id": "split_three_workers",
        "style": "plain",
        "text": (
            "this refactor is big: split it into three workers, one for the "
            "API, one for the CLI, one for the docs. wait for all of them, then "
            "run the tests and open one PR"
        ),
        "must": ["split", "join|agent", "test", "ship:pr"],
        "must_not": ["ship:merge"],
        "reply": (
            "It's renaming Client to Session everywhere. The API is src/api/, "
            "the CLI is src/cli/, the docs are docs/. Go ahead."
        ),
    },
    {
        "id": "loop_until_green",
        "style": "rambling",
        "text": (
            "I just want you to keep going at the failing suite until the whole "
            "thing is green, even if it takes a bunch of tries, more than a "
            "couple. then commit it, don't push"
        ),
        "must": ["test", "loop", "ship:commit"],
        "must_not": ["ship:push", "ship:pr", "ship:merge"],
        "alt_must": [["test!agent", "ship:commit"]],
    },
    {
        "id": "verify_before_pr",
        "style": "plain",
        "text": (
            "before you open the PR I want the behaviors checked: you check "
            "what you can, leave the UI ones for me"
        ),
        "must": ["verify", "ship:pr"],
        "must_not": ["ship:merge"],
        "config": {"verify": {"who": "both"}},
    },
    {
        "id": "like_last_time",
        "style": "like last time",
        "text": (
            "do the same route as our saved 'Nightly chores' one on this "
            "session now, but also ping me on my phone when the PR is up"
        ),
        "must": ["agent", "test", "ship:pr", "notify"],
        "must_not": ["ship:merge"],
        "seed_plan": True,
    },
    {
        "id": "bot_pr_merge_green",
        "style": "terse",
        "text": (
            "this is a dependabot PR. just wait for CI and merge it when green, "
            "no need to ask me"
        ),
        "must": ["ship:merge"],
        "must_not": ["you"],
    },
    {
        "id": "test_first",
        "style": "plain",
        "text": (
            "write a failing test for the bug first, then fix it, get a second "
            "agent to review the diff, then open a PR"
        ),
        "must": ["test", "test", "review", "ship:pr"],
        "must_not": ["ship:merge"],
    },
    {
        "id": "release_after_ok",
        "style": "terse",
        "text": "release: notes + version bump, merge after my OK",
        "must": ["ship:pr|checks", "you", "ship:merge"],
        "must_not": [],
    },
    {
        "id": "deploy_unknown",
        "style": "ambiguous",
        "text": "set up a route that deploys it when it's done",
        "ask": True,
        "must": [],
        "must_not": ["call", "preview"],
    },
    {
        "id": "merge_then_pr",
        "style": "should be refused",
        "text": "merge it into main right now and then open a PR for review",
        "ask_ok": True,
        "must": ["ship:pr", "ship:merge"],
        "must_not": [],
    },
]
