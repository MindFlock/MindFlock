"""Starting a session from the phone (`/m`): structural contract checks.

Until this, the mobile head said *"No sessions yet. Create one from the desktop
view."* — it could drive every session on the flock and start none of them. The
Describe box (`POST /api/session-plan`) is what made that fixable: one sentence
fills in the same form fields the desktop New Session dialog owns, so the phone
needs two questions and no folder picker, no template list, no launch flags.

The load-bearing assertions here are the two safety properties, not the layout:

1. **The confirm gate.** A plan may propose a folder that does not exist yet,
   and creating a directory is the one thing it proposes that outlives the
   session — closing a session takes its worktree with it, but nobody ever comes
   back for the folder. So **Start refuses while the tick naming that folder is
   unticked**, and these tests exist so the gate cannot be deleted, defaulted on,
   or routed around without something going red.

2. **No folder NAME reaches ``POST /api/instances``.** ``_prepare_plain_repo``
   realpaths whatever it is handed against the *server's* cwd and then
   ``makedirs`` it, which is how typing ``api`` into the desktop dialog once
   created a ``MindFlock/api`` directory. Every folder this page can send came
   out of a walk of the filesystem the server did itself.

Same idiom as ``test_frontend_mobile.py``: ``mobile.{html,js,css}`` are
hand-written and ship exactly as served (no bundler, no build step), so the
wiring is pinned as text.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/mobile.js").text


def _html() -> str:
    return client.get("/m").text


def _css() -> str:
    return client.get("/mobile.css").text


# --------------------------------------------------------------------------- #
# The entry points
# --------------------------------------------------------------------------- #
def test_mobile_has_a_new_session_entry_point():
    html = _html()
    # The "+" lives in the top bar rather than the git action row: that row acts
    # on the selected session and disables itself when there isn't one, which is
    # exactly the case this button exists for.
    assert 'id="new-btn"' in html
    assert 'id="new-sheet"' in html
    js = _js()
    assert (
        'document.getElementById("new-btn").addEventListener("click", openNewSheet);'
        in js
    )


def test_mobile_empty_placeholder_is_the_button_now():
    html = _html()
    # "Create one from the desktop view" is the one instruction a first-run user
    # holding a phone cannot follow.
    assert "Create one from the desktop view" not in html
    assert 'button type="button" id="empty"' in html
    assert 'emptyEl.addEventListener("click", openNewSheet);' in _js()
    # A button has to be un-styled back into the sentence it looks like, and then
    # marked as tappable — otherwise it is the old message with extra steps.
    empty_rule = _css().split("#empty {")[1].split("}")[0]
    assert "border: 0" in empty_rule
    assert "text-decoration: underline" in empty_rule


def test_mobile_new_sheet_reuses_the_bottom_sheet_pattern():
    html = _html()
    # Same .sheet / .sheet-box / .sheet-actions the commit sheet uses.
    assert '<div id="new-sheet" class="sheet hidden">' in html
    assert html.count('class="sheet-box"') >= 2


# --------------------------------------------------------------------------- #
# Two screens, one input each
# --------------------------------------------------------------------------- #
def test_mobile_new_sheet_is_two_screens_one_input_each():
    html = _html()
    for el in (
        'id="new-step1"',  # the sentence
        'id="new-text"',
        'id="new-go"',
        'id="new-cancel"',
        'id="new-step2"',  # the review
        'id="new-title"',
        'id="new-prompt"',
        'id="new-folder"',
        'id="new-back"',
        'id="new-start"',
        'id="new-folders"',  # the folder list (fallback + correction)
        'id="new-folder-list"',
    ):
        assert el in html, el
    # Only step 1 is visible on open; the other two screens start hidden.
    assert '<div id="new-step2" class="hidden">' in html
    assert '<div id="new-folders" class="hidden">' in html


def test_mobile_review_screen_shows_the_typed_sentence_as_the_prompt():
    js = _js()
    # The sentence IS the session's first prompt when the model didn't write one
    # — it is already an instruction, and an agent started with nothing to do is
    # worse than one started with the words that asked for it.
    assert "prompt: j.prompt || text," in js
    # ...and it is editable, not just displayed: what is in the box at Start time
    # is what gets sent.
    assert "newPlan.prompt = newPromptEl.value;" in js
    assert "function harvestPlan(" in js


def test_mobile_review_screen_has_one_mode_line():
    js = _js()
    assert "function modeLine(" in js
    for phrase in (
        '"A new folder"',
        '"work happens in the folder directly."',
        '"Work happens in a new worktree, not in the folder itself."',
    ):
        assert phrase in js, phrase
    # The server's own note is shown as well — it is the only sentence composed
    # from resolved facts (and the only one that can explain a clamp) — minus its
    # leading preamble, which tells the reader to "press Create" and there is no
    # Create button on this page.
    assert "newNoteEl.textContent = planNote(p.note);" in js
    assert (
        'NOTE_PREAMBLE = "Filled in from what you typed — check it and press Create. "'
        in js
    )
    # By exact prefix: if note_for's wording ever changes, the sentence comes
    # back rather than this silently eating a different one.
    assert "t.indexOf(NOTE_PREAMBLE) === 0 ? t.slice(NOTE_PREAMBLE.length) : t;" in js


def test_mobile_note_preamble_matches_the_server_that_writes_it():
    """The dropped sentence is the real one `note_for` emits, not a guess."""
    from backend.web.core import session_plan

    note = session_plan.note_for(
        rel="~/code/x",
        is_new=True,
        is_git=False,
        has_commits=False,
        want_worktree=False,
        in_place=True,
        init_repo=False,
        git_ok=True,
        chosen=None,
    )
    # Read out of the shipped JS rather than retyped here: a copy of the string
    # in this file would be a second place for it to drift.
    preamble = _js().split('var NOTE_PREAMBLE = "')[1].split('";')[0]
    assert note.startswith(preamble)
    # ...and dropping it leaves the part worth reading on a phone.
    assert note[len(preamble) :].startswith("Using ~/code/x")


# --------------------------------------------------------------------------- #
# The plan turn
# --------------------------------------------------------------------------- #
def test_mobile_new_session_asks_the_plan_route():
    js = _js()
    assert 'fetch("/api/session-plan", {' in js
    assert "body: JSON.stringify({ text: text.slice(0, PLAN_MAX_CHARS) })," in js
    # session_plan.MAX_SENTENCE, matched by the box's own maxlength.
    assert "PLAN_MAX_CHARS = 2000" in js
    assert 'maxlength="2000"' in _html()


def test_mobile_plan_button_shows_a_pending_state():
    js = _js()
    # A real model turn is ~10-25s; a button that never changes reads as a hung
    # page. Same 8s relabel the desktop Describe button does.
    assert "NEW_SLOW_MS = 8000" in js
    assert 'setPlanBusy(true, "Reading…");' in js
    assert 'newGoBtn.textContent = "Still reading…";' in js
    assert "newGoBtn.disabled = on;" in js


def test_mobile_plan_answers_are_sequence_guarded():
    js = _js()
    # Closing the sheet bumps the counter, which makes a late answer a no-op
    # whenever it lands — the subprocess is a read-only one-shot and runs to its
    # own timeout either way.
    assert "planSeq += 1;" in js
    assert js.count("if (seq !== planSeq) return;") >= 2


def test_mobile_plan_answer_defaults_are_the_fail_safe_direction():
    js = _js()
    # An absent `in_place` means in-place: of the two ways to be wrong about a
    # missing value, only `false` opens a worktree and writes a branch into
    # somebody's repo.
    assert "in_place: j.in_place !== false," in js
    # An absent `folder_exists` means the folder is NOT there. Being wrong that
    # way costs one extra tick on a folder that already exists; the other way
    # round is a directory created on the user's disk with nobody confirming it.
    assert "folder_exists: j.folder_exists === true," in js


# --------------------------------------------------------------------------- #
# No model? Not a dead end.
# --------------------------------------------------------------------------- #
def test_mobile_plan_failure_reaches_the_folder_list():
    js = _js()
    # /api/session-plan answers 502 with one human sentence when there is no CLI
    # installed, when it times out, or when the answer can't be read. On a phone
    # that must not be the end of the road: the sentence is shown and the folder
    # list takes over inside the same sheet.
    plan_fn = js.split("  function describeIt() {")[1].split("\n  function ")[0]
    assert "loadFolders(" in plan_fn
    assert '(err && err.message) || "couldn\'t read that"' in plan_fn
    assert 'fetch("/api/repos/suggest")' in js
    # The typed sentence still becomes the prompt on the manual path.
    assert "prompt: (newPlan && newPlan.prompt) || newTextEl.value.trim()," in js


def test_mobile_folder_list_is_reachable_from_the_review_screen():
    js = _js()
    # A plan that picked the wrong repo has to be correctable without a desktop.
    assert 'newFolderBtn.addEventListener("click", function () {' in js
    # The "2" is which screen Back returns to — see
    # test_mobile_folder_list_back_returns_to_the_screen_it_came_from.
    assert 'loadFolders("Pick the folder this session should work in.", 2);' in js


def test_mobile_folder_rows_are_built_as_text():
    js = _js()
    # Directory names off the user's own disk reach the DOM as text, the same
    # rule the diff panel holds for repo content.
    row_fn = js.split("  function folderRow(row) {")[1].split("\n  function ")[0]
    assert "name.textContent = row.name || row.path" in row_fn
    assert "innerHTML =" not in row_fn


def test_mobile_hand_picked_folder_mirrors_the_create_route_clamp():
    js = _js()
    # A non-git folder has no HEAD to fork a worktree from and the server forces
    # in-place, so a review screen that said "worktree" over it would promise
    # something the 202 quietly does not do.
    assert "in_place: newPlan ? (!!newPlan.in_place || !git) : true," in js
    # git init is never ticked for a folder chosen off a list of folders that
    # already exist — that is a decision nobody made.
    assert "init_repo: false," in js


# --------------------------------------------------------------------------- #
# THE CONFIRM GATE
# --------------------------------------------------------------------------- #
def test_mobile_start_is_guarded_by_the_confirm_tick():
    """The gate, pinned at its narrowest point: Start cannot reach the create
    route while a not-yet-existing folder is unconfirmed."""
    js = _js()
    assert "function startBlockReason(" in js
    assert "if (!p.folder_exists && !newConfirmEl.checked)" in js
    start_fn = js.split("  function startSession() {")[1].split("\n  function ")[0]
    # The guard runs FIRST and returns — the POST is downstream of it, so the
    # gate cannot be bypassed by any path through this function.
    assert "var blocked = startBlockReason(newPlan);" in start_fn
    assert "if (blocked) { newError(blocked); return; }" in start_fn
    assert start_fn.index("startBlockReason(") < start_fn.index('"/api/instances"')
    # And there is exactly one place that creates a session.
    assert js.count('fetch("/api/instances", {') == 1


def test_mobile_confirm_tick_defaults_off_and_names_the_folder():
    js = _js()
    html = _html()
    # Unticked for every plan that lands, including a second plan naming the
    # same folder: the tick means "I read THIS folder name and said yes".
    assert "newConfirmEl.checked = false;" in js
    assert (
        'newConfirmLabel.textContent =\n      "Create the folder " + '
        "(p.folder_display || p.repo_path" in js
    )
    # Shown only when the folder isn't there yet.
    assert 'newConfirmRow.classList.toggle("hidden", !!p.folder_exists);' in js
    # The markup ships unchecked — nothing to un-tick on first render.
    confirm_input = html.split('id="new-confirm"')[0].rsplit("<input", 1)[1]
    assert "checked" not in confirm_input


def test_mobile_confirm_refusal_says_why_and_names_the_folder():
    js = _js()
    # A sentence, not a disabled button: a greyed-out control with no
    # explanation is the version of a safety gate people learn to ignore.
    assert '"This would create " + (p.folder_display || p.repo_path) +' in js
    assert "tick the box to confirm that folder first." in js
    assert "newStartBtn.disabled = true;" in js  # only while the POST is in flight
    assert "newStartBtn.disabled = false;" in js


def test_mobile_confirm_copy_separates_the_directory_from_git_init():
    html = _html()
    # "Create a git repo in this folder" (init_repo) and "create the folder" are
    # different things and can both be true at once. The gate is about the
    # directory; the copy has to say so where the tick is.
    hint = html.split('id="new-confirm-row"')[1].split("</label>")[0]
    assert "makes\n                the directory itself" in hint
    assert "not the same thing as creating a\n                git repo" in hint
    # ...and the mode line says the other half when a plan asks for both.
    assert '", with a git repo created inside it"' in _js()


def test_mobile_confirm_gate_is_visually_a_question_not_prose():
    css = _css()
    rule = css.split(".new-confirm {")[1].split("}")[0]
    assert "border:" in rule and "background:" in rule
    # 20px, not the ~13px a phone renders by default.
    box = css.split(".new-confirm input {")[1].split("}")[0]
    assert "width: 20px" in box and "height: 20px" in box


# --------------------------------------------------------------------------- #
# Creating the session
# --------------------------------------------------------------------------- #
def test_mobile_create_sends_five_keys_and_no_more():
    js = _js()
    start_fn = js.split("  function startSession() {")[1].split("\n  function ")[0]
    for key in (
        "title: p.title,",
        "repo_path: p.repo_path,",
        "prompt: p.prompt,",
        "in_place: !!p.in_place,",
        "init_repo: !!p.init_repo,",
    ):
        assert key in start_fn, key
    # Every one of these inherits the user's configured default when the key is
    # absent, which is exactly why the phone doesn't ask about any of them.
    for key in (
        "program",
        "launch_args",
        "profile_id",
        "profile_model",
        "provisioned",
        "workspace_strategy",
    ):
        assert '"%s"' % key not in start_fn, key
        assert "%s:" % key not in start_fn, key


def test_mobile_create_never_sends_a_bare_folder_name():
    js = _js()
    # There is no free-text folder input on the page at all — the folder line in
    # step 2 is a BUTTON into a server-built list.
    html = _html()
    assert '<button type="button" id="new-folder" class="new-row">' in html
    assert 'id="new-folder"' not in html.replace(
        '<button type="button" id="new-folder" class="new-row">', ""
    )
    # And the create path refuses anything that isn't absolute anyway: a bare
    # name is realpath'd against the SERVER's cwd and then makedirs'd.
    assert 'if (!p.repo_path || p.repo_path.charAt(0) !== "/")' in js


def test_mobile_create_keeps_the_sheet_open_on_failure():
    js = _js()
    start_fn = js.split("  function startSession() {")[1].split("\n  function ")[0]
    # A 409 on a duplicate name is fixed by editing the name on screen, and
    # #status lives behind this sheet where nobody could read it.
    # rsplit: the inner ``r.json().catch(…)`` is the response-parsing idiom every
    # fetch on this page uses, not the failure handler.
    fail = start_fn.rsplit(".catch(", 1)[1]
    assert "closeNewSheet();" not in fail
    assert "newError(" in fail
    assert 'if (!r.ok) throw new Error((j && j.error) || "create failed (' in start_fn


def test_mobile_create_selects_the_new_session_when_it_lands():
    js = _js()
    # POST /api/instances answers 202: the instance registers as Loading and its
    # real start runs in a background task, so the row arrives a poll later.
    assert "function claimPending(" in js
    assert "claimPending();" in js
    claim = js.split("  function claimPending() {")[1].split("\n  function ")[0]
    assert "pickerEl.value = title;" in claim
    assert "select(title);" in claim
    # The TITLE comes back from the server — an auto-named session can be
    # re-numbered under the engine lock, and waiting for a name we invented
    # would be waiting for a row that never appears.
    assert "var title = (j && j.title) || p.title;" in js
    # A create that fails after its 202 never joins the list, and nothing here
    # listens for session.create_failed, so the wait is bounded.
    assert "PENDING_NEW_MS = 120000" in js
    assert "didn't start — open it on the desktop to see why" in claim


# --------------------------------------------------------------------------- #
# Phone details that are not optional
# --------------------------------------------------------------------------- #
def test_mobile_new_sheet_inputs_clear_the_ios_zoom_floor():
    css = _css()
    rule = css.split("#new-text, #new-title, #new-prompt {")[1].split("}")[0]
    # Below 16px iOS Safari zooms the whole page on focus.
    assert "font-size: 16px" in rule


def test_mobile_new_sheet_survives_the_soft_keyboard():
    css = _css()
    js = _js()
    # #app is already sized to the visual viewport, so max-height:100% is the
    # keyboard-aware height: the box scrolls inside it instead of running off
    # the bottom. Scoped to this sheet — the commit sheet is a single field that
    # must keep growing with its message.
    rule = css.split("#new-sheet .sheet-box {")[1].split("}")[0]
    assert "max-height: 100%" in rule
    assert "overflow-y: auto" in rule
    # The safe-area inset still comes from the shared .sheet-box rule.
    assert "env(safe-area-inset-bottom)" in css.split(".sheet-box {")[1].split("}")[0]
    # Focusing any field in the sheet re-applies the viewport, the same nudge the
    # compose box gets (focusin/focusout bubble, so one pair covers them all).
    assert "function nudgeViewport(" in js
    assert 'newSheet.addEventListener("focusin", nudgeViewport);' in js
    assert 'newSheet.addEventListener("focusout", nudgeViewport);' in js
    assert 'composeEl.addEventListener("focus", nudgeViewport);' in js


def test_mobile_new_sheet_dismisses_like_the_commit_sheet():
    js = _js()
    assert (
        'newSheet.addEventListener("click", function (ev) {\n'
        "    if (ev.target === newSheet) closeNewSheet();" in js
    )
    # Enter continues, Shift+Enter keeps editing — the same bargain the compose
    # box strikes on the same keyboard.
    assert (
        'newTextEl.addEventListener("keydown", function (ev) {\n'
        '    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); describeIt(); }'
        in js
    )


def test_mobile_new_session_js_is_still_es5_shaped():
    """mobile.js ships exactly as written — no bundler, no transpile step."""
    js = _js()
    section = js.split('// --- new session ("+")')[1].split("// --- wiring")[0]
    for banned in ("=>", "const ", "let ", "async ", "await ", "..."):
        assert banned not in section, banned


# --------------------------------------------------------------------------- #
# The session you just created is still being provisioned
# --------------------------------------------------------------------------- #
def test_mobile_does_not_attach_while_the_instance_is_still_loading():
    """``POST /api/instances`` registers the instance as Loading *before* it
    answers 202, and ``GET /api/instances`` lists it on the very next poll — so
    "there is a row" is not "there is a PTY". Attaching that early gets the
    socket closed with 4409, which ``onclose`` treats as terminal."""
    js = _js()
    # The status the row actually carries, read the way activityOf reads its
    # activity (`nextAct` already keys off this exact value).
    assert "function statusOf(" in js
    assert 'return statusOf(title) === "loading";' in js
    select_fn = js.split("  function select(title) {")[1].split("\n  }\n")[0]
    assert "if (isLoading(title)) {" in select_fn
    # The guard is upstream of the only connect() in select(), so no path
    # through this function opens a websocket to a workspace that isn't there.
    # (Anchored with its indentation: "disconnect();" contains "connect();".)
    assert select_fn.count("\n      connect();") == 1
    assert select_fn.index("isLoading(title)") < select_fn.index("\n      connect();")
    assert "awaitingReady = title;" in select_fn


def test_mobile_loading_attach_is_retried_by_a_poll_tick():
    js = _js()
    # The recovery has to be driven by something that RE-FIRES. Re-picking the
    # same session can't: select() early-returns on the current title, a re-pick
    # of the selected <option> fires no `change` event, and renderPicker only
    # re-selects when poll's signature moves — attnRank does not move when a
    # status leaves "loading". Before this, the 4409 was permanent and the phone
    # kept a blank terminal for the session it had just started.
    assert "function attachWhenReady(" in js
    poll_fn = js.split("  function poll() {")[1].split("\n  }\n")[0]
    assert "attachWhenReady();" in poll_fn
    # After claimPending, which is what selected the new session this tick.
    assert poll_fn.index("claimPending();") < poll_fn.index("attachWhenReady();")
    attach = js.split("  function attachWhenReady() {")[1].split("\n  }\n")[0]
    assert "if (isLoading(current)) return;" in attach
    assert "connect();" in attach
    assert attach.index("if (isLoading(current)) return;") < attach.index("connect();")
    # A selection that moved on is not this wait's business any more.
    assert "if (awaitingReady !== current)" in attach


def test_mobile_provisioning_line_outlives_the_create_flash():
    js = _js()
    # flashStatus("starting X…") arms a 2500ms clear; the provisioning line for
    # that same session goes up ~1s later, and the flash then wiped it — leaving
    # a blank black terminal with no message at all on a first-run phone.
    set_status = js.split("  function setStatus(msg) {")[1].split("\n  }\n")[0]
    assert (
        "if (statusFlashTimer) { clearTimeout(statusFlashTimer); statusFlashTimer = null; }"
        in set_status
    )
    # So the wait itself is a plain status, not a flash.
    select_fn = js.split("  function select(title) {")[1].split("\n  }\n")[0]
    assert "flashStatus(" not in select_fn


# --------------------------------------------------------------------------- #
# The create is sequence-guarded, like the plan
# --------------------------------------------------------------------------- #
def test_mobile_create_is_sequence_guarded_in_both_branches():
    """Tap Start, dismiss the sheet mid-POST, tap "+" and start typing: the
    first create's answer must not touch the sheet the user is now in."""
    js = _js()
    close_fn = js.split("  function closeNewSheet() {")[1].split("\n  }\n")[0]
    assert "startSeq += 1;" in close_fn
    start_fn = js.split("  function startSession() {")[1].split("\n  function ")[0]
    assert "var seq = ++startSeq;" in start_fn
    # rsplit on the (err) signature: the inner `.catch(function () { return {}; })`
    # is the response-parsing idiom every fetch on this page uses.
    ok, fail = start_fn.split(".catch(function (err) {")
    assert "if (seq !== startSeq) return;" in ok
    assert "if (seq !== startSeq) return;" in fail
    # Success branch: the guard comes before the sheet is closed, so a 202 can
    # no longer close a REOPENED sheet and discard the sentence in it...
    assert ok.index("if (seq !== startSeq) return;") < ok.index("closeNewSheet();")
    # ...but the session was really created, so it is still tracked and selected.
    assert ok.index("pendingNew = title;") < ok.index("if (seq !== startSeq) return;")
    # Failure branch: the mirror image — the PREVIOUS session's create failure
    # must not be written into the freshly reopened sheet.
    assert fail.index("if (seq !== startSeq) return;") < fail.index("newError(")


def test_mobile_start_button_state_never_survives_a_dismiss():
    js = _js()
    # startBusy / "Starting…" are not reset by a stale answer, so open and close
    # own them: without this, a sheet dismissed while the POST was in flight
    # reopened with Start permanently disabled (startSession returns at
    # `if (startBusy) return;`) and nothing but a page reload fixed it.
    for fn in ("openNewSheet", "closeNewSheet"):
        body = js.split("  function %s() {" % fn)[1].split("\n  }\n")[0]
        assert "resetStartBtn();" in body, fn
    reset = js.split("  function resetStartBtn() {")[1].split("\n  }\n")[0]
    assert "startBusy = false;" in reset
    assert 'newStartBtn.textContent = "Start session";' in reset


# --------------------------------------------------------------------------- #
# Back from the folder list
# --------------------------------------------------------------------------- #
def test_mobile_folder_list_back_returns_to_the_screen_it_came_from():
    js = _js()
    # "is there a plan" is not "which screen did I come from". describe → plan A
    # → Back → retype → Continue → 502 → folder list → Back used to land on
    # step 2 still showing plan A (showPlan never ran on that path), with its
    # confirm tick still set, and Start there created the FIRST sentence.
    back = js.split(
        'document.getElementById("new-folders-back").addEventListener'
        '("click", function () {'
    )[1].split("\n  });")[0]
    assert "newStep(foldersBack);" in back
    # Pinned inside the handler: the back target must not be DERIVED from the
    # plan again, by this or any other spelling.
    assert "newPlan" not in back
    assert "function loadFolders(msg, from) {" in js
    assert "foldersBack = from === 2 ? 2 : 1;" in js
    # Both entry points say where they came from: the review screen's Change
    # button (2) and the plan failure's fallback (1).
    assert 'loadFolders("Pick the folder this session should work in.", 2);' in js
    plan_fn = js.split("  function describeIt() {")[1].split("\n  function ")[0]
    assert '" — pick the folder yourself.", 1);' in plan_fn


def test_mobile_redescribe_drops_the_previous_plan_and_its_tick():
    js = _js()
    plan_fn = js.split("  function describeIt() {")[1].split("\n  function ")[0]
    # Cleared when the re-describe STARTS, not when its answer lands: a plan
    # that fails never calls showPlan, so a previous plan left in place stays
    # reviewable — and a confirm box still ticked over a plan the user never
    # read is a confirm-gate failure, not only a navigation one.
    assert "newPlan = null;" in plan_fn
    assert "newConfirmEl.checked = false;" in plan_fn
    assert plan_fn.index("newPlan = null;") < plan_fn.index('fetch("/api/session-plan"')
