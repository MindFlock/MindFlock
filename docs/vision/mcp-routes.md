# MCP routes: from a person's sentence to the right route (vz-mcp-routes, 2026-11-03)

The question: can an agent with the MindFlock MCP take a person's plain
description of a route ("fix it, test it, PR it, merge after my OK") and set
up the right route on a session, picking the right stops for the situation?
This page holds the audit of the agent's path, what changed, and a real eval:
13 person-style descriptions, run by a headless Claude agent against a sandbox
server.

Words follow [vocabulary.md](../vocabulary.md). The MCP tool names
(`describe_flight`, `start_flight` …) and the `/api/flights/*` paths are
internal names and stay as they are.

## 1. Audit: the agent's path before this change

What an agent does with *"fix the flaky login test, PR it"*:

1. **It reads the server instructions.** The only line about routes was
   `Routes: describe_flight → start_flight; flight_status; …`. There was
   nothing on when to use which stop. `INSTRUCTIONS` is pinned at 2000
   characters and was already at 1999.
2. **`describe_flight(text)`** sends the sentence to the server's one-shot
   model (`POST /api/flights/describe` → `core/flight_describe.py`). Its
   prompt listed each kind's summary and fields, and nothing about when a
   kind fits, the order of a route, or when to ask instead. The tool returned
   `steps`, `lines`, and `kinds` (one line per kind: label, a 110-character
   summary, field names).
   - **Where it guessed wrong:** nothing said that `ship lane=merge`
     already pushes, opens the PR and waits for green, so `Open PR → Merge` vs
     `Merge` alone was a coin toss. Nothing said that a Split needs a Join, or
     that a Loop goes after the stops it repeats. "After my OK" had no stated
     mapping to a `you` stop, and the agent was not told where
     `on_fail: agent` helps.
   - **The rare kinds looked as good as the common ones.** `screenshot`,
     `listen`, `call`, `budget` and `checkpoint` sat in the same list with
     the same weight as `test` and `checks`. An agent that sees "deploy" can
     reach for `call` or `preview` and invent a URL.
   - **The UI box and the agent could disagree.** The two descriptions came
     from different places: the prompt for the box, the kinds list for the
     agent.
3. **`start_flight(steps)`**. The only validator was `flights.normalize_steps`
   (shape: kind, lane, `when` grammar, a Finish last). It accepted
   **semantically broken routes** with no word:
   - a Join of `spawned` with no Split (it waits for nobody);
   - Open PR after Merge when green;
   - a Loop `back_to` a later or unknown stop;
   - Verify with nothing before it;
   - a Wait for me with no text;
   - a Loop with `times: 0` and no `until` (it never ends);
   - `when: judge is yes` with no Judge before it;
   - Reviews settle or CI checks with no PR.

   A server refusal came back as `bad_step` plus its `field`, with no hint
   about what to write instead.
4. **There was no dry run.** The agent could not read the route back to the
   person before it started. `lines` was per-stop and machine-shaped.
5. **On its own session, the route runs ahead of the work.** "On an existing
   session the route starts at the first non-agent stop" is right when the
   work is done. An agent that sets the route up first and then does the
   work instead gets a route that runs the tests and opens a PR with no fix
   in it. The eval found this on its first run. The fixes already existed on
   the server (`start_at`, round 19's `start_on: prompt|run`), but the MCP
   did not expose `start_on`, and nothing said when to use either.
6. **A merge on yourself is refused at start, not up front.** Under the
   default `children` scope a session never merges ITSELF
   (`Policy.require_ship`). The agent found out only at the real start, after
   it had read the route back and got the person's OK, so it had to ask a
   second time. The refusal pointed at `grounded=true` without saying that
   this is harmless and needs no second ask. (The eval hit this on 4 of 13
   cases.)
7. **Saved routes were invisible.** No MCP tool lists the shelf, and
   `plan_id` took only an id, so "like the Nightly chores one, but also…"
   was impossible. The agent tried the name as an id, got an empty plan, and
   had to ask.

## 2. What changed

**One cookbook, both paths.** [`backend/web/core/flight_cookbook.py`](../../backend/web/core/flight_cookbook.py)
is pure data plus helpers. It imports only the stdlib: the MCP imports it,
and `flights` would pull FastAPI into every session's MCP process. It holds:

- `STOPS`: one entry per kind (all 26): `use`, `avoid` (when not to, and what
  to use instead), typical `fields`, what it `waits` on, `fixable`
  (`on_fail: agent` makes sense) and `rare`. The rare kinds (screenshot,
  listen, call, budget, checkpoint, handoff, someone, preview) say plainly
  "almost always: not …" or "never invent a URL / command".
- `RECIPES`: 10 common situations, each a whole route that the real
  validator accepts. They cover ticket → PR with tests; ticket → merge after
  my OK; PR review answering CodeRabbit + CI; nightly chores on a schedule;
  split into workers + join; loop the agent until the tests pass; verify
  behaviors before the PR; a bot PR that merges when green; a release; and
  test-first.
- `RULES`: the order of a route, one ship stop per rising rung, where
  `on_fail: agent` helps, "after my OK" = Wait for me, and when to ask.
- `lint(steps)`: the semantic mistakes above. Each is an `error` (refused)
  or a `warn` (odd, so the agent should say it to the person), and each says
  what to do instead.
- `sentence(steps)`: the route read back in one line: `Agent: fix the login
  bug → Test (agent fixes failures ×2) → Open PR → waits for you: approve the
  merge → Merge when green`.
- `notation(steps)`: a terse, exact form an agent can copy:
  `agent(prompt="{ticket}") → test!agent → ship(lane="pr")`.

**The MCP (`backend/mcp/flights.py`).**

- `describe_flight` returns `sentence`, `problems`/`warnings`, and the
  cookbook in compact form: `stops`, `recipes`, `rules`, plus `saved` (the
  user's saved routes). The answer stays under `DESCRIBE_BUDGET` (12 000
  JSON characters, pinned by a test; about 9 300 today). Lines shrink when
  needed, and kinds never drop. A kind newer than the cookbook falls back to
  the server's own line.
- `start_flight(dry_run=true)` starts nothing. It returns the sentence,
  problems, warnings, `needs_confirm_merge`, `merge_blocked` (you may not
  merge yourself: do it grounded) and `next`.
- `start_flight`, `save_flight_plan` and `edit_flight` refuse a route that
  can't work, before anything is posted, with the fix in the message. A
  started route carries `sentence` and `warnings`. A server `bad_step` gets
  the cookbook's hint for its field.
- `start_flight(start_on="prompt"|"run")` exposes round 19's ready routes.
  The description says when to use `at='s1'` vs `start_on` on your own
  session.
- `plan_id` takes a saved route's name. An unknown one is refused with the
  shelf's names.
- The self-merge refusal and `merge_blocked` say: "Start the same steps with
  grounded=true (and confirm_merge=true, a name) instead — no need to ask
  again: they wait on your user's Sky until they attach this session, and
  that attach is their OK for the merge."
- The server instructions line is now `Routes: describe_flight →
  start_flight (dry_run first); …`. It stays at 1997 of 2000 characters.

**The Sky's box (`core/flight_describe.py`).** The one-shot prompt carries
`flight_cookbook.prompt_text()` (stops, rules, recipes as JSON), the same
words the agent gets. Its answer also returns `sentence` and `warnings`
beside `steps`. The frontend ignores keys it doesn't know, so nothing breaks
there; showing the sentence under the box is a frontend follow-up.

**Not done here (outside this fence).**

- A server-side `POST /api/flights/check` (normalise + lint with no side
  effects) would let the dry run use the server's own validator as well. The
  MCP's lint covers the shape checks the server makes (kind, lane, required
  command, prompts, URL) plus the semantic ones, but `when` grammar errors
  still surface only at the real start. That route would go in
  `routes/flights.py`, which is outside this fence.
- **Policy question for the owner:** under `children` scope an agent can
  never put a merge on its own route. It goes grounded, and the person
  attaches it. A relaxation would let a session run a merge that sits after
  a Wait for me on its own route. To be safe, that relaxation also needs
  `control_flight done/skip`, `edit_flight remove` and `jump` refused on
  that guarding stop for the agent. The trade-off is fewer clicks against a
  weaker guard. That is the owner's call, so it was left alone.

## 3. The eval

`scripts/route_eval/run_eval.py` runs each case in `scripts/route_eval/cases.py`
like this:

1. It boots sandbox servers (`tests/e2e/_server.SandboxServer` plus the
   ux-lab `LabServer` cut-offs). Each has a fresh HOME, its own tmux socket
   dir with `$TMUX` unset, a failing `tailscale` stub, no tailnet status
   file, `CS_CURSOR_AUTOADOPT=0`, the peer home and TMPDIR in scratch, and a
   free port. It is never `:8765`. There is one server per job, so a saved
   route is graded on its own shelf.
2. It creates a session (a stub agent) for the case.
3. It runs `claude -p` (Sonnet) with **only** the MindFlock MCP attached
   (`--strict-mcp-config`, the env that `providers/mcp_attach.py` gives a
   launch: `MINDFLOCK_PORT`, `MINDFLOCK_MCP_SCOPE=children`,
   `MINDFLOCK_MCP_MANAGED=1`, `MINDFLOCK_SESSION_TITLE=<the session>`). It
   loads no settings sources, so none of the owner's hooks run, and Bash,
   Edit and Write are disallowed. The agent gets the person's words, and is
   told only to set up the route and to ask if something is missing.
4. If the agent read the route back and waited, the harness sends the
   person's OK (`--resume`). A case where asking is fine is graded on the
   first answer.
5. It reads what was built: `GET /api/flights?all=1` (the session's routes,
   or a grounded one `by: agent:<it>`) and new saved routes. It grades that
   against the rubric: an ordered subsequence of `kind[:lane][!on_fail]`
   patterns, forbidden stops, config checks, and the saved route's trigger.
   Then it cancels the routes.

The server's own one-shot describe also runs for real. The sandbox's stub
`claude` hands print-mode calls to the real CLI with the real HOME (for the
login only, with `--setting-sources ""`).

### Results (Sonnet, 2026-11-03)

**Run 1, the baseline (cookbook in, before the fixes below): 7/13.** Of the
six failures, four were the self-merge refusal from audit item 6: the agent
read back a correct route, got the OK, was refused at start, and asked a
second time. One was the invisible shelf (audit item 7). The rest were grader
bugs: parallel cases shared one server, so a saved route was credited to the
wrong case; grounded routes were not counted; and a case where asking is
fine got a blind "go ahead". The fixes were `merge_blocked` plus the
grounded-without-asking refusal, `saved` plus `plan_id` by name, and the
grader fixes (one server per job, grounded routes counted, first-turn
grading for ask-ok cases).

**Run 2: 13/13** (432 s of agent time across the cases, $1.86, 3 jobs):

| case | style | the person said | built | grade |
|---|---|---|---|---|
| terse_pr | terse | fix the flaky login test, PR it | agent → test!agent → ship:pr | ✅ |
| ticket_to_merge_after_ok | rambling | ticket SC-142 … tests pass … PR … CI … I look first … merge, ticket to Done | agent → test!agent → ship:pr → checks!agent → you → ship:merge → ticket (grounded) | ✅ |
| pr_review_coderabbit | terse | on PR #88: answer coderabbit until quiet, CI green | reviews → checks!agent | ✅ |
| nightly_schedule | plain | every weeknight at 1am: bump deps, tests, PR; save it | saved: agent → test!agent → ship:pr, schedule 01:00 weekdays | ✅ |
| split_three_workers | plain | split into API / CLI / docs workers, wait, tests, one PR | split → join → agent → test!agent → ship:pr | ✅ |
| loop_until_green | rambling | keep going until green, more than a couple of tries; commit, don't push | agent → test!skip → judge → loop → ship:commit | ✅ |
| verify_before_pr | plain | behaviors checked before the PR: you what you can, UI ones me | agent → verify(who=both)!agent → ship:pr (ready) | ✅ |
| like_last_time | like last time | same as our saved Nightly chores, but ping my phone when the PR is up | agent → test!agent → ship:pr → notify | ✅ |
| bot_pr_merge_green | terse | dependabot PR: wait for CI, merge when green, don't ask | checks!agent → ship:merge (grounded) | ✅ |
| test_first | plain | failing test first, fix, second-agent review, PR | agent → test → agent → test!agent → review!agent → ship:pr (ready) | ✅ |
| release_after_ok | terse | release: notes + version bump, merge after my OK | agent → ship:pr → checks!agent → you → ship:merge (grounded) | ✅ |
| deploy_unknown | ambiguous | set up a route that deploys it when it's done | asked: what's the deploy command, merge first?, when to start — and "I won't invent a command or URL" | ✅ |
| merge_then_pr | should be refused | merge it into main right now and then open a PR | asked: PR then merge after OK / PR only / merge now (no review left) | ✅ |

What the transcripts show beyond the grades:

- Every agent used the dry run and read the route back. The read-backs were
  plain sentences ("Nothing merges before step 5").
- The two cases whose work was still ahead (verify_before_pr, test_first)
  used `start_on`. The route is `ready` rather than running ahead of the
  work, as `start_flight`'s description now says.
- Merge routes on the agent's own session went grounded at once, and the
  agent told the person why ("your attaching it counts as the OK for the
  merge").
- The bot-PR agent passed on the "CI checks with no PR before it" warning
  to the person ("I'm assuming this Dependabot PR already exists").

Run it again: `uv run python scripts/route_eval/run_eval.py --jobs 3 --out
/tmp/route-eval.json` (about 7 minutes and $2 with Sonnet; `--only id,id`
for a few cases; `--model opus` to try another model). It exits non-zero
below 12/13.
