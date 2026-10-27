/* Mobile single-terminal head for mindflock.
 *
 * One full-screen xterm.js bound to one session at a time, plus a session
 * picker and a soft-key bar. It speaks the exact same websocket protocol the
 * desktop grid (app.js) uses:
 *   - connect to  /api/instances/<title>/terminal   (agent)  or  /shell
 *   - ws.binaryType = "arraybuffer"; PTY output arrives as binary frames,
 *     control/error messages as JSON text frames
 *   - keystrokes are sent verbatim via term.onData
 *   - a resize is a JSON text frame {type:"resize", cols, rows}
 *   - reconnect on close unless code 4404 (instance gone) / 4409 (workspace gone)
 *     / 4423 (session paused — waits for Resume, then attaches)
 */
(function () {
  "use strict";

  var FitAddon = window.FitAddon ? window.FitAddon.FitAddon : null;
  // Terminal colors follow the shared appearance themes (theme.css): the
  // surface set drives the canvas, the accent drives the cursor. Fallbacks are
  // the classic desktop dark palette.
  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      if (v) return v;
    } catch (e) {}
    return fallback;
  }
  function termTheme() {
    return {
      background: cssVar("--term-bg", "#0f1117"),
      foreground: cssVar("--term-fg", "#d7dae3"),
      cursor: cssVar("--accent", "#7d56f4"),
      selectionBackground: "#33384a",
    };
  }

  var pickerEl = document.getElementById("picker");       // opens the Sessions sheet
  var pickerTitleEl = document.getElementById("picker-title");
  var stateChipEl = document.getElementById("state-chip");
  var sessionListEl = document.getElementById("session-list");
  var dotEl = document.getElementById("dot");
  var termWrap = document.getElementById("term-wrap");
  var termHost = document.getElementById("term");
  var emptyEl = document.getElementById("empty");
  var statusEl = document.getElementById("status");
  var ctrlBtn = document.getElementById("ctrl");
  var actionsEl = document.getElementById("actions");
  var commitSheet = document.getElementById("commit-sheet");
  var commitText = document.getElementById("commit-text");
  var diffWrap = document.getElementById("diff-wrap");
  var diffBody = document.getElementById("diff-body");
  var diffStat = document.getElementById("diff-stat");
  var diffBaseBtn = document.getElementById("diff-base");

  var term = null;
  var fit = null;
  var ws = null;
  var closedByUs = false;       // true when we intentionally tear down a ws
  var current = null;           // selected session title
  // Two different "which tab": `view` is what the screen shows (the Diff panel
  // is a view with no PTY behind it), `tab` is which tmux session the terminal
  // is attached to. Splitting them is what lets Diff overlay the terminal
  // without tearing the websocket down and rebuilding it on the way back.
  var view = "agent";           // "agent" | "shell" | "diff"
  var tab = "agent";            // "agent" | "shell"
  var ctrlActive = false;       // sticky Ctrl modifier
  var instances = [];
  // Title whose terminal we are deliberately NOT attaching to yet because the
  // server is still provisioning it (status "loading"), or because it is
  // PAUSED and this is its agent tab (the server refuses that socket with
  // 4423 rather than relaunch an agent Pause stopped). See select() and
  // attachWhenReady().
  var awaitingReady = "";
  var PAUSED_CLOSE = 4423;
  var PAUSED_LINE = "Paused · the agent is stopped; your worktree and " +
    "conversation are kept. ⋯ → Resume session starts it again.";

  // --- terminal bootstrap --------------------------------------------------
  function buildTerm() {
    term = new window.Terminal({
      cursorBlink: true,
      fontSize: 13,
      theme: termTheme(),
      fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, monospace',
      scrollback: 10000,
    });
    fit = FitAddon ? new FitAddon() : null;
    if (fit) term.loadAddon(fit);
    term.open(termHost);

    // Keystrokes -> ws. When the sticky Ctrl is armed, fold the next printable
    // char into its control code (e.g. "c" -> 0x03) the way a real Ctrl would.
    term.onData(function (data) {
      if (ctrlActive && data.length === 1) {
        var c = data.toUpperCase().charCodeAt(0);
        if (c >= 64 && c <= 95) data = String.fromCharCode(c & 0x1f);
        setCtrl(false);
      }
      send(data);
    });
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
  }

  var fitTimer = null;
  function fitSoon() {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(doFit, 60);
  }
  function doFit() {
    if (!fit || !term) return;
    if (!termHost.clientWidth || !termHost.clientHeight) return;
    try { fit.fit(); } catch (e) { return; }
    if (ws && ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
  }

  // --- websocket lifecycle -------------------------------------------------
  function disconnect() {
    closedByUs = true;
    if (ws) { try { ws.close(); } catch (e) {} }
    ws = null;
  }

  function connect() {
    if (!current) return;
    closedByUs = false;
    var path = tab === "shell" ? "/shell" : "/terminal";
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var sock = new WebSocket(
      proto + "://" + location.host +
      "/api/instances/" + encodeURIComponent(current) + path
    );
    sock.binaryType = "arraybuffer";
    ws = sock;

    sock.onopen = function () { setStatus(""); doFit(); };
    sock.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        try {
          var j = JSON.parse(ev.data);
          if (j && j.type === "error") { setStatus(j.message || "error"); return; }
        } catch (e) { term.write(ev.data); }
      } else {
        term.write(new Uint8Array(ev.data));
      }
    };
    sock.onclose = function (ev) {
      if (closedByUs || sock !== ws) return;
      // Auth gate rejected us (cookie expired) — reload to the sign-in page.
      if (ev && ev.code === 4401) { location.reload(); return; }
      // Backend reboots a dead agent session on reconnect, so retry — except
      // when the instance / workspace is truly gone.
      if (ev && (ev.code === 4404 || ev.code === 4409)) {
        setStatus(ev.code === 4404 ? "session gone" : "workspace gone");
        return;
      }
      // Paused (the pause just landed, or the list is a tick behind): no
      // retry loop — wait for Resume; the poll tick attaches after it.
      if (ev && ev.code === PAUSED_CLOSE) {
        awaitingReady = current;
        setStatus(PAUSED_LINE);
        return;
      }
      setStatus("reconnecting…");
      setTimeout(function () { if (!closedByUs && sock === ws) connect(); }, 2500);
    };
    sock.onerror = function () { setStatus("connection error"); };
  }

  // Switch the displayed session (or tab) without leaking the old terminal.
  function select(title) {
    if (title === current && term) return;
    disconnect();
    current = title;
    awaitingReady = "";          // a new selection supersedes a provisioning wait
    setCtrl(false);
    if (term) { try { term.dispose(); } catch (e) {} term = null; }
    termHost.innerHTML = "";
    updateDot();
    if (!title) { emptyEl.classList.remove("hidden"); setStatus(""); return; }
    emptyEl.classList.add("hidden");
    try { localStorage.setItem("cs_mobile_last", title); } catch (e) {}
    buildTerm();
    fitSoon();
    // THE LOADING GUARD. Attaching to an instance whose workspace is still
    // being made gets the socket closed with 4409, which onclose treats as
    // terminal ("workspace gone", no reconnect) — and nothing ever retried it:
    // select() early-returns on the same title, re-picking the current option
    // fires no `change` event, and renderPicker only re-selects when poll's
    // signature moves (attnRank does not move when a status leaves "loading").
    // The phone was left on a permanently blank terminal for the session it had
    // just created. So wait, visibly, and let a poll tick attach.
    if (isLoading(title)) {
      awaitingReady = title;
      setStatus("starting " + nameOf(title) + " — setting up the workspace…");
    } else if (agentPaused(title)) {
      // The shell tab still attaches: Pause stops only the agent.
      awaitingReady = title;
      setStatus(PAUSED_LINE);
    } else {
      connect();
    }
    updateActions();
    updatePlanCard();
    voiceOnSelect(title);
    if (view === "diff") loadDiff();
  }

  function switchTab(next) {
    if (next === view) return;
    view = next;
    document.querySelectorAll(".tab").forEach(function (b) {
      b.classList.toggle("active", b.dataset.tab === next);
    });
    // The Diff panel takes the terminal's place; the compose box and key bar go
    // with it (there is nothing to type at) but the git action bar stays — the
    // whole point of reading the diff on a phone is deciding whether to push it.
    var isDiff = next === "diff";
    diffWrap.classList.toggle("hidden", !isDiff);
    termWrap.classList.toggle("hidden", isDiff);
    document.getElementById("composer").classList.toggle("hidden", isDiff);
    document.getElementById("keys").classList.toggle("hidden", isDiff);
    if (isDiff) { loadDiff(); return; }
    // Coming back from Diff to the PTY the terminal is already attached to:
    // nothing to rebuild, just re-fit into the space it got back.
    if (next === tab) { fitSoon(); return; }
    tab = next;
    // Rebuild the terminal against the other tmux session of the same instance.
    var t = current; current = null; select(t);
  }

  function setStatus(msg) {
    // An explicit status supersedes a pending flashStatus clear. Without this,
    // the "starting X…" flash fired at create time wiped (at +2500ms) the
    // provisioning line select() puts up a second later for that same session,
    // and the phone was left on a blank black terminal with no message at all.
    if (statusFlashTimer) { clearTimeout(statusFlashTimer); statusFlashTimer = null; }
    if (!msg) { statusEl.classList.add("hidden"); statusEl.textContent = ""; return; }
    // The toast lives inside whichever panel is on screen — parked in the
    // terminal wrap it would be display:none along with it under the Diff tab,
    // which is exactly where "pushed" / "merge failed" needs to be readable.
    var host = view === "diff" ? diffWrap : termWrap;
    if (statusEl.parentNode !== host) host.appendChild(statusEl);
    statusEl.textContent = msg;
    statusEl.classList.remove("hidden");
  }

  function setCtrl(on) {
    ctrlActive = on;
    ctrlBtn.classList.toggle("on", on);
  }

  // --- session list polling ------------------------------------------------
  // POST /api/instances registers the instance as Loading and answers 202
  // BEFORE the worktree exists; GET /api/instances lists it on the very next
  // poll. So "is there a row" is not "is there a PTY" — this is how select()
  // tells the two apart.
  function statusOf(title) {
    for (var i = 0; i < instances.length; i++)
      if (instances[i].title === title) return instances[i].status || "";
    return "";
  }
  function isLoading(title) {
    return statusOf(title) === "loading";
  }
  // A paused session's AGENT tab has nothing to attach to until Resume.
  function agentPaused(title) {
    return tab !== "shell" && statusOf(title) === "paused";
  }
  // What a session is CALLED: the server's `name` (the rename, else the label
  // the desktop rail shows), else the title. Display only — every route below
  // is still addressed by `title`.
  function nameOf(title) {
    for (var i = 0; i < instances.length; i++)
      if (instances[i].title === title) return instances[i].name || title;
    return title;
  }

  // The `?s=` deep link a notification taps through to. On the shared phone
  // link (one URL that whichever of your devices is up answers) it names the
  // session as "<device>::<title>" — how every OTHER device lists it — so
  // the device that owns it has to strip its own prefix back off, which needs
  // to know which device it is: the public hello says.
  var deepLink = new URLSearchParams(location.search).get("s");
  var selfDevice = null; // null = not known yet
  function resolveDeepLink(s, titles) {
    if (!s) return null;
    if (titles.indexOf(s) >= 0) return s;
    var i = s.indexOf("::");
    if (i > 0 && selfDevice && s.slice(0, i) === selfDevice) {
      var bare = s.slice(i + 2);
      if (titles.indexOf(bare) >= 0) return bare;
    }
    return null;
  }
  if (deepLink && deepLink.indexOf("::") > 0) {
    fetch("/api/remote/hello")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        selfDevice = (j && j.device) || "";
        // The first render may have beaten us here and fallen back to another
        // session — the link names THIS device's, so take it now.
        var titles = instances.map(function (x) { return x.title; });
        var t = resolveDeepLink(deepLink, titles);
        if (t && t !== current) select(t);
      })
      .catch(function () { selfDevice = ""; });
  }

  // The Sessions sheet's rows, rebuilt whenever poll's signature moves (a
  // title, its attention rank or its state word changed). Each row reads like
  // the rail's: a tone dot, the name, the state word, the repo — and a ⋯ (or a
  // long press) for that session's actions.
  function renderPicker() {
    var prev = current;
    renderRows();
    if (!instances.length) {
      // UNCONDITIONALLY, not `if (prev)`. The placeholder starts hidden in the
      // markup and select(null) is the only thing that reveals it, so guarding
      // this on a previous selection meant a phone that opened /m with no
      // sessions at all — the first-run case — showed a blank black rectangle
      // and never the "No sessions yet" line. It only ever appeared after the
      // LAST session was closed while you were looking at it. Harmless to
      // repeat: with no websocket and no terminal, select(null) is three
      // no-ops and a classList change. Now that the placeholder is the button
      // that starts a session, this is the difference between a usable
      // first-run phone and a dead end.
      select(null);
      return;
    }
    // O1: attention-first — sessions that need the human sort to the top, the
    // same order the bell lists them in, so triage works without a sidebar.
    renderRows();
    // Keep the current selection if it still exists; else pick a sensible one.
    var titles = instances.map(function (i) { return i.title; });
    var want = prev && titles.indexOf(prev) >= 0 ? prev : null;
    if (!want) {
      var saved = null;
      try { saved = localStorage.getItem("cs_mobile_last"); } catch (e) {}
      var qs = resolveDeepLink(deepLink, titles);
      want = qs ? qs
           : (saved && titles.indexOf(saved) >= 0) ? saved
           : titles[0];
    }
    if (want !== current) select(want);
  }

  // The rows are numbered 1, 2, … top to bottom, and rowOrder is that order:
  // what "session two" means by voice. While the sheet is open the order
  // holds (a row that changes state does not jump under the finger or the
  // number just read); a new session goes to the end, a closed one drops
  // out. Closed, the next render sorts afresh.
  var rowOrder = [];
  function renderRows() {
    sessionListEl.innerHTML = "";
    var sorted = instances.slice().sort(function (a, b) {
      return attnRank(a) - attnRank(b);
    }).map(function (inst) { return inst.title; });
    var open = !!sessionsSheet && !sessionsSheet.classList.contains("hidden");
    if (open && rowOrder.length) {
      var kept = rowOrder.filter(function (t) { return sorted.indexOf(t) >= 0; });
      rowOrder = kept.concat(sorted.filter(function (t) { return kept.indexOf(t) < 0; }));
    } else {
      rowOrder = sorted;
    }
    if (!instances.length) {
      sessionListEl.appendChild(folderNote("No open sessions."));
      return;
    }
    rowOrder.forEach(function (title, i) {
      var row = sessionRow(instByTitle(title));
      var main = row.querySelector(".sess-main");
      var num = document.createElement("span");
      num.className = "sess-num";
      num.textContent = String(i + 1);
      num.setAttribute("aria-hidden", "true");
      main.insertBefore(num, main.firstChild);
      sessionListEl.appendChild(row);
    });
  }

  // How long a press must be held to count as a long press (and not a tap).
  var LONG_PRESS_MS = 500;

  function sessionRow(inst) {
    var title = inst.title;
    var ss = sessionState(inst);
    var row = document.createElement("div");
    row.className = "sess-row" + (title === current ? " current" : "");
    row.setAttribute("data-title", title);
    var main = document.createElement("button");
    main.type = "button";
    main.className = "sess-main";
    var dot = document.createElement("span");
    dot.className = "dot " + attnMark(inst) + " st-" + ss.state;
    var text = document.createElement("span");
    text.className = "sess-text";
    // textContent throughout: titles and repo names are the user's own strings.
    var name = document.createElement("span");
    name.className = "sess-name";
    name.textContent = inst.name || title;
    var sub = document.createElement("span");
    sub.className = "sess-sub";
    var word = document.createElement("span");
    word.className = "sess-state tone-" + ss.tone;
    word.textContent = ss.label;
    sub.appendChild(word);
    var rz = rzChip(inst);
    if (rz) {
      var chip = document.createElement("span");
      chip.className = "rz-chip " + rz.cls;
      chip.textContent = rz.label;
      chip.title = rz.title;
      chip.setAttribute("aria-label", rz.title);
      sub.appendChild(document.createTextNode(" "));
      sub.appendChild(chip);
    }
    if (inst.repo) sub.appendChild(document.createTextNode("  ·  " + inst.repo));
    text.appendChild(name);
    text.appendChild(sub);
    // What it asks, under the state word: the Question chip says THAT it
    // asks, this line says what (a tap opens the ask sheet over it).
    var askLine = askRowText(inst);
    if (askLine) {
      var q = document.createElement("span");
      q.className = "sess-ask";
      q.textContent = askLine;
      text.appendChild(q);
    }
    main.appendChild(dot);
    main.appendChild(text);
    var more = document.createElement("button");
    more.type = "button";
    more.className = "sess-more";
    more.textContent = "⋯";
    more.setAttribute("aria-label", "Actions for " + nameOf(title));
    more.addEventListener("click", function () { openActSheet(title); });
    // Long press = ⋯. A timer rather than `contextmenu` alone: iOS Safari never
    // fires contextmenu for a touch, Android Chrome does — both are wired, and
    // `pressed` stops the click that follows the release from also opening the
    // session.
    var timer = null, pressed = false, x0 = 0, y0 = 0;
    function cancelPress() { if (timer) { clearTimeout(timer); timer = null; } }
    main.addEventListener("pointerdown", function (ev) {
      pressed = false;
      x0 = ev.clientX; y0 = ev.clientY;
      cancelPress();
      timer = setTimeout(function () {
        timer = null;
        pressed = true;
        openActSheet(title);
      }, LONG_PRESS_MS);
    });
    main.addEventListener("pointermove", function (ev) {
      if (Math.abs(ev.clientX - x0) > 10 || Math.abs(ev.clientY - y0) > 10) cancelPress();
    });
    main.addEventListener("pointerup", cancelPress);
    main.addEventListener("pointercancel", cancelPress);
    main.addEventListener("contextmenu", function (ev) {
      ev.preventDefault();
      cancelPress();
      if (!pressed) { pressed = true; openActSheet(title); }
    });
    main.addEventListener("click", function () {
      if (pressed) { pressed = false; return; }
      closeSheet(sessionsSheet);
      select(title);
      askOpenFor(title, "row");   // asking → its sheet, over the session
    });
    row.appendChild(main);
    row.appendChild(more);
    return row;
  }

  // The header: the dot and the chip are painted from the SAME state object,
  // the way the rail paints a row's dot and chip (sessionState.ts).
  function updateDot() {
    var inst = currentInst();
    pickerTitleEl.textContent = current ? nameOf(current) : (instances.length ? "Sessions" : "no sessions");
    if (!inst) {
      dotEl.className = "dot tone-muted";
      stateChipEl.classList.add("hidden");
      return;
    }
    var ss = sessionState(inst);
    dotEl.className = "dot tone-" + ss.tone + " st-" + ss.state;
    dotEl.title = ss.hint || ss.label;
    stateChipEl.textContent = ss.label;
    stateChipEl.className = "state-chip tone-" + ss.tone;
    stateChipEl.title = ss.hint || "";
  }

  // --- git workflow actions (Commit / Push / PR / Merge) --------------------
  // The same guided flow the desktop pane header offers (lib/stage.ts
  // nextStep): each button hits the identical /api/instances/<title>/<action>
  // endpoint app.js uses, and the step recommended for the current stage is
  // highlighted so triage works from a phone.
  function currentInst() {
    for (var i = 0; i < instances.length; i++)
      if (instances[i].title === current) return instances[i];
    return null;
  }

  // Which button is the recommended next step for this stage (mirrors
  // stage.ts nextStep). null = no clear next step (busy / provisioning).
  function nextAct(inst) {
    if (!inst || inst.status === "loading" || inst.status === "paused") return null;
    if (inst.workspace_missing) return null;
    // "Back to idle" pin: the owner has said this branch's cycle is finished and
    // they are still working on it, so the recommended step is the start of the
    // ladder again — not Push/PR/Merge. Server-side (stage_reset.py), which is
    // what makes the phone and the desktop agree about it.
    if (inst.stage_reset) return "commit";
    switch (inst.stage || "agent") {
      case "agent": return "commit";
      case "interrupt": return "commit";     // re-commit after a pre-commit ✗
      case "committed": return inst.has_origin === false ? null : "push";
      case "pushed": return "pr";
      case "pr": return "merge";
      default: return null;                  // precommit (running) / provisioning
    }
  }

  function updateActions() {
    var inst = currentInst();
    var next = nextAct(inst);
    actionsEl.querySelectorAll(".act").forEach(function (btn) {
      var act = btn.dataset.act;
      btn.classList.toggle("is-next", act === next);
      btn.disabled = !inst;
    });
  }

  // POST an action endpoint, resolving the parsed JSON and rejecting with the
  // server's {error} message (same contract app.js's api() speaks). The error
  // also carries the HTTP status and the parsed body: a 409 here is a QUESTION
  // more often than a failure (a red zone, failing checks, a shared folder),
  // and the body is what says which.
  function postFor(title, action, body) {
    return fetch("/api/instances/" + encodeURIComponent(title) + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw apiError((j && j.error) || "request failed (" + r.status + ")", r.status, j);
        return j;
      });
    });
  }
  function postAction(action, body) {
    return postFor(current, action, body);
  }
  function apiError(msg, status, body) {
    var e = new Error(msg);
    e.status = status;
    e.body = body || {};
    return e;
  }

  // The red-zone gate's 409 (sessionActions.ts redZoneBreaches): the body's
  // `red_zone_breaches` list, or null for any other error.
  function redZoneBreaches(err) {
    if (!err || err.status !== 409) return null;
    var list = err.body && err.body.red_zone_breaches;
    return Array.isArray(list) && list.length ? list : null;
  }

  // A push / PR / merge the red-zone gate blocked, explained in the desktop's
  // words (sessionActions.ts offerRedZoneOverride), with the same "<verb>
  // anyway" that re-sends past the gate for this one action.
  function explainRedZone(title, verb, breaches, retry) {
    var n = breaches.length;
    var shown = breaches.slice(0, 5).map(function (b) {
      return b.path + (b.pattern && b.pattern !== b.path ? " (" + b.pattern + ")" : "");
    }).join(" · ");
    var green = breaches.every(function (b) { return b.kind === "green"; });
    askSheet(
      green ? verb + " blocked — changes outside the green zone" : verb + " blocked — red zone changed",
      nameOf(title) + " changed " + n + " " +
        (green ? "file" + (n === 1 ? "" : "s") + " outside its scope" : "protected file" + (n === 1 ? "" : "s")) +
        ": " + shown + (n > 5 ? " · and " + (n - 5) + " more" : "") +
        ". Revert them (the agent can: “revert your changes to …”), or " +
        verb.toLowerCase() + " anyway — this once.",
      retry ? verb + " anyway" : "", retry || null);
  }

  // `title` is captured at the tap and carried through every retry: a "Push
  // anyway" answered after the picker moved must push the session it was
  // asked about, not whichever one is on screen by then.
  function doPush(force, title, overrideRz) {
    title = title || current;
    if (!title) return;
    flashStatus("pushing…");
    var pushBody = force ? { force: true } : {};
    if (overrideRz) pushBody.override_red_zones = true;
    postFor(title, "push-branch", pushBody)
      .then(function () { flashStatus("pushed"); setTimeout(poll, 800); })
      .catch(function (err) {
        var rz = redZoneBreaches(err);
        if (rz) {
          setStatus("");
          explainRedZone(title, "Push", rz, function () { doPush(force, title, true); });
          return;
        }
        // O3 soft gate: checks haven't passed — offer an explicit override on
        // the ask sheet, exactly like the desktop pushSession flow.
        if (err.message === "checks haven't passed for this commit") {
          setStatus("");
          askSheet(
            "Push blocked — checks haven't passed",
            nameOf(title) + "'s commit has no passing check run (its state reads Checks failing). Override for this push only?",
            "Push anyway",
            function () { doPush(true, title, overrideRz); });
          return;
        }
        flashStatus("push failed: " + err.message);
      });
  }

  // The GitHub CLI is optional everywhere, including here. Pushing is plain
  // `git push` over whatever remote the user configured (SSH or HTTPS); only
  // opening/merging a PR needs credentials, and when the server has none it
  // answers 200 with ok:false plus a prefilled GitHub URL. So the phone shows a
  // link and a remedy, never a raw "gh is not installed" bounced back at it.
  var PR_REMEDY = "add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI";

  function doMakePr(title, overrideRz) {
    title = title || current;
    if (!title) return;
    flashStatus("opening PR…");
    postFor(title, "make-pr", overrideRz ? { override_red_zones: true } : {})
      .then(function (j) {
        if (j && j.ok === false) {
          if (j.compare_url) {
            window.open(j.compare_url, "_blank");
            flashStatus("opened GitHub’s compare page — " + (j.message || PR_REMEDY));
          } else {
            flashStatus(j.message || PR_REMEDY);
          }
          setTimeout(poll, 800);
          return;
        }
        flashStatus(j && j.note ? j.note : "PR opened");
        if (j && j.url) window.open(j.url, "_blank");
        // PR is open — restart the guided cycle, as the desktop does after
        // make-pr: the stage goes back to idle so Commit is the next step
        // again. Server-side (the ↺ route), so both screens agree.
        return postFor(title, "reset-stage", {})
          .catch(function () { /* the next push re-derives the stage anyway */ })
          .then(function () { setTimeout(poll, 800); });
      })
      .catch(function (err) {
        var rz = redZoneBreaches(err);
        if (rz) {
          setStatus("");
          explainRedZone(title, "Open PR", rz, function () { doMakePr(title, true); });
          return;
        }
        flashStatus("Open PR failed: " + err.message);
      });
  }

  // Asked first on the ask sheet — a merge can't be undone from MindFlock.
  function doMerge(confirmed, title, overrideRz) {
    title = title || current;
    if (!title) return;
    if (!confirmed) {
      askSheet(
        "Merge this PR?",
        nameOf(title) + "'s pull request merges on GitHub — this can't be undone from MindFlock.",
        "Merge PR",
        function () { doMerge(true, title); });
      return;
    }
    flashStatus("merging…");
    postFor(title, "merge-pr", overrideRz ? { override_red_zones: true } : {})
      .then(function (j) {
        if (j && j.ok === false) {
          if (j.pr_url) {
            window.open(j.pr_url, "_blank");
            flashStatus("opened the PR to merge on GitHub — " + (j.message || PR_REMEDY));
          } else {
            flashStatus(j.message || PR_REMEDY);
          }
        } else {
          flashStatus("merged");
        }
        setTimeout(poll, 800);
      })
      .catch(function (err) {
        var rz = redZoneBreaches(err);
        if (rz) {
          setStatus("");
          explainRedZone(title, "Merge", rz, function () { doMerge(true, title, true); });
          return;
        }
        flashStatus("merge failed: " + err.message);
      });
  }

  function openCommitSheet() {
    if (!current) return;
    commitSheet.classList.remove("hidden");
    commitText.focus();
  }
  function closeCommitSheet() {
    commitSheet.classList.add("hidden");
  }
  function submitCommit() {
    var msg = commitText.value.trim();
    if (!msg) { commitText.focus(); return; }
    closeCommitSheet();
    commitText.value = "";
    // Watch the pre-commit hooks run: flip to the shell tab (same as the
    // desktop CommitDialog switching to the terminal before submitting).
    switchTab("shell");
    flashStatus("committing…");
    doCommit(msg, false);
  }

  // A zone breach on your OWN window is a warning with "Commit anyway", like
  // the desktop's commit card — never a block.
  function doCommit(msg, proceed) {
    var body = proceed ? { message: msg, proceed_anyway: true } : { message: msg };
    postAction("commit", body)
      .then(function () { flashStatus("commit started — watch the shell"); setTimeout(poll, 800); })
      .catch(function (err) {
        if (err && err.status === 409 && err.body && err.body.zone_warning) {
          var hits = err.body.red_zone_breaches || [];
          setStatus("");
          askSheet(
            "Commit touches a zone",
            hits.slice(0, 5).map(function (h) { return h.path || h; }).join(" · ") +
              (hits.length > 5 ? " · and " + (hits.length - 5) + " more" : "") +
              ". Commit anyway?",
            "Commit anyway",
            function () { doCommit(msg, true); });
          return;
        }
        flashStatus("commit failed: " + err.message);
      });
  }

  // --- diff panel ------------------------------------------------------------
  // The desktop Diff tab, narrowed to what a phone can show: unified only (a
  // split view needs width that isn't there) and colorized the same way
  // (lib/diff.ts parseUnifiedDiff — "@@" cyan, "+" green, "-" red). Same
  // endpoint and same two baselines the desktop offers, so the numbers here
  // match the badge over there:
  //   base=fork  everything vs the branch's fork point (committed + not)
  //   base=head  uncommitted only
  var DIFF_MAX_LINES = 4000;   // a phone renders (and scrolls) no more usefully
  var diffBase = "fork";
  // Same localStorage key as the desktop's diff base (lib/diff.ts) — a phone is
  // a different browser, so this is just one name for one preference.
  try { diffBase = localStorage.getItem("mf_diffbase") === "head" ? "head" : "fork"; } catch (e) {}
  var diffSeq = 0;             // guards against a slow load overwriting a newer one

  // Diff text is repo content: build every line with textContent, never HTML.
  function diffNode(cls, text) {
    var d = document.createElement("div");
    d.className = cls;
    d.textContent = text;
    return d;
  }

  function diffNote(msg) {
    diffBody.innerHTML = "";
    diffBody.appendChild(diffNode("note", msg));
  }

  function setDiffStat(added, removed) {
    diffStat.innerHTML = "";
    var a = diffNode("add", "+" + (added || 0));
    var d = diffNode("del", "−" + (removed || 0));
    a.style.display = d.style.display = "inline";
    diffStat.appendChild(a);
    diffStat.appendChild(document.createTextNode("  "));
    diffStat.appendChild(d);
  }

  // Header lines git emits that a phone has no room for: the filename pulled
  // out of "diff --git" already says all of it, in a header that stays put.
  var DIFF_NOISE = /^(index |--- |\+\+\+ |new file mode|deleted file mode|old mode|new mode|similarity index|dissimilarity index|rename |copy )/;

  function renderDiff(content) {
    var frag = document.createDocumentFragment();
    var lines = content.split("\n");
    // A trailing newline yields one empty last element — not a diff line.
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    var shown = 0;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("diff --git ") === 0) {
        var m = line.match(/ b\/(.+)$/);
        frag.appendChild(diffNode("file", m ? m[1] : line.slice(11)));
        continue;
      }
      if (DIFF_NOISE.test(line)) continue;
      if (shown >= DIFF_MAX_LINES) {
        frag.appendChild(diffNode(
          "note",
          "… truncated at " + DIFF_MAX_LINES + " lines — open this session on " +
          "the desktop to read the rest."));
        break;
      }
      var c = line.charAt(0);
      var cls = line.indexOf("@@") === 0 ? "hunk"
              : c === "+" ? "add"
              : c === "-" ? "del" : "";
      // An empty context line still needs a box to occupy, hence the space.
      frag.appendChild(diffNode("dl " + cls, line || " "));
      shown++;
    }
    diffBody.innerHTML = "";
    diffBody.appendChild(frag);
    diffBody.scrollTop = 0;
  }

  function loadDiff() {
    if (view !== "diff") return;
    if (!current) { diffStat.textContent = ""; diffNote("No session selected."); return; }
    var seq = ++diffSeq;
    var title = current;
    diffStat.textContent = "loading…";
    fetch("/api/instances/" + encodeURIComponent(title) + "/diff?base=" + diffBase)
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || "diff failed (" + r.status + ")");
          return j;
        });
      })
      .then(function (j) {
        if (seq !== diffSeq) return;   // a newer load (or session) won
        if (j.error) { diffStat.textContent = ""; diffNote(j.error); return; }
        setDiffStat(j.added, j.removed);
        if (!(j.content || "").trim()) {
          diffNote(diffBase === "head"
            ? "No uncommitted changes."
            : "No changes on this branch yet.");
          return;
        }
        renderDiff(j.content);
      })
      .catch(function (err) {
        if (seq !== diffSeq) return;
        diffStat.textContent = "";
        diffNote((err && err.message) || "could not load the diff");
      });
  }

  function setDiffBaseLabel() {
    diffBaseBtn.textContent = diffBase === "head" ? "Uncommitted" : "All changes";
  }
  setDiffBaseLabel();

  // --- one session state ----------------------------------------------------
  // The rail's table (frontend/src/lib/sessionState.ts deriveSessionState),
  // copied word for word so the phone and the desktop say the same thing about
  // the same session. Six states, six tones; the tone names are the CSS classes
  // (tone-muted … tone-broken) the dot, the chip and the row words are painted
  // with.
  //
  //   state      tone       label
  //   starting   muted      Starting
  //   working    working    Working
  //   needs_you  attention  Question · Pre-commit failed · Checks failing · Setup failed
  //   idle       neutral    Idle
  //   limit      waiting    Waiting on limit[, resumes HH:MM]
  //   offline    broken     Offline (actions: Restart)
  //
  // Three rows sit outside the table, as on the rail: "Paused", "Missing" and
  // "Sleeping" (an agent stopped on purpose, idle, to free memory — opening
  // it or sending it anything wakes it) — none is a state of a running AGENT. No debounce here: the phone reads one
  // 4s poll, not the desktop's event stream.
  var SESSION_STATES = {
    starting:  { tone: "muted",     label: "Starting" },
    working:   { tone: "working",   label: "Working" },
    needs_you: { tone: "attention", label: "Question" },
    idle:      { tone: "neutral",   label: "Idle" },
    limit:     { tone: "waiting",   label: "Waiting on limit" },
    offline:   { tone: "broken",    label: "Offline" },
  };
  var REASON_LABEL = {
    question: "Question",
    precommit_failed: "Pre-commit failed",
    checks_failing: "Checks failing",
    setup_failed: "Setup failed",
    stuck: "Idle",
  };
  // Idle this long on uncommitted work and the rail's bell lists it as stuck.
  var STUCK_IDLE_S = 20 * 60;

  function fmtClock(epochS) {
    var d = new Date(epochS * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function st(state, extra) {
    var o = { state: state, tone: SESSION_STATES[state].tone, label: SESSION_STATES[state].label,
              inBell: false, restartable: false, hint: "" };
    for (var k in extra) o[k] = extra[k];
    return o;
  }
  function needsYou(reason, hint) {
    return st("needs_you", { reason: reason, label: REASON_LABEL[reason], inBell: true, hint: hint });
  }

  function sessionState(inst) {
    if (inst.workspace_missing)
      return st("offline", { label: "Missing", hint: "Workspace directory no longer exists — delete this session" });
    if (inst.status === "paused")
      return st("idle", { label: "Paused", tone: "muted", hint: "Session paused" });
    if (inst.pending || inst.status === "loading")
      return st("starting", { hint: "Provisioning workspace" });
    if (inst.sleeping)
      return st("idle", { label: "Sleeping", tone: "muted",
                          hint: "Asleep to free memory — opening it or sending it anything wakes it" });
    if (inst.setup && inst.setup.state === "running")
      return st("starting", { hint: "Worktree setup running" });
    if (inst.setup && inst.setup.state === "failed")
      return needsYou("setup_failed", "Worktree setup failed — prompts are held");
    var act = inst.activity || "idle";
    // The server's `asking` (a dialog, or a question in prose nobody has
    // typed to since) is a Question whatever the activity reads — a prose
    // question sits on an idle agent and is not "waiting for input".
    var asking = inst.asking;
    if (asking && (asking.kind === "dialog" || asking.kind === "question"))
      return needsYou("question", "The agent asked you a question" + (asking.text ? ": " + String(asking.text) : ""));
    if (act === "clarify") return needsYou("question", "The agent asked you a question");
    if (act === "working") return st("working", { hint: "Agent is working" });
    if (act === "limit") {
      var until = Number(inst.queue && inst.queue.limited_until) || 0;
      var resumeAt = until > Date.now() / 1000 ? until : 0;
      return st("limit", {
        label: SESSION_STATES.limit.label + (resumeAt ? ", resumes " + fmtClock(resumeAt) : ""),
        hint: "Usage limit reached — resumes on its own when the window resets",
      });
    }
    if (act === "offline")
      return st("offline", { inBell: true, restartable: !inst.device,
                             hint: "The agent's terminal is gone — Restart relaunches it" });
    if (inst.stage === "interrupt")
      return needsYou("precommit_failed", inst.failed_step ? "Pre-commit failed at: " + inst.failed_step : "Pre-commit failed");
    if (inst.check && inst.check.state === "failed" && !inst.check.stale)
      return needsYou("checks_failing", "Checks failed — push is gated until they pass (or push anyway)");
    var since = Number(inst.activity_since) || 0;
    var idleFor = since > 0 ? Date.now() / 1000 - since : 0;
    var un = inst.diff_stat && inst.diff_stat.uncommitted;
    var dirty = un && (Number(un.additions) || 0) + (Number(un.deletions) || 0) > 0;
    if (idleFor > STUCK_IDLE_S && dirty)
      return st("idle", { reason: "stuck", inBell: true, hint: "Idle with unfinished work — possibly stuck" });
    return st("idle", { hint: "Agent is idle — waiting for input" });
  }

  // O1: attention rank (lower = more urgent). The bell's order
  // (sessionState.ts attentionPriority): a question, then whatever is broken,
  // then failing checks; a pushed branch waiting for its PR comes after those,
  // and everything else last. A plan waiting for Go ranks with the broken ones
  // (the bell ranks it like an approval).
  function attnRank(inst) {
    var ss = sessionState(inst);
    if (ss.inBell) return ss.reason === "question" ? 0 : ss.reason === "checks_failing" ? 2 : 1;
    if (planPending(inst) && inst.status !== "paused" && !inst.workspace_missing) return 1;
    if (inst.status !== "paused" && inst.stage === "pushed") return 3;
    return 9;
  }
  // The row's marker: its tone class — the same colour the rail's dot wears.
  function attnMark(inst) {
    return "tone-" + sessionState(inst).tone;
  }

  // --- the rail's zone chip (lib/codemapSeen.ts redZoneChip), ported --------
  // Loudest first: breaches, a block since you last looked, a guard that isn't
  // holding, a plan waiting for Go, then the quiet shield / green check.
  // "Last looked" is the desktop's Map (mf_codemap_seen, when this browser has
  // one) or opening the session here (cs_mobile_rz_seen, stamped with the
  // block's own server time, so no clock skew). Tones match the rail's.
  function planPending(inst) {
    return Number(inst && inst.redzone && inst.redzone.plan_pending) || 0;
  }
  function rzSeenMap(key) {
    try {
      var v = JSON.parse(localStorage.getItem(key) || "{}");
      return v && typeof v === "object" ? v : {};
    } catch (e) { return {}; }
  }
  function rzSeenAt(title) {
    return Math.max(Number(rzSeenMap("mf_codemap_seen")[title]) || 0,
                    Number(rzSeenMap("cs_mobile_rz_seen")[title]) || 0);
  }
  function markRzSeen(inst) {
    var ts = Number(inst && inst.redzone && inst.redzone.last_block_ts) || 0;
    if (!ts || rzSeenAt(inst.title) >= ts) return;
    var seen = rzSeenMap("cs_mobile_rz_seen");
    seen[inst.title] = ts;
    try { localStorage.setItem("cs_mobile_rz_seen", JSON.stringify(seen)); } catch (e) {}
  }
  function rzChip(inst) {
    var rz = inst && inst.redzone;
    if (!rz || inst.pending) return null;
    var zones = rz.zones || 0, breaches = rz.breaches || 0, planned = planPending(inst);
    if (breaches > 0)
      return { cls: "rz-breach", label: "⛔" + breaches,
               title: breaches + " red-zone file" + (breaches === 1 ? "" : "s") +
                      " changed on this branch — pushing is blocked until reverted." };
    if ((rz.last_block_ts || 0) > rzSeenAt(inst.title))
      return { cls: "rz-breach", label: "⛔", title: "A red zone blocked an edit since you last looked." };
    if (zones > 0 && (rz.guard === "off" || rz.guard === "arming"))
      return { cls: "rz-warn", label: "!",
               title: rz.guard === "off"
                 ? "Red-zone guard is off — MindFlock is re-arming it. Edits are detected, not blocked, until then."
                 : "Red-zone guard is arming — it takes effect on the agent's next tool call." };
    if (planned > 0)
      return { cls: "rz-warn rz-plan", label: "plan · " + planned, title: "Plan ready — review & Go" };
    if (zones > 0 && rz.guard === "guarded" && rz.mode === "green")
      return { cls: "rz-ok", label: "✓", title: "Scoped to its green zones — edits outside them are blocked." };
    if (zones > 0 && rz.guard === "guarded")
      return { cls: "rz-ok", label: "🛡",
               title: zones + " red zone" + (zones === 1 ? "" : "s") + " guarded — edits there are blocked." };
    return null;
  }

  function poll() {
    return fetch("/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (list) {
        instances = Array.isArray(list) ? list : [];
        // Re-render when the title set OR any attention state changes, so
        // markers move without a session being added/removed.
        var sig = instances.map(function (i) {
          var rz = rzChip(i);
          return attnRank(i) + i.title + "\u0000" + (i.name || "") + "\u0000" + sessionState(i).label +
            "\u0000" + (rz ? rz.label : "") + "\u0000" + (i.asking ? String(i.asking.ts) + String(i.asking.text || "") : "");
        }).join("|");
        if (sig !== poll._sig) { poll._sig = sig; renderPicker(); }
        // After renderPicker, before the dot: a session created from the "+"
        // sheet is selected the tick it shows up, and both of those read the
        // selection.
        claimPending();
        pollApprovals();
        // ...and after claimPending, because the session it just selected is
        // exactly the one that is still provisioning.
        attachWhenReady();
        updateDot();
        updateActions();
        updatePlanCard();
        voiceOnPoll();
        askOnPoll();
      })
      .catch(function () { /* transient; next tick retries */ });
  }

  // --- your devices: approve a join from the phone --------------------------
  // GET /api/fleet lists the requests waiting on this device and the copies
  // it holds for your other devices (`via`): approving one here answers it
  // there (the server relays it under the devices' key, with the code shown
  // here). Only a phone signed in to this device sees them — the server
  // leaves them out for anyone else. A push's tap opens /m#approve=<id>,
  // which flashes that card (or says it's gone).
  var approveEl = document.getElementById("approve-cards");
  var approveWanted = (/(?:^|[#&])approve=([0-9a-f]{16})/.exec(location.hash) || [])[1] || "";
  var approveSig = "";
  var approveBusy = false;
  function renderApprovals(reqs) {
    if (!approveEl || approveBusy) return;
    var sig = reqs.map(function (r) { return r.id; }).join(",") + "|" + approveWanted;
    if (sig === approveSig) return;
    approveSig = sig;
    approveEl.innerHTML = "";
    var missing = approveWanted && !reqs.some(function (r) { return r.id === approveWanted; });
    if (!reqs.length && !missing) {
      approveEl.classList.add("hidden");
      fitSoon();
      return;
    }
    reqs.forEach(function (r) {
      var card = document.createElement("div");
      card.className = "approve-card" + (r.id === approveWanted ? " flash" : "");
      card.setAttribute("data-approve", r.id);
      var who = r.host || r.device;
      var line = document.createElement("div");
      line.textContent = who + " wants to join your devices";
      var code = document.createElement("div");
      code.className = "approve-code";
      code.textContent = r.code || "";
      var note = document.createElement("div");
      note.className = "approve-note";
      note.textContent = "Check " + who + " shows the same code." +
        (r.via ? " It asked " + (r.via_host || r.via) + "." : "");
      var btns = document.createElement("div");
      btns.className = "approve-btns";
      [["approve", "Approve"], ["deny", "Deny"]].forEach(function (b) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "act" + (b[0] === "approve" ? " is-next" : "");
        btn.textContent = b[1];
        btn.addEventListener("click", function () { answerRequest(r, b[0]); });
        btns.appendChild(btn);
      });
      card.appendChild(line);
      card.appendChild(code);
      card.appendChild(note);
      card.appendChild(btns);
      approveEl.appendChild(card);
    });
    if (missing) {
      var gone = document.createElement("div");
      gone.className = "approve-card approve-note";
      gone.textContent = "That join request is gone — it was answered, or it expired.";
      approveEl.appendChild(gone);
    }
    approveEl.classList.remove("hidden");
    fitSoon();
  }
  function answerRequest(r, decision) {
    approveBusy = true;
    fetch("/api/fleet/requests/" + encodeURIComponent(r.id) + "/" + decision, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ via: r.via || "", code: r.code || "" }),
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          setStatus(res.ok
            ? (decision === "approve" ? (r.host || r.device) + " is joining your devices" : "Denied " + (r.host || r.device))
            : (j && j.error) || "Couldn't answer (HTTP " + res.status + ")");
          setTimeout(function () { setStatus(""); }, 4000);
        });
      })
      .catch(function () { setStatus("Couldn't reach MindFlock"); })
      .then(function () {
        approveBusy = false;
        if (r.id === approveWanted) approveWanted = "";
        approveSig = "";
        pollApprovals();
      });
  }
  function pollApprovals() {
    if (!approveEl) return;
    fetch("/api/fleet")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        var reqs = (j && Array.isArray(j.requests)) ? j.requests : [];
        renderApprovals(reqs.filter(function (r) { return r && r.id; }));
      })
      .catch(function () { /* transient */ });
  }
  window.addEventListener("hashchange", function () {
    approveWanted = (/(?:^|[#&])approve=([0-9a-f]{16})/.exec(location.hash) || [])[1] || "";
    approveSig = "";
    pollApprovals();
  });

  // --- a plan waiting for Go (the Map's plan loop, from the phone) ----------
  // The row's `redzone.plan_pending` says the current session's agent wrote
  // its plan and waits; the card lists the plan (GET …/code-map/live, the
  // Map's own read) and gives the Map's two buttons: Go (naming the zones
  // added since the plan, as the Map's Go does) and Go — only the planned
  // files (POST …/code-map/go {scope_to_plan: true}). Built here, not in
  // mobile.html: it sits right under the approve cards.
  var PLAN_SHOWN = 8;
  var planEl = document.createElement("div");
  planEl.id = "plan-card";
  planEl.className = "hidden";
  planEl.setAttribute("aria-live", "polite");
  if (approveEl && approveEl.parentNode) approveEl.parentNode.insertBefore(planEl, approveEl.nextSibling);
  var planKey = "";
  var planBusy = false;
  // The plan a Go just answered: kept hidden while a poll still carries it.
  var planGone = { key: "", at: 0 };
  function hidePlanCard() {
    if (planEl.classList.contains("hidden")) return;
    planEl.classList.add("hidden");
    planEl.innerHTML = "";
    fitSoon();
  }
  function updatePlanCard() {
    if (!planEl) return;
    var inst = currentInst();
    if (inst) markRzSeen(inst);
    var n = inst && inst.status !== "paused" ? planPending(inst) : 0;
    if (!n) { planKey = ""; hidePlanCard(); return; }
    var key = inst.title + "|" + n;
    if (key === planGone.key && Date.now() - planGone.at < 15000) { hidePlanCard(); return; }
    if (key === planKey) { planButtons(); return; }
    planKey = key;
    var title = inst.title;
    // A `since` past any record keeps the feed out of the answer.
    fetch("/api/instances/" + encodeURIComponent(title) + "/code-map/live?since=1e12")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (planKey !== key) return;
        var plan = (j && j.plan) || {};
        var items = Array.isArray(plan.items) ? plan.items.filter(function (it) { return it && it.path; }) : [];
        var planTs = Number(plan.ts) || 0;
        var zones = (j && Array.isArray(j.zones)) ? j.zones : [];
        var goZones = zones.filter(function (z) {
          return z && z.id && !z.waived && (Number(z.created) || 0) > planTs;
        });
        renderPlanCard(title, n, items, goZones);
      })
      .catch(function () { if (planKey === key) planKey = ""; /* the next tick retries */ });
  }
  function renderPlanCard(title, n, items, goZones) {
    planEl.innerHTML = "";
    var card = document.createElement("div");
    card.className = "approve-card plan-card";
    var head = document.createElement("div");
    head.className = "plan-head";
    head.textContent = "Plan ready — review & Go";
    card.appendChild(head);
    var list = document.createElement("ul");
    list.className = "plan-items";
    items.slice(0, PLAN_SHOWN).forEach(function (it) {
      var li = document.createElement("li");
      var p = document.createElement("span");
      p.className = "plan-path";
      p.textContent = it.path + (it.new ? " (new)" : "");
      li.appendChild(p);
      if (it.intent) {
        var why = document.createElement("span");
        why.className = "plan-intent";
        why.textContent = " — " + it.intent;
        li.appendChild(why);
      }
      if (it.blocked || it.outside) {
        li.className = "plan-flag";
        li.title = it.blocked ? "In a red zone" : "Outside the green zones";
      }
      list.appendChild(li);
    });
    card.appendChild(list);
    var more = items.length - PLAN_SHOWN;
    var note = document.createElement("div");
    note.className = "approve-note";
    note.textContent = (items.length ? (more > 0 ? "+" + more + " more. " : "") : n + " planned file" + (n === 1 ? "" : "s") + ". ") +
      "Only the planned files also keeps the agent to them (green zones).";
    card.appendChild(note);
    var btns = document.createElement("div");
    btns.className = "approve-btns";
    var nz = goZones.length;
    var ids = goZones.map(function (z) { return z.id; });
    [[false, nz ? "Go — with " + nz + " zone" + (nz === 1 ? "" : "s") : "Go"],
     [true, "Go — only the planned files"]].forEach(function (b) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "act plan-go" + (b[0] ? "" : " is-next");
      btn.textContent = b[1];
      btn.addEventListener("click", function () { goPlan(title, ids, b[0]); });
      btns.appendChild(btn);
    });
    card.appendChild(btns);
    planEl.appendChild(card);
    planEl.classList.remove("hidden");
    planButtons();
    fitSoon();
  }
  // Like the Map's: no Go while the agent waits on a question in the terminal.
  function planButtons() {
    var inst = currentInst();
    var asking = !!inst && inst.activity === "clarify";
    planEl.querySelectorAll(".plan-go").forEach(function (btn) {
      btn.disabled = planBusy || asking;
      btn.title = asking ? "The agent is asking something in the terminal — answer it first" : "";
    });
  }
  function goPlan(title, zoneIds, scopeToPlan) {
    planBusy = true;
    planButtons();
    postFor(title, "code-map/go", { zone_ids: zoneIds, scope_to_plan: scopeToPlan })
      .then(function (j) {
        var told = j && j.told;
        flashStatus(told === "sent" ? "Sent to the agent."
          : told === "queued" ? "Queued — it goes to the agent when this turn ends."
          : "Couldn't reach the agent" + (j && j.reason ? ": " + j.reason : "."));
        if (told) {
          planGone = { key: planKey, at: Date.now() };
          hidePlanCard();
        }
      })
      .catch(function (err) { flashStatus((err && err.message) || "Couldn't reach MindFlock"); })
      .then(function () {
        planBusy = false;
        planKey = "";
        planButtons();
        setTimeout(poll, 800);
      });
  }

  // --- touch scrollback ------------------------------------------------------
  // The agent TUI runs on tmux's alt screen, so xterm's local scrollback is
  // empty — the history lives on the tmux side. Same trick as the desktop
  // grid's attachWheelScroll (app.js): translate a vertical one-finger drag
  // into SGR mouse-wheel ticks sent straight down the PTY; tmux (`mouse on`)
  // turns them into copy-mode scrolling. Natural direction — content follows
  // the finger. A drag only becomes a scroll once it is clearly vertical, so
  // a tap still focuses the terminal (and pops the soft keyboard) and
  // multi-touch gestures are left alone.
  function sendWheelTicks(clientX, clientY, up, count) {
    if (!term || !ws || ws.readyState !== WebSocket.OPEN) return false;
    var rect = termHost.getBoundingClientRect();
    var cols = term.cols || 80, rows = term.rows || 24;
    var cellW = rect.width / cols || 8, cellH = rect.height / rows || 16;
    var col = Math.max(1, Math.min(cols, Math.floor((clientX - rect.left) / cellW) + 1));
    var row = Math.max(1, Math.min(rows, Math.floor((clientY - rect.top) / cellH) + 1));
    var btn = up ? 64 : 65;
    var seq = "";
    for (var i = 0; i < count; i++) seq += "\x1b[<" + btn + ";" + col + ";" + row + "M";
    ws.send(seq);
    return true;
  }
  var touchLast = null, touchScrolling = false;
  termHost.addEventListener("touchstart", function (ev) {
    if (ev.touches.length !== 1) { touchLast = null; return; }  // pinch etc.
    touchLast = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
    touchScrolling = false;
  }, { capture: true, passive: true });
  termHost.addEventListener("touchmove", function (ev) {
    if (!touchLast || ev.touches.length !== 1) return;
    var t = ev.touches[0];
    var dy = t.clientY - touchLast.y, dx = t.clientX - touchLast.x;
    if (!touchScrolling) {
      if (Math.abs(dy) < 8 || Math.abs(dy) <= Math.abs(dx)) return;  // not (yet) a vertical drag
      touchScrolling = true;
    }
    var cellH = termHost.getBoundingClientRect().height / ((term && term.rows) || 24) || 16;
    // 3 ticks per cell dragged: a 1:1 finger-to-line mapping feels glacial on
    // a phone screen (the TUI scrolls one line per tick, and there are only
    // ~40 rows to a full swipe). 3x tracks a fast flick without overshooting
    // a slow precise drag.
    var TOUCH_SCROLL_MULT = 3;
    var lines = Math.floor(Math.abs(dy) * TOUCH_SCROLL_MULT / cellH);
    if (lines > 0) {
      // Finger moving down reveals earlier output = wheel up.
      if (!sendWheelTicks(t.clientX, t.clientY, dy > 0, Math.min(24, lines))) return;
      touchLast = { x: t.clientX, y: t.clientY };  // consume the emitted distance
    }
    ev.preventDefault();   // no page bounce, no synthetic tap after the drag
    ev.stopPropagation();
  }, { capture: true, passive: false });
  termHost.addEventListener("touchend", function () {
    touchLast = null; touchScrolling = false;
  }, { capture: true, passive: true });

  // --- compose box -----------------------------------------------------------
  // A normal phone text field: native paste / autocorrect / cursor handling,
  // then the whole line goes down the PTY at once, followed by Enter. Raw
  // keystroke mode (tap the terminal) and the soft-key bar keep working —
  // arrows/ctrl always act on the PTY directly, even while composing.
  var composeEl = document.getElementById("compose");
  var sendBtn = document.getElementById("send");
  function sendCompose() {
    if (voiceTakesSend()) return;   // voice mode: the draft goes through POST /send
    if (everythingTakesSend()) return;   // Everything: so does a typed one (say:true)
    var text = composeEl.value;
    if (text) {
      // Type the draft, then press Enter as a SEPARATE keystroke a beat
      // later: a TUI like claude treats a rapid burst as a paste, and a \r
      // inside a paste becomes a newline in its input box instead of a
      // submit. The gap ends the paste burst so the Enter actually sends.
      send(text);
      setTimeout(function () { send("\r"); }, 150);
    } else {
      send("\r");        // empty Send = bare Enter — confirms TUI prompts
    }
    composeEl.value = "";
    autosizeCompose();
    composeEl.focus();   // keep the keyboard up for the next message
  }
  // Grow with the draft (paste a paragraph and still see it) up to ~5 rows.
  function autosizeCompose() {
    composeEl.style.height = "auto";
    composeEl.style.height = Math.min(composeEl.scrollHeight, 120) + "px";
    fitSoon();           // the terminal shrinks/grows with the composer
  }
  composeEl.addEventListener("input", autosizeCompose);
  composeEl.addEventListener("keydown", function (ev) {
    // Enter sends (like a chat box); Shift+Enter inserts a newline in the
    // draft (hardware keyboards / multiline pastes).
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendCompose(); }
  });
  // pointerdown + preventDefault so tapping Send never blurs the compose box
  // (a blur would dismiss the soft keyboard between messages).
  sendBtn.addEventListener("pointerdown", function (ev) {
    ev.preventDefault();
    sendCompose();
  });

  // --- image paste -----------------------------------------------------------
  // A pasted image can't go down the PTY as keystrokes, and the agent CLI runs
  // on the server so it can't see this phone's clipboard either (same problem
  // the desktop grid solves in app.js). So the image bytes are uploaded to
  // /api/paste-image — saved inside the session's workspace — and the returned
  // PATH is what gets pasted: into the compose draft while composing, straight
  // down the PTY otherwise.
  function insertIntoCompose(text) {
    var s = composeEl.selectionStart == null ? composeEl.value.length : composeEl.selectionStart;
    var e = composeEl.selectionEnd == null ? s : composeEl.selectionEnd;
    composeEl.value = composeEl.value.slice(0, s) + text + composeEl.value.slice(e);
    composeEl.selectionStart = composeEl.selectionEnd = s + text.length;
    autosizeCompose();
  }
  var statusFlashTimer = null;
  function flashStatus(msg) {
    setStatus(msg);
    if (statusFlashTimer) clearTimeout(statusFlashTimer);
    statusFlashTimer = setTimeout(function () { setStatus(""); }, 2500);
  }
  function uploadPastedFile(file, composing) {
    flashStatus("uploading " + (file.name || "image") + "…");
    var q = current ? "?session=" + encodeURIComponent(current) : "";
    if (file.name) q += (q ? "&" : "?") + "name=" + encodeURIComponent(file.name);
    fetch("/api/paste-image" + q, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    })
      .then(function (r) {
        if (!r.ok) throw new Error("upload failed (" + r.status + ")");
        return r.json();
      })
      .then(function (j) {
        if (composing) { insertIntoCompose(j.path + " "); composeEl.focus(); }
        else send(j.path);
        flashStatus((file.name || "image") + " → " + j.path);
      })
      .catch(function (err) {
        flashStatus("file paste failed: " + ((err && err.message) || "error"));
      });
  }
  // Capture phase so this wins over xterm's own paste handling when the
  // clipboard holds a file (screenshot, or a file copied in a file manager);
  // plain-text pastes are left alone and take the native path (compose box)
  // or xterm's (terminal focused).
  document.addEventListener("paste", function (ev) {
    var items = ev.clipboardData && ev.clipboardData.items;
    if (!items) return;
    var file = null;
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        file = items[i].getAsFile();
        if (file) break;
      }
    }
    if (!file) return;
    ev.preventDefault();
    ev.stopPropagation();
    uploadPastedFile(file, document.activeElement === composeEl);
  }, true);
  // iPadOS/Android split-screen drag-drop: a file dragged from Files/Photos
  // uploads the same way; its saved path lands in the compose box so the
  // user sees what they're sending before it goes to the agent.
  document.addEventListener("dragover", function (ev) {
    var t = ev.dataTransfer && ev.dataTransfer.types;
    if (t && Array.prototype.indexOf.call(t, "Files") !== -1) ev.preventDefault();
  });
  document.addEventListener("drop", function (ev) {
    var files = ev.dataTransfer && ev.dataTransfer.files;
    if (!files || !files.length) return;
    ev.preventDefault();
    ev.stopPropagation();
    for (var i = 0; i < files.length; i++) uploadPastedFile(files[i], true);
  });

  // --- new session ("+") -----------------------------------------------------
  // Starting work from the phone. Until this, /m said "No sessions yet. Create
  // one from the desktop view." — the mobile head could drive every session on
  // the flock and start none of them.
  //
  // Two questions, one input each. Step 1 is a sentence; POST /api/session-plan
  // reads it and answers with the same form fields the desktop New Session
  // dialog owns. Step 2 is the review, where the only editable things are the
  // name and the first prompt. That is the whole feature, and the omissions are
  // the design: templates, the folder combobox, Browse, provisioning, workspace
  // strategy, launch flags, the account picker and the model pin all stay on the
  // desktop, because every one of them has a working default and the way to
  // inherit a default is to send no key at all.
  //
  // NO FOLDER NAME EVER REACHES POST /api/instances. Every folder on this page
  // is either a plan's `repo_path` (which the server resolved itself — the model
  // answers with the NUMBER of a row in a menu the server built by walking the
  // filesystem, never a path) or a row of GET /api/repos/suggest. There is
  // deliberately no free-text folder field: `_prepare_plain_repo` realpaths
  // whatever it is handed against the SERVER's cwd and then makedirs it, which
  // is how typing "api" into the desktop dialog once created a MindFlock/api
  // directory, and the only guard against that lives in the desktop's
  // TypeScript (isNameQuery). A page with no text field cannot reach it at all.
  var PLAN_MAX_CHARS = 2000;   // session_plan.MAX_SENTENCE; the box caps it too
  // 8s, the same moment the desktop's Describe button relabels itself. The plan
  // is a real model turn (~10-25s), which is long enough that a button which
  // never changes reads as a page that has hung.
  var NEW_SLOW_MS = 8000;
  // The desktop New dialog's own words (NewSessionDialog.tsx, round 5): "Start"
  // starts it now; "Let the agent pick the folder…" reads the sentence and shows the review.
  var NEW_GO_LABEL = "Let the agent pick the folder…";
  // How long a 202 has to turn into a row in the session list before we stop
  // claiming it is starting. A create answers 202 immediately and does the
  // worktree/provisioning/tmux work in a background task, so the row is usually
  // one 4s poll away — but a create that fails after its 202 emits
  // session.create_failed and NEVER joins the list, and nothing on this page
  // listens for that event. Bounded, so a failure ends in a sentence rather
  // than in a "starting…" that was never true.
  var PENDING_NEW_MS = 120000;

  var newSheet = document.getElementById("new-sheet");
  var newStep1 = document.getElementById("new-step1");
  var newStep2 = document.getElementById("new-step2");
  var newFolders = document.getElementById("new-folders");
  var newTextEl = document.getElementById("new-text");
  var newGoBtn = document.getElementById("new-go");
  var newTitleEl = document.getElementById("new-title");
  var newPromptEl = document.getElementById("new-prompt");
  var newFolderBtn = document.getElementById("new-folder");
  var newFolderNameEl = document.getElementById("new-folder-name");
  var newModeEl = document.getElementById("new-mode");
  var newNoteEl = document.getElementById("new-note");
  var newConfirmRow = document.getElementById("new-confirm-row");
  var newConfirmEl = document.getElementById("new-confirm");
  var newConfirmLabel = document.getElementById("new-confirm-label");
  var newStartBtn = document.getElementById("new-start");
  var newListEl = document.getElementById("new-folder-list");
  var newFoldersMsg = document.getElementById("new-folders-msg");
  var newErrEl = document.getElementById("new-error");
  var newDeviceRow = document.getElementById("new-device-row");
  var newDeviceEl = document.getElementById("new-device");
  var newFolderLabel = document.getElementById("new-folder-label");
  var newConfirmWhere = document.getElementById("new-confirm-where");
  var newSkipBtn = document.getElementById("new-skip");

  var newPlan = null;          // the answer being reviewed (model's or hand-picked)
  var planSeq = 0;             // cancels an in-flight plan; see closeNewSheet
  var planBusy = false;
  var planSlowTimer = null;
  var startBusy = false;
  // Cancels an in-flight CREATE the way planSeq cancels an in-flight plan. The
  // create POST is unsettled for as long as the network makes it (a phone that
  // lost signal can leave a fetch pending for a minute), and its answer must not
  // reach a sheet the user has since dismissed and reopened. See startSession.
  var startSeq = 0;
  var foldersBack = 1;         // screen the folder list was entered FROM
  var suggestHome = "";        // $HOME as /api/repos/suggest reported it
  var pendingNew = "";         // title we are waiting to see in the session list
  var pendingNewUntil = 0;
  // WHERE the session runs. "" is this device; anything else is the MagicDNS
  // key of a connected tailnet device, reached through the server's
  // /api/devices/<device>/fwd/ allow-list (backend/web/core/remote.py) — the
  // plan, the folder list and the create itself are all answered over THERE,
  // so the folders on offer are that device's and the session is born on it.
  // devicePick is the user's choice and survives the sheet closing (the same
  // way the desktop dialog keeps it), so a phone that always starts work on
  // the desktop tower does not have to say so every time.
  var devicePick = "";
  var newDevices = [];         // startable devices: connected, from GET /api/devices
  var selfHost = "";           // this server's own hostname, for the "(this device)" row
  // The device the CURRENT plan, folder list and create aim at. Captured when
  // Review details first is pressed, not read off the select later: the folder list and the
  // review screen both have to talk about the device the plan was read on.
  var newTarget = "";

  function newError(msg) {
    newErrEl.textContent = msg || "";
    newErrEl.classList.toggle("hidden", !msg);
  }

  function newStep(which) {
    newError("");
    newStep1.classList.toggle("hidden", which !== 1);
    newStep2.classList.toggle("hidden", which !== 2);
    newFolders.classList.toggle("hidden", which !== 3);
  }

  // The path that reaches `path` on `device` ("" = here, unchanged). Mirrors
  // frontend/src/lib/devices.ts devicePath.
  function devicePath(device, path) {
    return device ? "/api/devices/" + encodeURIComponent(device) + "/fwd" + path : path;
  }

  // A session's title as THIS server lists it: another device's sessions are
  // namespaced <device>::<title> in GET /api/instances.
  function deviceTitle(device, title) {
    return device ? device + "::" + title : title;
  }

  // What to call a device: its hostname, unless that is ambiguous (another
  // device, or this one, shares it) — then the unique MagicDNS key. Mirrors
  // the desktop's deviceLabel.
  function deviceLabel(key) {
    if (!key) return selfHost || "this device";
    var d = null, clash = 0;
    for (var i = 0; i < newDevices.length; i++) {
      if (newDevices[i].device === key) d = newDevices[i];
    }
    if (!d || !d.host) return key;
    for (var j = 0; j < newDevices.length; j++) {
      if ((newDevices[j].host || "") === d.host) clash += 1;
    }
    return (clash > 1 || d.host === selfHost) ? key : d.host;
  }

  // The device the select is pointing at right now, "" whenever the pick is no
  // longer startable (it went offline, or was disconnected): a session aimed at
  // a device that can't answer would only fail at Start.
  function pickedDevice() {
    for (var i = 0; i < newDevices.length; i++) {
      if (newDevices[i].device === devicePick) return devicePick;
    }
    return "";
  }

  function renderDevices() {
    newDeviceEl.innerHTML = "";
    var here = document.createElement("option");
    here.value = "";
    here.textContent = (selfHost || "This device") + " (this device)";
    newDeviceEl.appendChild(here);
    for (var i = 0; i < newDevices.length; i++) {
      var o = document.createElement("option");
      o.value = newDevices[i].device;
      // textContent: hostnames come off the network.
      o.textContent = deviceLabel(newDevices[i].device);
      newDeviceEl.appendChild(o);
    }
    newDeviceEl.value = pickedDevice();
    // Only once a second device is there to pick.
    newDeviceRow.classList.toggle("hidden", !newDevices.length);
  }

  // Refreshed on every opening — devices come and go (a laptop lid closes) and
  // the list is a cheap in-memory read on the server. Failure just leaves the
  // picker hidden, which is the single-device sheet this page always had.
  function loadDevices() {
    fetch("/api/devices")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        selfHost = (j && j.self && j.self.host) || "";
        var all = (j && j.devices) || [];
        newDevices = [];
        for (var i = 0; i < all.length; i++) {
          if (all[i] && all[i].connected && all[i].device) newDevices.push(all[i]);
        }
        renderDevices();
      })
      .catch(function () { newDevices = []; renderDevices(); });
  }

  function openNewSheet() {
    // A fresh sheet every time. A half-reviewed plan left over from the last
    // opening is indistinguishable from this one's, and the folder it names is
    // almost certainly not the folder this sentence is about.
    newPlan = null;
    newTextEl.value = "";
    setPlanBusy(false, NEW_GO_LABEL);
    // The Start button too. Nothing the create's answer does resets it when
    // that answer is stale, so a sheet dismissed mid-create used to reopen with
    // "Starting…" disabled forever (startSession returns at
    // `if (startBusy) return;`) and no way out but reloading the page.
    resetStartBtn();
    renderDevices();
    loadDevices();
    newSheet.classList.remove("hidden");
    newStep(1);
    newTextEl.focus();
  }

  function closeNewSheet() {
    // The seq bump is what actually cancels an in-flight plan: it makes the
    // answer a no-op whenever it lands. The subprocess on the far side is not
    // killed by this and runs to its own timeout — it is a read-only one-shot
    // with stdin closed, so that is bounded and harmless rather than something
    // worth building a kill channel for.
    planSeq += 1;
    // Same for the create: bumping this is what makes a 202 (or a failure) that
    // lands after the dismissal a no-op instead of closing — or writing the
    // previous session's error into — a sheet the user has reopened and is
    // typing in. The session itself is still created; only the sheet is off
    // limits.
    startSeq += 1;
    if (planSlowTimer) { clearTimeout(planSlowTimer); planSlowTimer = null; }
    setPlanBusy(false, NEW_GO_LABEL);
    resetStartBtn();
    newSheet.classList.add("hidden");
    newPlan = null;
    newError("");
  }

  function setPlanBusy(on, label) {
    planBusy = on;
    newGoBtn.disabled = on;
    if (!on) newSkipBtn.classList.add("hidden");
    // The plan in flight is being read on the device picked when Review was
    // pressed; a select that still moved would be pointing somewhere else.
    newDeviceEl.disabled = on;
    newGoBtn.textContent = label;
  }

  function resetStartBtn() {
    startBusy = false;
    newStartBtn.disabled = false;
    newStartBtn.textContent = "Start";
  }

  // The ~-relative spelling, for DISPLAY only — mirrors session_plan._tilde and
  // the frontend's homeRelative, boundary-safe so /home/ann-old is never
  // shortened against /home/ann. Unlike the server's version this falls back to
  // the path as-is for a folder outside home: that rule exists because the
  // server's spelling goes into a model's prompt, and nothing here does.
  function homeRel(path) {
    var p = String(path || "");
    var h = String(suggestHome || "");
    if (!h) return p;
    if (p === h) return "~";
    if (p.indexOf(h + "/") === 0) return "~/" + p.slice(h.length + 1);
    return p;
  }

  // ONE muted line, composed here from the resolved fields rather than lifted
  // out of the server's note. The note is a paragraph about the whole plan; this
  // is the single fact both screens are really asking about — where the work
  // lands — and it has to be readable without reading the paragraph.
  function modeLine(p) {
    if (!p.folder_exists) {
      return "A new folder" +
        (p.init_repo ? ", with a git repo created inside it" : "") + " — " +
        (p.in_place ? "work happens in the folder directly."
                    : "work happens in a new worktree.");
    }
    return p.in_place
      ? "Work happens in the folder directly."
      : "Work happens in a new worktree, not in the folder itself.";
  }

  // The note's first sentence is the desktop dialog's preamble, and it names a
  // button that does not exist on this page ("press Create" — here it says
  // Create session, under a heading that already asks the question). Everything
  // AFTER it is the resolved facts — which folder, which mode, the "I wasn't
  // certain" clause, the cut-short search — which is the whole reason to show
  // the note. Dropped by EXACT prefix, so the day note_for's wording changes
  // the sentence simply reappears instead of this quietly eating a different
  // one. The server owns that sentence and is right to: the note is composed
  // from resolved facts precisely so no client can rewrite it.
  var NOTE_PREAMBLE = "Filled in from what you typed — check it and press Create. ";
  function planNote(note) {
    var t = String(note || "");
    return t.indexOf(NOTE_PREAMBLE) === 0 ? t.slice(NOTE_PREAMBLE.length) : t;
  }

  // Whatever is in the two editable boxes is what the plan IS from here on.
  // Called before every screen change out of step 2, so correcting the folder
  // (or going Back and re-describing) never quietly reverts a name the user
  // had already fixed.
  function harvestPlan() {
    if (!newPlan) return;
    newPlan.title = newTitleEl.value.trim();
    newPlan.prompt = newPromptEl.value;
  }

  function showPlan(p) {
    newPlan = p;
    newFolderLabel.textContent = p.device ? "Folder on " + deviceLabel(p.device) : "Folder";
    newConfirmWhere.textContent = p.device ? deviceLabel(p.device) : "this machine";
    newTitleEl.value = p.title || "";
    newPromptEl.value = p.prompt || "";
    newFolderNameEl.textContent = p.folder_display || p.repo_path || "";
    newModeEl.textContent = modeLine(p);
    newNoteEl.textContent = planNote(p.note);
    newNoteEl.classList.toggle("hidden", !newNoteEl.textContent);
    // THE CONFIRM GATE, re-armed. Unticked for every plan that lands, including
    // a second plan naming the same folder: the tick has to mean "I read THIS
    // folder name and said yes", and a tick that survives a re-describe is the
    // previous sentence's yes standing in for this one's.
    newConfirmEl.checked = false;
    newConfirmLabel.textContent =
      "Create the folder " + (p.folder_display || p.repo_path || "");
    newConfirmRow.classList.toggle("hidden", !!p.folder_exists);
    newStep(2);
  }

  // Ask the server to read the sentence. Failure is not an error state here —
  // /api/session-plan answers 502 with one human sentence when there is no CLI
  // to ask, when it times out, or when the answer can't be read, and on a phone
  // that cannot be a dead end, so the failure IS the folder list.
  function describeIt() {
    var text = newTextEl.value.trim();
    if (!text) { newTextEl.focus(); return; }
    if (planBusy) return;
    // The previous plan goes NOW, not when the new one lands. Keeping it meant a
    // plan that FAILED (502 → the folder list) left the previous one reviewable
    // behind a single Back tap: step 2 showing the first sentence's name, folder
    // and prompt — showPlan never ran on that path — and Start creating THAT
    // session while the sentence just typed was silently discarded. The tick
    // goes with it: a confirm box still ticked over a plan the user did not
    // review is a confirm-gate failure, not merely a navigation one.
    newPlan = null;
    newConfirmEl.checked = false;
    newTarget = pickedDevice();
    var dev = newTarget;
    var seq = ++planSeq;
    newError("");
    setPlanBusy(true, "Reading…");
    planSlowTimer = setTimeout(function () {
      if (seq !== planSeq) return;
      newGoBtn.textContent = "Still reading…";
      // A read can take as long as the target's model turn — over a minute on
      // a slow device. Never leave the phone with nothing to do but wait.
      newSkipBtn.classList.remove("hidden");
    }, NEW_SLOW_MS);
    fetch(devicePath(dev, "/api/session-plan"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, PLAN_MAX_CHARS) }),
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || "couldn't read that (" + r.status + ")");
          return j;
        });
      })
      .then(function (j) {
        if (seq !== planSeq) return;
        planDone(seq);
        showPlan({
          device: dev,
          title: j.title || "",
          repo_path: j.repo_path || "",
          // The sentence is the session's first prompt when the model didn't
          // write one — it is already an instruction, and silently starting an
          // agent with nothing to do is worse than starting it with the words
          // that asked for it.
          prompt: j.prompt || text,
          // An absent key means in-place, at both ends: of the two ways to be
          // wrong about a missing value, only `false` opens a worktree and
          // writes a branch into somebody's repo.
          in_place: j.in_place !== false,
          init_repo: !!j.init_repo,
          // And an absent key here means the folder is NOT there — the fail-safe
          // direction, because the cost of being wrong is one extra tick on a
          // folder that already exists, while the other way round is a directory
          // created on the user's disk without anyone confirming it. The wire
          // contract always sends this key; this only fires against a server
          // older than the gate.
          folder_exists: j.folder_exists === true,
          folder_display: j.folder_display || j.repo_path || "",
          note: j.note || "",
        });
      })
      .catch(function (err) {
        if (seq !== planSeq) return;
        planDone(seq);
        loadFolders(((err && err.message) || "couldn't read that") +
                    " — pick the folder yourself.", 1);
      });
  }

  function planDone(seq) {
    if (seq !== planSeq) return;
    if (planSlowTimer) { clearTimeout(planSlowTimer); planSlowTimer = null; }
    setPlanBusy(false, NEW_GO_LABEL);
  }

  // "Pick the folder myself": abandon the read in flight (its answer, if it
  // ever comes, is dropped by the planSeq check) and go to the folder list on
  // the device the read was aimed at.
  function skipPlan() {
    if (!planBusy) return;
    var seq = ++planSeq;
    planDone(seq);
    loadFolders("", 1);
  }

  function folderNote(msg) {
    var d = document.createElement("div");
    d.className = "new-muted";
    d.textContent = msg;
    return d;
  }

  function folderRow(row) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "new-row new-folder-row";
    var name = document.createElement("span");
    // textContent, never innerHTML — these are directory names off the user's
    // own disk, the same rule the diff panel holds for repo content.
    name.textContent = row.name || row.path || "";
    var hint = document.createElement("span");
    hint.className = "new-row-hint";
    hint.textContent = homeRel(row.path) + (row.is_git ? "  ·  git" : "");
    b.appendChild(name);
    b.appendChild(hint);
    b.addEventListener("click", function () { pickFolder(row); });
    return b;
  }

  // The folder list: the no-model fallback AND the correction path. Every row
  // came out of a walk of the filesystem the SERVER did (the same suggestions
  // the desktop dialog's chips show), which is exactly what makes tapping one
  // safe to hand straight to create as a path.
  function loadFolders(msg, from) {
    // Where Back goes, TRACKED rather than inferred. It used to be
    // `newStep(newPlan ? 2 : 1)` — "is there a plan" standing in for "which
    // screen did I come from" — which is how the fallback list (entered from
    // step 1, after a failed plan) sent the user "back" to a review screen for a
    // plan they had already replaced.
    foldersBack = from === 2 ? 2 : 1;
    newStep(3);
    newFoldersMsg.textContent = msg || "";
    newListEl.innerHTML = "";
    newListEl.appendChild(folderNote("loading…"));
    // The TARGET device's folders: a path off this disk names nothing there.
    fetch(devicePath(newTarget, "/api/repos/suggest"))
      .then(function (r) { return r.json(); })
      .then(function (j) {
        suggestHome = (j && j.home) || "";
        var rows = (j && j.suggestions) || [];
        newListEl.innerHTML = "";
        if (!rows.length) {
          newListEl.appendChild(folderNote(
            "No folders found on " + (newTarget ? deviceLabel(newTarget) : "this machine") +
            " — make one on the desktop first."));
          return;
        }
        for (var i = 0; i < rows.length; i++)
          newListEl.appendChild(folderRow(rows[i]));
      })
      .catch(function () {
        newListEl.innerHTML = "";
        newListEl.appendChild(folderNote("couldn't read the folder list"));
      });
  }

  // A hand-picked folder becomes the same shape a plan has, so step 2 and Start
  // have exactly one kind of thing to read.
  function pickFolder(row) {
    var git = !!row.is_git;
    showPlan({
      device: newTarget,
      title: (newPlan && newPlan.title) || row.name || "",
      repo_path: row.path || "",
      prompt: (newPlan && newPlan.prompt) || newTextEl.value.trim(),
      // Mirrors the create route's own clamp instead of hoping: a non-git folder
      // has no HEAD to fork a worktree from, so the server forces in-place, and
      // a review screen that said "worktree" over it would be promising
      // something the 202 quietly does not do. A plan's worktree choice survives
      // a folder correction; a bare pick defaults in-place for the same reason
      // the absent key does.
      in_place: newPlan ? (!!newPlan.in_place || !git) : true,
      // Never ticked for a folder the user chose off a list of folders that
      // already exist: git init here is a decision nobody made.
      init_repo: false,
      // Every row of this list is a directory the server found by walking the
      // disk, so there is nothing here to confirm the creation of.
      folder_exists: true,
      folder_display: homeRel(row.path),
      note: "",
    });
  }

  // Why Start can't run yet, or "" when it can. A sentence rather than a
  // disabled button: a greyed-out control with no explanation is the version of
  // a safety gate that people learn to ignore, and the gate's whole job is to be
  // read.
  function startBlockReason(p) {
    if (!p) return "say what you want to work on first";
    // A folder NAME must never reach POST /api/instances (see this section's
    // header). Unreachable by construction — a plan's repo_path is absolute by
    // contract and every list row came from the server's own walk — and checked
    // anyway, because the failure it guards is a directory created in whatever
    // the server's cwd happens to be.
    if (!p.repo_path || p.repo_path.charAt(0) !== "/")
      return "that folder didn't come back as a real path — pick one from the list";
    // THE CONFIRM GATE. Creating a directory is the one thing a plan proposes
    // that outlives the session and that no later undo reaches: closing a
    // session removes its worktree, but nobody ever comes back for the folder.
    // So a folder that is not there yet has to be agreed to in as many words,
    // and the refusal names the folder the same way the tick does.
    if (!p.folder_exists && !newConfirmEl.checked)
      return "This would create " + (p.folder_display || p.repo_path) +
        ", which isn't there yet — tick the box to confirm that folder first.";
    return "";
  }

  function startSession() {
    if (startBusy) return;
    harvestPlan();
    var blocked = startBlockReason(newPlan);
    if (blocked) { newError(blocked); return; }
    var p = newPlan;
    var seq = ++startSeq;
    startBusy = true;
    newStartBtn.disabled = true;
    newStartBtn.textContent = "Starting…";
    newError("");
    fetch(devicePath(p.device, "/api/instances"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // FIVE KEYS, and no more. `program`, `launch_args`, `profile_id`,
      // `profile_model`, `provisioned` and `workspace_strategy` are absent on
      // purpose: an absent key inherits whatever the user configured on the
      // desktop, which is the entire reason the phone never has to ask about
      // any of them.
      body: JSON.stringify({
        title: p.title,
        repo_path: p.repo_path,
        prompt: p.prompt,
        in_place: !!p.in_place,
        init_repo: !!p.init_repo,
      }),
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || "create failed (" + r.status + ")");
          return j;
        });
      })
      .then(function (j) {
        // 202: the instance registers as Loading and its real start (worktree,
        // provisioning, tmux) runs in a background task, so the row arrives in
        // the list a poll later. The TITLE comes back from the server because an
        // auto-named session can be re-numbered under the engine lock, and
        // waiting for a name we invented would be waiting for a row that never
        // appears.
        var title = (j && j.title) || p.title;
        // The session exists whatever the sheet is doing now, so it is still
        // tracked and selected when it lands. On another device the 202 names
        // the session the way THAT device knows it; this server lists it
        // namespaced, and that is the row to wait for.
        pendingNew = deviceTitle(p.device, title);
        pendingNewUntil = Date.now() + PENDING_NEW_MS;
        flashStatus("starting " + title +
                    (p.device ? " on " + deviceLabel(p.device) : "") + "…");
        setTimeout(poll, 800);
        // But the SHEET belongs to whoever is using it. Tap Start, dismiss the
        // sheet while the POST is in flight, tap "+" and start typing: without
        // this guard the 202 closed the sheet under the user and threw away the
        // sentence they were writing. closeNewSheet already reset the button.
        if (seq !== startSeq) return;
        resetStartBtn();
        closeNewSheet();
      })
      .catch(function (err) {
        // The sheet STAYS OPEN. A 409 on a name that already exists is fixed by
        // editing the name that is on screen, and #status lives behind this
        // sheet where nobody could read it anyway. Unless it is not this
        // create's sheet any more — the mirror image of the .then guard above,
        // and without it the PREVIOUS session's create failure is written into
        // the freshly reopened sheet.
        if (seq !== startSeq) return;
        resetStartBtn();
        newError((err && err.message) || "could not start the session");
      });
  }

  // Select the session we just created, once it exists. Done from the poll tick
  // rather than from the create's own .then because the 202 is an acceptance,
  // not an arrival — selecting a title that isn't in the picker yet would
  // connect a websocket to a session tmux hasn't been told about.
  function claimPending() {
    if (!pendingNew) return;
    for (var i = 0; i < instances.length; i++) {
      if (instances[i].title === pendingNew) {
        var title = pendingNew;
        pendingNew = "";
        // renderPicker has already run for this tick (a new title always moves
        // the signature), so the row exists to be selected.
        select(title);
        return;
      }
    }
    if (Date.now() > pendingNewUntil) {
      var gone = pendingNew;
      pendingNew = "";
      flashStatus(gone + " didn't start — open it on the desktop to see why");
    }
  }

  // The other half of the loading guard. A poll tick is the only thing on this
  // page that re-fires by itself, so it is what notices provisioning finished
  // and attaches the terminal — the picker cannot, for all the reasons select()
  // lists. Bounded by nothing on purpose: the row leaves "loading" either way
  // (it starts, it fails and disappears, and claimPending's own deadline covers
  // the create that never joins the list at all).
  function attachWhenReady() {
    if (!awaitingReady) return;
    // Selection moved on; that select() already decided whether to connect.
    if (awaitingReady !== current) { awaitingReady = ""; return; }
    if (isLoading(current)) return;
    // Paused: the Paused line stays up; attach once Resume flips the status.
    if (agentPaused(current)) return;
    awaitingReady = "";
    connect();                 // onopen clears the provisioning line
  }

  document.getElementById("new-btn").addEventListener("click", openNewSheet);
  emptyEl.addEventListener("click", openNewSheet);
  newGoBtn.addEventListener("click", describeIt);
  newSkipBtn.addEventListener("click", skipPlan);
  document.getElementById("new-cancel").addEventListener("click", closeNewSheet);
  newStartBtn.addEventListener("click", startSession);
  document.getElementById("new-back").addEventListener("click", function () {
    harvestPlan();
    newStep(1);
  });
  newDeviceEl.addEventListener("change", function () {
    devicePick = newDeviceEl.value || "";
  });
  newFolderBtn.addEventListener("click", function () {
    harvestPlan();
    loadFolders("Pick the folder this session should work in.", 2);
  });
  document.getElementById("new-folders-back").addEventListener("click", function () {
    newStep(foldersBack);
  });
  // Ticking the gate clears the refusal it caused — leaving "tick the box
  // first" on screen under a ticked box is the sheet arguing with itself.
  newConfirmEl.addEventListener("change", function () {
    if (newConfirmEl.checked) newError("");
  });
  // Enter continues, Shift+Enter keeps editing — the same bargain the compose
  // box strikes, because this is the same kind of box on the same keyboard.
  newTextEl.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); describeIt(); }
  });
  // Tap the dimmed backdrop to dismiss, like the commit sheet.
  newSheet.addEventListener("click", function (ev) {
    if (ev.target === newSheet) closeNewSheet();
  });
  // Every field in here is one the soft keyboard can bury; focusin/focusout
  // bubble, so one pair of listeners covers all of them.
  newSheet.addEventListener("focusin", nudgeViewport);
  newSheet.addEventListener("focusout", nudgeViewport);

  // --- sessions, session actions, recently closed, questions ------------------
  // What the rail's row menu does, on a phone: open a session, Close it (keeps
  // the worktree), Delete it (asked first, inline), Resume it if paused, Restart
  // it when it is offline, and bring a closed one back. Every question is a
  // sheet on this page — confirm() and friends are a silent "no" in the
  // desktop app and in some phone shells.
  var sessionsSheet = document.getElementById("sessions-sheet");
  var actSheet = document.getElementById("act-sheet");
  var actTitleEl = document.getElementById("act-title");
  var actStateEl = document.getElementById("act-state");
  var actListEl = document.getElementById("act-list");
  var actErrEl = document.getElementById("act-error");
  var recentSheet = document.getElementById("recent-sheet");
  var recentListEl = document.getElementById("recent-list");
  var recentErrEl = document.getElementById("recent-error");
  var askSheetEl = document.getElementById("ask-sheet");
  var askTitleEl = document.getElementById("ask-title");
  var askDetailEl = document.getElementById("ask-detail");
  var askOkBtn = document.getElementById("ask-ok");
  var askCancelBtn = document.getElementById("ask-cancel");

  function openSheet(el) { el.classList.remove("hidden"); }
  function closeSheet(el) { el.classList.add("hidden"); }

  function openSessionsSheet() {
    renderRows();   // closed: a fresh sort, then held while open
    document.getElementById("sessions-voice-note").classList.toggle("hidden", voice.state === "off");
    askNotifyRender();
    openSheet(sessionsSheet);
  }

  // --- the ask sheet: one question, or one gate explained ---
  var askRun = null;
  function askSheet(title, detail, okLabel, onOk) {
    askTitleEl.textContent = title;
    askDetailEl.textContent = detail || "";
    askRun = okLabel ? onOk : null;
    askOkBtn.textContent = okLabel || "";
    askOkBtn.classList.toggle("hidden", !okLabel);
    // With nothing to do but read it, the one button is an acknowledgement.
    askCancelBtn.textContent = okLabel ? "Cancel" : "OK";
    openSheet(askSheetEl);
    // Focus lands on Cancel, as on the desktop's Delete strip: a second tap of
    // whatever opened this never answers it.
    askCancelBtn.focus();
  }
  function closeAsk() {
    askRun = null;
    closeSheet(askSheetEl);
  }

  // --- one session's actions ---
  var actFor = "";            // the title the open actions sheet is about
  var actBusy = false;
  // A long press opens this sheet while the finger is still down; the release
  // that follows must not land as a tap on whatever row is now under it.
  var actOpenedAt = 0;
  var ACT_SETTLE_MS = 400;
  function actSettled() { return Date.now() - actOpenedAt > ACT_SETTLE_MS; }

  function instByTitle(title) {
    for (var i = 0; i < instances.length; i++)
      if (instances[i].title === title) return instances[i];
    return null;
  }

  function actError(msg) {
    actErrEl.textContent = msg || "";
    actErrEl.classList.toggle("hidden", !msg);
  }

  // "Folder in use by a, b. Close those first." — the rail's sentence
  // (sessionActions.ts folderInUseText) for the 409 Delete answers while a
  // copy of the session still works in the same folder.
  function folderInUseText(titles) {
    return "Folder in use by " + (titles.length ? titles.join(", ") : "another session") + ". Close those first.";
  }
  function folderInUse(err) {
    if (!err || err.status !== 409 || !err.body || err.body.error !== "folder in use") return null;
    return Array.isArray(err.body.sessions) ? err.body.sessions.map(String) : [];
  }
  function keptRunningText(j) {
    var n = (j && Array.isArray(j.children)) ? j.children.length : 0;
    if (!n) return "";
    return " · " + (n === 1 ? "1 worker session kept running" : n + " worker sessions kept running");
  }

  function actRow(label, hint, run, extraCls) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "new-row act-row" + (extraCls ? " " + extraCls : "");
    var main = document.createElement("span");
    main.className = "act-label";
    main.textContent = label;
    b.appendChild(main);
    if (hint) {
      var h = document.createElement("span");
      h.className = "act-hint";
      h.textContent = hint;
      b.appendChild(h);
    }
    b.addEventListener("click", function () { if (!actBusy && actSettled()) run(b); });
    return b;
  }

  function openActSheet(title) {
    actFor = title || "";
    actBusy = false;
    actOpenedAt = Date.now();
    actError("");
    renderActSheet();
    closeSheet(sessionsSheet);
    openSheet(actSheet);
  }
  function closeActSheet() {
    actFor = "";
    closeSheet(actSheet);
  }

  function renderActSheet() {
    actListEl.innerHTML = "";
    var title = actFor;
    var inst = title ? instByTitle(title) : null;
    actTitleEl.textContent = title ? nameOf(title) : "No session open";
    actStateEl.innerHTML = "";
    if (inst) {
      var ss = sessionState(inst);
      var chip = document.createElement("span");
      chip.className = "state-chip tone-" + ss.tone;
      chip.textContent = ss.label;
      actStateEl.appendChild(chip);
      if (ss.hint) {
        var h = document.createElement("span");
        h.className = "act-hint";
        h.textContent = "  " + ss.hint;
        actStateEl.appendChild(h);
      }
      if (ss.restartable)
        actListEl.appendChild(actRow("Restart", "relaunches the agent, resuming the conversation",
          function () { sessionCall(title, "revive", "Restarting " + nameOf(title) + "…", "Restart failed"); }));
      if (inst.sleeping && !inst.device && inst.status !== "paused")
        actListEl.appendChild(actRow("Wake", "relaunches the sleeping agent, resuming the conversation",
          function () { sessionCall(title, "wake", "Waking " + nameOf(title) + "…", "Wake failed"); }));
      // No Pause here, as on the desktop's menus (the palette, the CLI and the
      // API still pause); a paused session is resumed from this sheet.
      if (inst.status === "paused")
        actListEl.appendChild(actRow("Resume session", "starts the agent again in the same worktree",
          function () { sessionCall(title, "resume", "Resuming " + nameOf(title) + "…", "Resume failed"); }));
      actListEl.appendChild(actRow("Close", "stops the agent and moves it to Recently closed",
        function () { closeSession(title); }));
      // A peer (sandboxed shared-folder) session is unshared, never deleted —
      // the rail hides Delete for it too.
      if (!inst.peer_share)
        actListEl.appendChild(actRow("Delete…", "", function (btn) { askDelete(title, btn); }, "act-danger"));
    } else if (title) {
      actListEl.appendChild(folderNote("This session is no longer open."));
    }
    actListEl.appendChild(actRow("Recently closed", "reopen a session you closed",
      function () { closeActSheet(); openRecentSheet(); }));
  }

  // Resume / Restart / Wake: one POST each, the sheet closes on success and
  // stays open (with the server's sentence) on a refusal.
  function sessionCall(title, route, okMsg, failWord) {
    actBusy = true;
    actError("");
    postFor(title, route, {})
      .then(function () {
        actBusy = false;
        closeActSheet();
        flashStatus(okMsg);
        poll();
      })
      .catch(function (err) {
        actBusy = false;
        actError(failWord + ": " + err.message);
      });
  }

  function closeSession(title) {
    actBusy = true;
    actError("");
    postFor(title, "close", {})
      .then(function (j) {
        actBusy = false;
        closeActSheet();
        flashStatus("Closed — reopen from Recently closed" + keptRunningText(j));
        poll();
      })
      .catch(function (err) {
        actBusy = false;
        actError("Close failed: " + err.message);
      });
  }

  // Delete… turns its own row into the rail's inline strip: what it does, and
  // Cancel / Delete — focus on Cancel, so a second tap never deletes.
  function askDelete(title, btn) {
    actError("");
    var strip = document.createElement("div");
    strip.className = "delete-strip";
    var msg = document.createElement("div");
    msg.textContent = "Deletes the folder and branch, including uncommitted changes. This can't be undone — use Close to keep it.";
    var btns = document.createElement("div");
    btns.className = "delete-strip-btns";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    var del = document.createElement("button");
    del.type = "button";
    del.className = "act-danger";
    del.textContent = "Delete";
    cancel.addEventListener("click", function () { renderActSheet(); });
    del.addEventListener("click", function () {
      if (actBusy) return;
      actBusy = true;
      del.disabled = cancel.disabled = true;
      postFor(title, "cleanup", {})
        .then(function (j) {
          actBusy = false;
          closeActSheet();
          flashStatus("Deleted " + nameOf(title) + " — worktree and branch removed" + keptRunningText(j));
          poll();
        })
        .catch(function (err) {
          actBusy = false;
          renderActSheet();
          var held = folderInUse(err);
          actError(held ? folderInUseText(held) : "Delete failed: " + err.message);
        });
    });
    btns.appendChild(cancel);
    btns.appendChild(del);
    strip.appendChild(msg);
    strip.appendChild(btns);
    btn.parentNode.replaceChild(strip, btn);
    cancel.focus();
  }

  // --- recently closed ---
  var recentSeq = 0;

  // closed_at arrives as epoch seconds, epoch ms or an ISO string.
  function closedMs(x) {
    var v = x && x.closed_at;
    if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
    var n = Date.parse(v);
    return isNaN(n) ? 0 : n;
  }
  function agoWord(ms) {
    if (!ms) return "";
    var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function recentError(msg) {
    recentErrEl.textContent = msg || "";
    recentErrEl.classList.toggle("hidden", !msg);
  }

  function openRecentSheet() {
    closeSheet(sessionsSheet);
    recentError("");
    recentListEl.innerHTML = "";
    recentListEl.appendChild(folderNote("loading…"));
    openSheet(recentSheet);
    var seq = ++recentSeq;
    fetch("/api/recently-closed")
      .then(function (r) {
        if (!r.ok) throw new Error("couldn't read the list (" + r.status + ")");
        return r.json();
      })
      .then(function (list) {
        if (seq !== recentSeq) return;
        list = Array.isArray(list) ? list.slice() : [];
        list.sort(function (a, b) { return closedMs(b) - closedMs(a); });
        recentListEl.innerHTML = "";
        if (!list.length) { recentListEl.appendChild(folderNote("Nothing closed recently.")); return; }
        list.forEach(function (ent) { recentListEl.appendChild(recentRow(ent)); });
      })
      .catch(function (err) {
        if (seq !== recentSeq) return;
        recentListEl.innerHTML = "";
        recentError((err && err.message) || "couldn't read the list");
      });
  }

  function recentRow(ent) {
    var row = document.createElement("div");
    row.className = "recent-row";
    var text = document.createElement("span");
    text.className = "sess-text";
    var name = document.createElement("span");
    name.className = "sess-name";
    name.textContent = ent.title || "session";
    var sub = document.createElement("span");
    sub.className = "sess-sub";
    sub.textContent = [ent.branch || "", agoWord(closedMs(ent))].filter(Boolean).join("  ·  ");
    text.appendChild(name);
    text.appendChild(sub);
    row.appendChild(text);
    if (ent.exists === false) {
      var gone = document.createElement("span");
      gone.className = "act-hint";
      gone.textContent = "worktree gone";
      row.appendChild(gone);
      return row;
    }
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "act recent-reopen";
    btn.textContent = "Reopen";
    btn.addEventListener("click", function () { reopenClosed(ent, btn); });
    row.appendChild(btn);
    return row;
  }

  function reopenClosed(ent, btn) {
    btn.disabled = true;
    recentError("");
    fetch("/api/recently-closed/" + encodeURIComponent(ent.id) + "/reopen", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.error) || "reopen failed (" + r.status + ")");
          return j;
        });
      })
      .then(function (j) {
        closeSheet(recentSheet);
        // Selected once it shows up in the list — the same wait a new session
        // gets (claimPending).
        pendingNew = (j && j.title) || ent.title || "";
        pendingNewUntil = Date.now() + PENDING_NEW_MS;
        flashStatus("Reopened " + (ent.title || "session"));
        poll();
      })
      .catch(function (err) {
        btn.disabled = false;
        recentError((err && err.message) || "reopen failed");
      });
  }

  document.getElementById("more-btn").addEventListener("click", function () { openActSheet(current); });
  document.getElementById("sessions-recent").addEventListener("click", openRecentSheet);
  document.getElementById("sessions-new").addEventListener("click", function () {
    closeSheet(sessionsSheet);
    openNewSheet();
  });
  document.getElementById("act-done").addEventListener("click", closeActSheet);
  document.getElementById("recent-done").addEventListener("click", function () { closeSheet(recentSheet); });
  askOkBtn.addEventListener("click", function () {
    var run = askRun;
    closeAsk();
    if (run) run();
  });
  askCancelBtn.addEventListener("click", closeAsk);
  // Tap the dimmed backdrop to dismiss, like the commit and new sheets.
  [sessionsSheet, actSheet, recentSheet, askSheetEl].forEach(function (el) {
    el.addEventListener("click", function (ev) {
      if (ev.target !== el) return;
      if (el === actSheet) { if (actSettled()) closeActSheet(); }
      else if (el === askSheetEl) closeAsk();
      else closeSheet(el);
    });
  });

  // --- wiring --------------------------------------------------------------
  pickerEl.addEventListener("click", openSessionsSheet);

  document.querySelectorAll(".tab").forEach(function (b) {
    b.addEventListener("click", function () { switchTab(b.dataset.tab); });
  });

  // Git workflow bar. Commit opens the message sheet; the rest POST straight
  // to their endpoints (with the desktop's questions and gates, asked on the
  // ask sheet).
  actionsEl.querySelectorAll(".act").forEach(function (b) {
    b.addEventListener("click", function () {
      if (b.disabled || !current) return;
      switch (b.dataset.act) {
        case "commit": openCommitSheet(); break;
        case "push": doPush(false, current); break;
        case "pr": doMakePr(current); break;
        case "merge": doMerge(false, current); break;
      }
    });
  });
  // Diff panel controls: flip the baseline (persisted), or re-read the diff.
  diffBaseBtn.addEventListener("click", function () {
    diffBase = diffBase === "head" ? "fork" : "head";
    try { localStorage.setItem("mf_diffbase", diffBase); } catch (e) {}
    setDiffBaseLabel();
    loadDiff();
  });
  document.getElementById("diff-refresh").addEventListener("click", function () {
    loadDiff();
  });

  document.getElementById("commit-ok").addEventListener("click", submitCommit);
  document.getElementById("commit-cancel").addEventListener("click", closeCommitSheet);
  // Tap the dimmed backdrop (outside the sheet box) to dismiss.
  commitSheet.addEventListener("click", function (ev) {
    if (ev.target === commitSheet) closeCommitSheet();
  });
  // Ctrl/Cmd+Enter commits from the message box (hardware keyboards); plain
  // Enter inserts a newline since the message is multiline.
  commitText.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") { ev.preventDefault(); submitCommit(); }
  });

  // Soft-key bar. Use pointerdown + preventDefault so tapping a key never blurs
  // the terminal's hidden textarea — otherwise the phone keyboard would dismiss
  // on every Esc/arrow tap.
  var KEY_SEQ = {
    esc: "\x1b", tab: "\t", enter: "\r",
    up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D",
  };
  document.querySelectorAll("#keys .key").forEach(function (btn) {
    btn.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      // Compose-aware: preventDefault SHOULD keep focus where it was, but
      // some mobile engines blur the field anyway, dismissing the keyboard —
      // so explicitly restore focus to the compose box after handling.
      var composing = document.activeElement === composeEl;
      if (btn.id === "ctrl") {
        setCtrl(!ctrlActive);
        if (composing) composeEl.focus();
        return;
      }
      var key = btn.dataset.key;
      if (composing && key === "enter") {
        // ⏎ under an open draft means "send it" — same as the Send button.
        sendCompose();
        return;
      }
      var seq = KEY_SEQ[key];
      if (seq) send(seq);
      if (composing) composeEl.focus();
    });
  });

  // Keep the layout pinned to the *visual* viewport so the key bar floats just
  // above the soft keyboard (which shrinks visualViewport but not innerHeight).
  // The app must be sized to the area the keyboard actually leaves visible,
  // or the bottommost bar ends up buried behind the keyboard. A single
  // visualViewport reading is not trustworthy: values arrive mid-keyboard-
  // animation and stay stale if no further event fires. So (a) clamp to
  // window.innerHeight — authoritative when interactive-widget=resizes-content
  // is honored and the keyboard resizes the layout viewport — and (b) poll
  // until the height stops moving instead of trusting one event.
  // iOS Safari (page mode) computes visualViewport.height as screen − status
  // bar − keyboard, forgetting the compact address-bar strip it keeps above
  // the keyboard. Measured with the ?debug=1 overlay (2026-07-09, iPhone):
  // vvH 458 while only ~400px were actually visible — off by exactly that
  // strip. No web API exposes it, so when the keyboard is clearly up (the
  // viewport lost >150px to it) in non-standalone iOS, reserve a fixed
  // allowance. Overshooting costs a few px of dark gap above the keyboard;
  // undershooting buries the key bar.
  var IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
            (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  var STANDALONE = !!(navigator.standalone ||
      (window.matchMedia && matchMedia("(display-mode: standalone)").matches));
  var IOS_KB_CHROME_PX = 60;
  function iosChromeAllowance(h) {
    if (!IOS || STANDALONE) return 0;
    return (window.innerHeight - h) > 150 ? IOS_KB_CHROME_PX : 0;
  }
  // Folded into visibleHeight (not applied ad hoc) so the settle loop compares
  // like with like — subtracting only inside applyViewport would leave a
  // permanent 60px "drift" that re-triggers the loop forever.
  function visibleHeight() {
    var vv = window.visualViewport;
    var h = vv ? Math.min(vv.height, window.innerHeight) : window.innerHeight;
    return h - iosChromeAllowance(h);
  }
  function viewportTop() {
    var vv = window.visualViewport;
    return vv ? Math.max(0, vv.offsetTop) : 0;
  }
  var appliedH = 0, appliedTop = 0;
  function applyViewport() {
    var h = visibleHeight();
    var top = viewportTop();
    appliedH = h;
    appliedTop = top;
    var app = document.getElementById("app");
    app.style.height = h + "px";
    // Browsers that PAN the visual viewport toward the focused compose box
    // (iOS always; Android when interactive-widget=resizes-content is not
    // honored) slide the layout viewport partly out of view — offsetTop is
    // exactly how far, and without compensation the key bar sits that many
    // pixels under the keyboard. A single event-time reading is mid-animation
    // garbage, but the settle loop below re-applies until BOTH height and
    // offset stop moving, so a stale transform corrects itself within a tick.
    app.style.transform = top > 1 ? "translateY(" + top + "px)" : "";
    if (window.scrollY || window.scrollX) window.scrollTo(0, 0);
    fitSoon();
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", applyViewport);
    window.visualViewport.addEventListener("scroll", applyViewport);
  }
  window.addEventListener("resize", applyViewport);
  // Settle loop: catches keyboard-animation endpoints (and browser-chrome
  // show/hide) that never fire a viewport event. Cheap — reapplies only on
  // an actual change.
  setInterval(function () {
    if (Math.abs(visibleHeight() - appliedH) > 1 ||
        Math.abs(viewportTop() - appliedTop) > 1) applyViewport();
  }, 250);
  // The pan happens on focus/blur of a text field; the viewport events for it
  // can arrive before the keyboard finishes animating, so nudge a couple of
  // re-applies behind the settle loop's back. One definition, shared by the
  // compose box and the new-session sheet's fields — two copies of this is how
  // one of them keeps its 400ms and the other quietly loses it.
  function nudgeViewport() {
    setTimeout(applyViewport, 100);
    setTimeout(applyViewport, 400);
  }
  composeEl.addEventListener("focus", nudgeViewport);
  composeEl.addEventListener("blur", nudgeViewport);
  window.addEventListener("orientationchange", function () { setTimeout(applyViewport, 200); });

  // --- viewport debug overlay (?debug=1) -------------------------------------
  // iOS Safari can't be remote-inspected from the server box, so when the
  // keyboard geometry goes wrong the only way to see WHICH number is lying
  // (vv.height including Safari's bottom-bar strip is the usual suspect) is
  // to print the live values on the page and read them off a screenshot.
  var dbgEl = null;
  if (/[?&#]debug/.test(location.search + location.hash)) {
    dbgEl = document.createElement("div");
    dbgEl.id = "vv-debug";
    document.getElementById("app").appendChild(dbgEl);
    setInterval(updateDebug, 300);
  }
  function updateDebug() {
    if (!dbgEl) return;
    var vv = window.visualViewport;
    dbgEl.textContent =
      "inner " + window.innerHeight +
      " · vvH " + (vv ? Math.round(vv.height) : "n/a") +
      " · vvTop " + (vv ? Math.round(vv.offsetTop) : "n/a") +
      " · scale " + (vv ? vv.scale.toFixed(2) : "n/a") +
      " · scrollY " + Math.round(window.scrollY) +
      " · applied " + Math.round(appliedH) + "/" + Math.round(appliedTop);
  }

  // --- appearance sync -------------------------------------------------------
  // The look is chosen in the desktop's Settings → Appearance and persisted
  // server-side (ui.accent / ui.surface). The inline <head> script already
  // applied this browser's cached copy before first paint; here we fetch the
  // server value, reconcile, and restyle the live terminal + browser chrome.
  function applyAppearance(accent, surface) {
    accent = accent || "";
    surface = surface || "";
    try {
      if (accent) document.documentElement.setAttribute("data-accent", accent);
      else document.documentElement.removeAttribute("data-accent");
      if (surface) document.documentElement.setAttribute("data-surface", surface);
      else document.documentElement.removeAttribute("data-surface");
      if (accent) localStorage.setItem("cs_accent", accent);
      else localStorage.removeItem("cs_accent");
      if (surface) localStorage.setItem("cs_surface", surface);
      else localStorage.removeItem("cs_surface");
    } catch (e) {}
    if (term) term.options.theme = termTheme();
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", cssVar("--bg", "#0f1117"));
  }
  function syncAppearance() {
    fetch("/api/settings")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        var ui = (j && j.settings && j.settings.ui) || {};
        applyAppearance(
          typeof ui.accent === "string" ? ui.accent : "",
          typeof ui.surface === "string" ? ui.surface : "");
      })
      .catch(function () { /* offline — keep the cached look */ });
  }
  // Restyle the chrome for the cached attributes now, then reconcile.
  var meta0 = document.querySelector('meta[name="theme-color"]');
  if (meta0) meta0.setAttribute("content", cssVar("--bg", "#0f1117"));
  syncAppearance();

  // =========================================================================
  // VOICE — talking to the selected session from the phone
  // =========================================================================
  // One state machine, `voice.state`:
  //   off        voice mode is off (strip hidden, mic reads "off")
  //   idle       on; nothing happening — "Your turn", tap to talk
  //   listening  the recognizer is up for a prompt. Radio rules: every
  //              phrase heard lands in the compose box and it keeps listening
  //              across pauses until a trailing "over" stops listening with
  //              the words left in the box (nothing sent), a trailing
  //              "submit" sends the box, a trailing "disregard" empties it
  //              and listens on, push to talk ends it like "over" (prefs.ptt
  //              "toggle": the second tap on the mic or the strip; "hold":
  //              letting go of the mic — both wait for the words still on
  //              their way, and never send), Cancel stops and drops them, or
  //              nothing has been heard for VOICE_DICT_IDLE_MS (kept, not
  //              sent). Only "submit" and the Send button send. The first phrase said
  //              into an empty box goes to POST /api/voice/route, which says
  //              whether it is a prompt or a command ("switch to…", "session
  //              two", "next session", "new session…", "session three yes");
  //              the local VOICE_CMD table answers when the server cannot.
  //              At the END of any phrase (and of every streamed run of
  //              words) a stop phrase ("stop recording", "that's all") stops
  //              listening with the box kept, and while the session is asking
  //              (voiceAsks) an answer ("yes", "no thanks", "option two")
  //              answers it — neither ever lands in the box
  //   sending    POST /send (or /answer) in flight
  //   working    the agent has the prompt; a 3 s poll of last_reply waits for
  //              its first sentence and speaks it ONCE as the "what it is
  //              doing" ack, then the reply event/poll takes over
  //   speaking   a line of the conversation is queued or being spoken
  //              (speakLine); recognition is stopped meanwhile so the phone
  //              does not hear itself; the flow that asked decides what comes
  //              next
  //   awaiting   the agent asked a question (has_question): listening for the
  //              answer, 12 s, one "Still there?" re-prompt, then idle
  //   dialog     a permission dialog was read out; listening for yes / no /
  //              always / a number — free text is NEVER typed into a dialog
  //
  // Voice is for the SELECTED session (`current`): picking another session
  // moves voice to it (POST /voice {on:true} there), "switch to <name>",
  // "session two" (the Sessions sheet's second row, as numbered there) and
  // "next session" do the same by voice. The flag of the session you left
  // stays on. An answer for ANOTHER session ("session three yes", "tell
  // parser no") answers it there without moving voice.
  //
  // Spoken replies (prefs.replies: "off" | "mine", the default | "all"):
  // "mine" asks for the Say: line PER PROMPT — POST /send carries say:true
  // only for a prompt that was dictated (voice.dictated), so a typed prompt
  // costs no Say tokens, and its reply (no Say line) is never read out
  // (voice.expectSay). "all" (Everything) reads every session's finished
  // turn (voiceTurnLine), voice on or off — the events socket stays open
  // for it — and every prompt the phone sends, typed too, asks for a Say
  // line. Prompts typed anywhere else ask for it through the server's
  // say-all marker (POST /api/voice/say-all {on}): switching TO Everything
  // turns it on, switching away turns it off; on load, a GET /api/voice
  // `say_all` that disagrees with the pref is set to the pref, once; a 403
  // (not the owner) is said once and the pref stays (voiceSayAll*). An
  // older page's boolean loads as "mine" (true) / "off" (false).
  //
  // One voice at a time: everything the phone says — replies, dialog
  // read-outs, another session's ask or turn, router lines — goes through
  // ONE queue (speakLine → voiceQueueStep), strictly in arrival order. A
  // line about another session than the one the phone is on starts with
  // that session's name; the phone's own session is spoken bare
  // (voiceLineText). The queue holds while the mic listens, and a listen
  // after a read-out starts once it has drained (voiceListenAfter).
  //
  // Phone rules this code exists for (research-notes.md, verified 2026-10-22):
  //   * iOS speaks NOTHING unless the first speechSynthesis.speak() ran
  //     synchronously inside a tap (click, not touchstart, no await before
  //     it) — voicePrime() is the first statement of every gesture handler,
  //     the hold-to-talk pointerdown / pointerup ones included (a touch
  //     activates on pointerup, so speech is unlocked by the release or the
  //     click that turned voice on).
  //     Chrome Android errors "not-allowed" before a tap: the same prime.
  //   * iOS has recognition only in a Safari TAB (WebKit 225298): a
  //     home-screen app or any other iOS browser has no SpeechRecognition at
  //     all → the Safari banner with this page's URL to carry over.
  //   * iOS speaks only through compact built-in voices, `onend` is
  //     unreliable and long utterances get cut (Google network voices stop at
  //     ~15 s): text is chunked by sentence, every chunk has a timeout
  //     fallback, and utterance objects stay referenced until they end (a
  //     GC'd utterance never fires onend).
  //   * Nothing runs while the page is hidden or the phone is locked (iOS
  //     suspends the page in seconds, the socket dies in ~30 s): hidden →
  //     stop everything; visible → re-request the wake lock, reconnect, and
  //     ask GET /voice for a say that arrived meanwhile.
  //   * An <audio> element finishing kills iOS recognition: every prompt is
  //     speechSynthesis, never <audio>.
  //   * navigator.audioSession.type = "playback" (Safari 17+) inside the first
  //     tap, so the silent switch does not mute the page.
  //   * navigator.vibrate(30) on Android marks "listening" (iOS ignores it).
  //
  // Adapters, reachable as window.mfVoice.stt / .tts, all with one surface:
  //   stt {name, available(), start({onInterim, onFinal, onError, onEnd,
  //        onLevel?, onSpeech?, onStatus?, until?, whole?, answers?}), stop(), active(), release()} —
  //        onFinal fires once per PHRASE (one start may deliver several);
  //        onInterim(text, tail?): tail = the end of text still settling;
  //        onStatus carries a caption that is not words ("Transcribing…").
  //        PhoneSTT over webkitSpeechRecognition (continuous), adding end()
  //        (stop gracefully: what was heard still arrives); ServerSTT
  //        records a clip (WAV, off the AudioContext) per phrase — its silence detector
  //        ends the clip — and POSTs it to /api/voice/transcribe, adding
  //        finish() (end the clip now: the tap) and pending() (on its way);
  //        on a server that streams it sends the phrase in chunks and
  //        onFinal fires with each run of settled words as they come, unless
  //        whole() says the phrase must arrive in one piece (it may be a
  //        command, or a dialog's answer); a stop phrase is handed over the
  //        moment it settles, and the words that may start a control phrase
  //        (answers(): the session is asking, so "yes" too) are held back
  //   tts {name, available(), prime(), speak(text) → Promise, cancel(),
  //        voices()} — PhoneTTS over speechSynthesis; ServerTTS POSTs
  //        /api/voice/speak and plays the audio through one AudioContext,
  //        falling back to PhoneTTS for any line the server cannot say
  // voiceApplyAdapters picks per GET /api/voice (on voice start and whenever
  // the page comes back): the server's when Settings say "server" AND the
  // server reports ready, else the phone's. The state machine never asks
  // which one is active; it only uses the surface above.
  // The events socket (WS /api/events) is open only while voice is on; the
  // page's 4 s /api/instances poll is the fallback so a dead socket never
  // loses a reply (voiceOnPoll).
  var voiceBtn = document.getElementById("voice-btn");
  var voiceStrip = document.getElementById("voice-strip");
  var voiceStateEl = document.getElementById("voice-state");
  var voiceCaptionEl = document.getElementById("voice-caption");
  var voiceCancelBtn = document.getElementById("voice-cancel");
  var voiceHushBtn = document.getElementById("voice-hush");
  var voiceGearBtn = document.getElementById("voice-gear");
  var voiceLevelEl = document.getElementById("voice-level");
  var voiceLevelBar = document.getElementById("voice-level-bar");
  var voiceHearBtn = document.getElementById("voice-hear");
  var voiceErrEl = document.getElementById("voice-error");
  var voiceSheet = document.getElementById("voice-sheet");
  var voiceOnToggle = document.getElementById("voice-on-toggle");
  var voiceSheetSession = document.getElementById("voice-sheet-session");
  var voiceHandsEl = document.getElementById("voice-handsfree");
  var voicePttEl = document.getElementById("voice-ptt");
  var voiceRepliesEl = document.getElementById("voice-replies");
  var voiceWakeEl = document.getElementById("voice-wake");
  var voiceWakeNote = document.getElementById("voice-wake-note");
  var voiceRateEl = document.getElementById("voice-rate");
  var voiceRateVal = document.getElementById("voice-rate-val");
  var voiceVoiceEl = document.getElementById("voice-voice");
  var voiceApproveLine = document.getElementById("voice-approve-line");
  var voiceDiagEl = document.getElementById("voice-diag");
  var voiceDoneBtn = document.getElementById("voice-done");
  var safariBanner = document.getElementById("voice-safari-banner");
  var safariUrlEl = document.getElementById("voice-safari-url");
  var safariCopyBtn = document.getElementById("voice-safari-copy");
  var safariCloseBtn = document.getElementById("voice-safari-close");
  var safariAltEl = document.getElementById("voice-safari-alt");

  var ANDROID = /Android/i.test(navigator.userAgent);
  var SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var VOICE_STORE = "cs_voice";          // localStorage: session, on, rate, voiceName, handsFree, ptt, replies, wake
  var VOICE_LINE_MAX_AGE_MS = 60000;     // a queued line this old when its turn comes is dropped, not said late
  var VOICE_DICT_IDLE_MS = 120000;       // listening ends after this long without a word (the words stay)
  var VOICE_RAPID_RESTART_MAX = 8;       // recognizers that die within a second of starting, in a row
  var VOICE_HINT_TAP = "Say “over” to keep it, “submit” to send, “disregard” to start over";
  var VOICE_HINT_HOLD = "Release to keep it; say “submit” to send";
  var VOICE_KEPT = "In the box — say “submit”, or tap Send";
  var VOICE_OVER_WAIT_MS = 1500;         // push to talk: how long the last phrase may take to arrive
  var VOICE_ROUTE_TIMEOUT_MS = 2000;     // POST /api/voice/route; past it the local table decides
  var VOICE_TAP_MS = 15000;              // listening window after a deliberate tap
  var VOICE_ANSWER_MS = 12000;           // ...after the agent asked a question
  var VOICE_HANDSFREE_MS = 8000;         // ...after a plain reply, hands-free only
  var VOICE_DIALOG_MS = 12000;           // ...after a dialog was read out
  var VOICE_ACK_POLL_MS = 3000;          // last_reply poll while working
  var VOICE_ACK_MAX_MS = 10 * 60 * 1000; // give up on the ack after this long
  var VOICE_WORD_MS = 400;               // onend fallback: per word...
  var VOICE_CHUNK_GRACE_MS = 1500;       // ...plus this per chunk
  var VOICE_BARGE_MUTE_MS = 150;         // barge-in: the mic opens this long after the speaker is cut (its tail is not heard)
  var VOICE_QUIET_CAPTION_MS = 1500;     // "Quiet." stays on the strip this long

  // --- PhoneSTT: SpeechRecognition / webkitSpeechRecognition ----------------
  // The state machine restarts it while its listening window is open; the
  // adapter never restarts itself. stop() detaches the handlers first, so a
  // stop never reports an end; end() (push to talk letting go) is the
  // graceful one: the recognizer delivers what it still has, then onEnd.
  function PhoneSTT() {
    var rec = null;
    function available() { return !!SpeechRec; }
    function start(h) {
      stop();
      if (!SpeechRec) { if (h.onError) h.onError("unavailable"); return false; }
      var r;
      try { r = new SpeechRec(); } catch (e) { if (h.onError) h.onError("unavailable"); return false; }
      // continuous: Chrome keeps one session up across pauses and delivers
      // a final per phrase (no restart, no Android beep per phrase); iOS
      // ignores it and ends after each phrase — the state machine restarts.
      r.continuous = true;
      r.interimResults = true;
      r.maxAlternatives = 1;
      r.lang = navigator.language || "en-US";
      var interim = "";
      r.onresult = function (ev) {
        var finals = "";
        interim = "";
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          var res = ev.results[i];
          var txt = (res[0] && res[0].transcript) || "";
          if (res.isFinal) finals += txt; else interim += txt;
        }
        if (finals.trim()) {
          if (h.onFinal) h.onFinal(finals.trim());   // one phrase; the recognizer stays up
        } else if (h.onInterim) {
          h.onInterim(interim.trim());
        }
      };
      r.onerror = function (ev) {
        if (h.onError) h.onError((ev && ev.error) || "error");
      };
      r.onend = function () {
        if (rec === r) rec = null;
        // iOS: a final can stall and never arrive — the last interim reading
        // is what was said, and better than losing the words.
        var tail = interim.trim();
        interim = "";
        if (tail && h.onFinal) h.onFinal(tail);
        if (h.onEnd) h.onEnd();
      };
      rec = r;
      try { r.start(); } catch (e) {
        rec = null;
        if (h.onError) h.onError("start-failed");
        return false;
      }
      return true;
    }
    function stop() {
      var r = rec;
      rec = null;
      if (!r) return;
      r.onresult = r.onerror = r.onend = null;
      try { if (r.abort) r.abort(); else r.stop(); } catch (e) {}
    }
    function end() {
      var r = rec;
      if (!r) return false;
      try { r.stop(); } catch (e) { stop(); return false; }
      return true;
    }
    function active() { return !!rec; }
    return { name: "phone", available: available, start: start, stop: stop, end: end, active: active, release: stop };
  }

  // --- PhoneTTS: speechSynthesis --------------------------------------------
  function PhoneTTS() {
    var synth = window.speechSynthesis || null;
    var keep = [];     // utterances alive until they end (a GC'd one never fires onend)
    var gen = 0;       // cancel() bumps it: an older speak()'s chain stops
    function available() { return !!(synth && window.SpeechSynthesisUtterance); }
    function unkeep(u) { var i = keep.indexOf(u); if (i >= 0) keep.splice(i, 1); }
    // SYNCHRONOUS, from inside the tap handler, before any await: the page's
    // first speak() unlocks speech on iOS (and Chrome Android) only when it
    // runs in the gesture. An empty utterance is enough and says nothing.
    function prime() {
      if (!available()) return;
      try {
        var u = new SpeechSynthesisUtterance("");
        u.volume = 0;
        u.onend = u.onerror = function () { unkeep(u); };
        keep.push(u);
        synth.speak(u);
      } catch (e) {}
    }
    function voices() {
      try { return (synth && synth.getVoices()) || []; } catch (e) { return []; }
    }
    function pick() {
      var want = voice.prefs.voiceName;
      if (!want) return null;
      var vs = voices();
      for (var i = 0; i < vs.length; i++) if (vs[i].name === want) return vs[i];
      return null;
    }
    // Sentence-chunked; each chunk resolves on onend OR a timeout of
    // ~400 ms/word + 1.5 s, because onend is unreliable on phones.
    function speak(text) {
      return new Promise(function (resolve) {
        var parts = sentenceChunks(text);
        if (!available() || !parts.length) { resolve(); return; }
        var g = gen, i = 0;
        function next() {
          if (g !== gen || i >= parts.length) { resolve(); return; }
          var part = parts[i++];
          var u = new SpeechSynthesisUtterance(part);
          var v = pick();
          if (v) u.voice = v; else u.lang = navigator.language || "en-US";
          var rate = voice.prefs.rate || 1;
          u.rate = rate;
          var done = false, timer = null;
          function finish() {
            if (done) return;
            done = true;
            clearTimeout(timer);
            unkeep(u);
            next();
          }
          u.onend = finish;
          u.onerror = finish;
          var words = part.split(/\s+/).length;
          timer = setTimeout(finish, (words * VOICE_WORD_MS + VOICE_CHUNK_GRACE_MS) / rate);
          keep.push(u);
          try { synth.speak(u); } catch (e) { finish(); }
        }
        next();
      });
    }
    function cancel() {
      gen++;
      if (synth) { try { synth.cancel(); } catch (e) {} }
    }
    return { name: "phone", available: available, prime: prime, speak: speak, cancel: cancel, voices: voices };
  }

  // --- ServerSTT: WAV recorder → POST /api/voice/transcribe ------------------
  // The phone records ONE clip and the server recognises it (Settings →
  // Voice, recognition "server"; GET /api/voice says when the server is
  // ready). This is the recognizer an iOS home-screen app has — Web Speech
  // exists only in a Safari tab — and the one that keeps the audio off
  // Google's and Apple's servers. The clip is 16-bit PCM mono WAV at 16 kHz,
  // whatever the browser: the server's built-in recognizers (Windows'
  // System.Speech, macOS' Speech framework) need plain PCM, and faster-whisper
  // and OpenAI take WAV too. The browser's own recorder cannot make it
  // (webm/opus on Chrome, mp4/AAC on Safari), so the samples come straight off the shared
  // AudioContext (voiceAudioCtx, made inside the tap): the mic stream →
  // ScriptProcessorNode(4096, 1, 1) → a muted GainNode → the destination
  // (Chrome does not run a ScriptProcessor that reaches nothing). Each block's
  // channel 0 is copied into a list; finish() joins them, resamples to
  // VOICE_WAV_RATE (voiceResampleClip: the browser's own anti-aliased
  // resampler, else voiceResample's low-pass decimator) and encodes
  // (voiceWavEncode) — the same clip as the desktop app's lib/wavRecorder.ts. The mic stream is
  // asked for once and kept warm across turns — VOICE_KEEP_STREAM_MS after
  // the last phrase (longer while a reply is on its way, VOICE_KEEP_HIDDEN_MS
  // once the page hides; keep()) — its tracks stopped then and when voice
  // goes off (release()). There are no interim words: onStatus says
  // "Hearing you…" while the detector hears speech, else "Listening… <n> s"
  // (the machine adds the radio hint); onLevel feeds the mic's ring and the
  // strip's meter (voiceLevelOf: floor-relative, 0..1) from the RMS of the
  // same blocks, and onSpeech fires on a phrase's first loud block.
  // The clip is one phrase: it ends on VOICE_SILENCE_MS of quiet after speech
  // was heard, on a tap (finish()), or at the cap (VOICE_CLIP_MAX_MS, or the listening window if
  // that is shorter); stop() discards it and never reports, like PhoneSTT's.
  // While the clip is on its way (pending()) the state machine's window
  // waits instead of expiring. Errors reach onError with PhoneSTT's codes
  // plus "server" (detail = the line to show; a 409 is the one-line hint to
  // turn recognition on) and "nothing-recorded".
  //
  // Streaming (GET /api/voice → server_ready.stt_stream: the local whisper
  // backend): the words appear WHILE the phrase is spoken. start() opens a
  // stream (POST /api/voice/stream {lang} → {id}) at once, alongside
  // getUserMedia; nothing is sent before the first loud block (only the
  // last VOICE_LEAD_MS before it is kept, so the first chunk goes as the
  // word starts); from then, whenever the previous
  // request has returned and VOICE_STREAM_CHUNK_MS of new audio is in, the
  // samples since the last cut go out as one more 16 kHz WAV (POST
  // …/chunk → {committed, partial}) — one request at a time, so a slow
  // server gets fewer, longer chunks instead of a queue. `committed` words
  // never change: they reach onFinal (into the box) as they come — all but
  // the last, and never a run ending in a radio word (voiceStreamSplit), so
  // "over" / "submit" / "disregard" act only as the phrase's real end.
  // `partial`, the unstable tail, is the caption (onInterim). While whole()
  // says so (an empty box: the router decides what the phrase is; a
  // dialog's answer), nothing reaches onFinal before the end — the caption
  // shows the words. The phrase ends as a clip does (silence, the tap, the
  // cap): the unsent audio, then POST …/end → {committed} → onFinal of the
  // rest, and the state machine's restart opens the next phrase's stream.
  // stop() DELETEs the stream. A 409 on the open (the server does not
  // stream after all: clip per phrase until GET /api/voice says otherwise)
  // or any failed chunk turns the phrase into a clip: the words so far
  // stay, the audio not yet answered is transcribed in one piece.
  var VOICE_WAV_RATE = 16000;            // the rate of the uploaded clip
  var VOICE_SILENCE_MS = 1100;           // quiet after speech that ends the clip
  var VOICE_CLIP_MAX_MS = 12000;         // the hard cap on one clip
  var VOICE_STREAM_CHUNK_MS = 300;       // the least new audio a stream chunk carries (the server's minimum)
  var VOICE_STREAM_MAX_MS = 30000;       // a streamed phrase's cap (its words arrive as it goes)
  var VOICE_LEVEL_MS = 100;              // the caption clock's period
  var VOICE_RMS_SPEECH = 0.02;           // RMS (0..1) above which the clip has speech
  var VOICE_KEEP_STREAM_MS = 90000;      // the microphone stays warm this long after its last phrase (keep())
  var VOICE_KEEP_HIDDEN_MS = 15000;      // ...but only this long once the page is hidden
  var VOICE_LEAD_MS = 300;               // a stream's audio kept from before the first loud block (the soft start of the word)
  var VOICE_HEARING_MS = 400;            // "Hearing you…" holds this long after the last loud block
  var VOICE_RING_ATTACK_MS = 30;         // the mic's ring: how fast it rises to the voice's level
  var VOICE_RING_DECAY_MS = 150;         // ...and its fall's time constant
  var VOICE_UPLOAD_TIMEOUT_MS = 45000;   // the transcribe request's own cap
  var VOICE_RESAMPLE_TIMEOUT_MS = 5000;  // OfflineAudioContext's render; past it, voiceResample
  var VOICE_SINC_HALF = 16;              // voiceResample's kernel half-width, in output samples
  var VOICE_SINC_CUTOFF = 0.45;          // ...its cutoff, as a fraction of the output rate (0.5 = Nyquist)
  var VOICE_SINC_STEPS = 64;             // ...its table: entries per input sample
  // The clip at VOICE_WAV_RATE, resolved: the browser's own resampler
  // (OfflineAudioContext(1, n * 16000 / rate, 16000) playing the clip — a
  // proper anti-aliased one), else voiceResample (no OfflineAudioContext, a
  // rate it refuses, a render that fails or never ends). voice.rec.resampler
  // names the one that ran (Diagnostics).
  function voiceResampleClip(samples, from) {
    var to = VOICE_WAV_RATE;
    if (!from || from === to) {
      voice.rec.resampler = "none (the microphone runs at 16 kHz)";
      return Promise.resolve(samples);
    }
    var OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () { settle(null, "the browser's render timed out"); }, VOICE_RESAMPLE_TIMEOUT_MS);
      function settle(out, why) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (out) { voice.rec.resampler = "OfflineAudioContext (the browser's)"; resolve(out); return; }
        voice.rec.resampler = "low-pass decimator (" + why + ")";
        resolve(voiceResample(samples, from, to));
      }
      function rendered(b) {
        try { settle(b.getChannelData(0)); } catch (e) { settle(null, "the browser's render failed"); }
      }
      if (!OAC) { settle(null, "no OfflineAudioContext"); return; }
      try {
        var octx = new OAC(1, Math.ceil((samples.length * to) / from), to);
        var buf = octx.createBuffer(1, samples.length, from);
        buf.getChannelData(0).set(samples);
        var src = octx.createBufferSource();
        src.buffer = buf;
        src.connect(octx.destination);
        src.start(0);
        // Older WebKit has only the event; the rest return a promise too.
        octx.oncomplete = function (ev) { rendered(ev.renderedBuffer); };
        var p = octx.startRendering();
        if (p && p.then) p.then(rendered, function () { settle(null, "the browser's render failed"); });
      } catch (e) {
        settle(null, "the browser refused " + from + " → " + to + " Hz");
      }
    });
  }
  // From `from` Hz to `to` Hz (mono), the fallback: down (the usual 48 or
  // 44.1 kHz mic → 16 kHz), a low-pass decimator — every output sample is
  // the windowed-sinc (Blackman) weighted average of the input samples
  // around it, cut off at VOICE_SINC_CUTOFF of the output rate, so what the
  // 16 kHz clip cannot hold is filtered out instead of folding back into
  // the speech band as noise (picking or interpolating samples aliases it,
  // and a plain average of the span barely dents it). Up (an 8 kHz
  // headset), linear interpolation: nothing above the new Nyquist to fold.
  // The input itself when the rates match.
  function voiceResample(samples, from, to) {
    if (!from || !to || from === to || !samples.length) return samples;
    var n = samples.length;
    var out = new Float32Array(Math.floor((n * to) / from));
    var step = from / to, i;
    if (step < 1) {
      for (i = 0; i < out.length; i++) {
        var pos = i * step, i0 = Math.floor(pos), i1 = i0 + 1 < n ? i0 + 1 : n - 1;
        out[i] = samples[i0] + (samples[i1] - samples[i0]) * (pos - i0);
      }
      return out;
    }
    // The kernel, tabled VOICE_SINC_STEPS per input sample over
    // [-half, half]: fc in cycles per input sample.
    var fc = VOICE_SINC_CUTOFF / step, half = VOICE_SINC_HALF * step;
    var size = Math.ceil(2 * half * VOICE_SINC_STEPS) + 1, kern = new Float32Array(size);
    for (i = 0; i < size; i++) {
      var x = i / VOICE_SINC_STEPS - half, u = i / (size - 1);
      var sinc = x === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * x) / (Math.PI * x);
      kern[i] = sinc * (0.42 - 0.5 * Math.cos(2 * Math.PI * u) + 0.08 * Math.cos(4 * Math.PI * u));
    }
    for (i = 0; i < out.length; i++) {
      var c = i * step, lo = Math.max(0, Math.ceil(c - half)), hi = Math.min(n - 1, Math.floor(c + half));
      var acc = 0, wsum = 0;
      for (var k = lo; k <= hi; k++) {
        var w = kern[Math.round((k - c + half) * VOICE_SINC_STEPS)];
        acc += samples[k] * w;
        wsum += w;
      }
      // Normalised by the weights it used: a level is kept, at the edges too.
      out[i] = wsum ? acc / wsum : 0;
    }
    return out;
  }
  // 16-bit PCM mono WAV: the 44-byte RIFF header, little-endian, then the
  // samples clamped to [-1, 1]. An ArrayBuffer (the caller makes the Blob).
  function voiceWavEncode(samples, rate) {
    var size = samples.length * 2;
    var buf = new ArrayBuffer(44 + size), v = new DataView(buf);
    function ascii(at, s) { for (var i = 0; i < s.length; i++) v.setUint8(at + i, s.charCodeAt(i)); }
    ascii(0, "RIFF");
    v.setUint32(4, 36 + size, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    v.setUint32(16, 16, true);         // fmt chunk size
    v.setUint16(20, 1, true);          // PCM
    v.setUint16(22, 1, true);          // mono
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true);   // byte rate = rate * block align
    v.setUint16(32, 2, true);          // block align = channels * 2 bytes
    v.setUint16(34, 16, true);         // bits per sample
    ascii(36, "data");
    v.setUint32(40, size, true);
    for (var i = 0, at = 44; i < samples.length; i++, at += 2) {
      var s = samples[i];
      var c = s > 1 ? 1 : s < -1 ? -1 : s === s ? s : 0;
      v.setInt16(at, c < 0 ? Math.round(c * 32768) : Math.round(c * 32767), true);
    }
    return buf;
  }
  // A stream's settled words, split: what may go into the box now, and what
  // waits for the next words or the flush — the last word (it may be the
  // radio word that ends the phrase) and, with it, any words that would make
  // the run END in a radio word ("go over the" → "go" now, "over the"
  // waits: an "over" mid-sentence is a word, but "go over" alone would stop
  // listening) — and up to VOICE_HOLD_MAX trailing words that may be the
  // start of a control phrase ("fix it stop" → "fix it" now, "stop" waits:
  // it may be "stop recording"; with `answers`, the session is asking and
  // "yes" / "option" wait too). [now, held].
  function voiceStreamSplit(text, answers) {
    var words = String(text || "").trim().split(/\s+/).filter(Boolean);
    var held = words.length ? [words.pop()] : [];
    while (words.length && voiceDictParse(words.join(" ")).kind) held.unshift(words.pop());
    var all = words.concat(held);
    for (var k = Math.min(VOICE_HOLD_MAX, all.length); k > held.length; k--) {
      if (voiceMayStart(all.slice(all.length - k).join(" "), answers)) { held = all.slice(all.length - k); break; }
    }
    return [all.slice(0, all.length - held.length).join(" "), held.join(" ")];
  }
  function voiceJoin(a, b) {
    a = String(a || "").trim();
    b = String(b || "").trim();
    return a && b ? a + " " + b : a || b;
  }
  // The level the mic's ring and the meter show (0..1) for a block's RMS:
  // relative to the noise floor, so a room's hum sits at zero, on a square
  // root so a quiet voice still moves it well.
  function voiceLevelOf(rms, floor) {
    var over = (Number(rms) || 0) - Math.max((Number(floor) || 0) * 2, 0.003);
    return over > 0 ? Math.min(1, Math.sqrt(over * 8)) : 0;
  }
  // One step (dtMs later) of the shown level toward the latest block's:
  // it rises within VOICE_RING_ATTACK_MS and falls with a
  // VOICE_RING_DECAY_MS time constant. Pure: the rAF loop calls it per frame.
  function voiceLevelStep(shown, target, dtMs) {
    var dt = Math.max(0, Number(dtMs) || 0);
    if (target >= shown) return shown + (target - shown) * Math.min(1, dt / VOICE_RING_ATTACK_MS);
    return target + (shown - target) * Math.exp(-dt / VOICE_RING_DECAY_MS);
  }
  // The start-up trace of a phrase: ms from start() (for the first phrase,
  // the tap itself) to the microphone ready, the stream open, the first loud
  // block, the first words shown and the first settled ones. The last one
  // is in Diagnostics; with localStorage cs_voice_debug set, the console
  // gets every mark.
  function voiceTraceMark(g, what) {
    if (!g.trace || what in g.trace) return;
    g.trace[what] = Date.now() - g.t00;
    voice.rec.trace = g.trace;
    if (g.debug) { try { console.log("[voice] " + what + " +" + g.trace[what] + " ms"); } catch (e) {} }
  }
  function voiceDebugOn() {
    try { var f = localStorage.getItem("cs_voice_debug"); return !!f && f !== "0" && f !== "false"; } catch (e) { return false; }
  }
  // The wake word's lone-utterance detector (ServerSTT.standby feeds it; see
  // "the wake word" in the state machine): the RMS of each block and its
  // length in ms. A wake is a word or two ON ITS OWN — VOICE_WAKE_QUIET_MS
  // of quiet before it, VOICE_WAKE_MIN_MS..VOICE_WAKE_MAX_MS from its first
  // loud block to its last, VOICE_WAKE_QUIET_MS of quiet after (dips shorter
  // than that, the stop in "mind·flock", are inside the word). Shorter is a
  // click or a cough, longer a sentence: both dropped, nothing sent. The
  // noise floor and "loud" are the recorder's (scaled to the block's length:
  // its rule is per 85 ms block).
  var VOICE_WAKE_BLOCK = 2048;           // the standby's ScriptProcessor block (~43 ms at 48 kHz)
  var VOICE_WAKE_QUIET_MS = 350;         // quiet that ends a lone utterance, and that must come before it
  var VOICE_WAKE_MIN_MS = 250;           // its sound lasts at least this long...
  var VOICE_WAKE_MAX_MS = 1600;          // ...and at most this (longer is a sentence: never a wake)
  var VOICE_WAKE_LEAD_MS = 150;          // audio kept from before its first loud block (the word's soft start)
  function voiceWakeNew() {
    return { t: 0, floor: 0, phase: "wait", quiet: 0, from: 0, last: 0, tail: 0 };
  }
  // One block: d's clock moves on by ms. → null, or what just ended:
  // {kind: "clip", from, to, ms} — from..to on d's clock (ms), the lead-in
  // and the quiet tail included; ms = the sound's length — or {kind:
  // "short" | "long"}. phase: "wait" (no quiet before yet: the start, or
  // speech that went on), "ready" (quiet enough), "word" (sounding).
  function voiceWakeStep(d, rms, ms) {
    var at = d.t, k = ms / 85;
    rms = Number(rms) || 0;
    d.t += ms;
    d.floor = d.floor === 0 ? rms : Math.min(rms, d.floor * (1 + 0.05 * k) + 0.001 * k);
    var loud = rms > Math.max(VOICE_RMS_SPEECH, d.floor * 3);
    if (d.phase === "word") {
      if (loud) {
        d.last = d.t;
        d.tail = 0;
        if (d.last - d.from > VOICE_WAKE_MAX_MS) { d.phase = "wait"; d.quiet = 0; return { kind: "long" }; }
        return null;
      }
      d.tail += ms;
      if (d.tail < VOICE_WAKE_QUIET_MS) return null;
      d.phase = "ready";   // its quiet tail is the quiet before the next one
      d.quiet = d.tail;
      var span = d.last - d.from;
      if (span < VOICE_WAKE_MIN_MS) return { kind: "short", ms: span };
      return { kind: "clip", from: Math.max(0, d.from - VOICE_WAKE_LEAD_MS), to: d.t, ms: span };
    }
    if (loud) {
      if (d.phase === "ready") { d.phase = "word"; d.from = at; d.last = d.t; d.tail = 0; }
      else d.quiet = 0;   // no quiet before it: part of something longer
      return null;
    }
    d.quiet += ms;
    if (d.phase === "wait" && d.quiet >= VOICE_WAKE_QUIET_MS) d.phase = "ready";
    return null;
  }
  // The samples of the blocks ({at: ms on the detector's clock, x}) that
  // overlap from..to, joined.
  function voiceWakeSlice(blocks, from, to, rate) {
    var keep = blocks.filter(function (b) { return b.at < to && b.at + (b.x.length * 1000) / rate > from; });
    var n = 0, at = 0;
    keep.forEach(function (b) { n += b.x.length; });
    var out = new Float32Array(n);
    keep.forEach(function (b) { out.set(b.x, at); at += b.x.length; });
    return out;
  }
  function ServerSTT() {
    var stream = null;        // the microphone, kept across turns
    var token = null;         // one object per start(); null = not active
    var rec = null;           // the recording of the current clip (recStart)
    var uploading = false;    // the clip (or the stream's rest) is on its way to the server
    var aborter = null;       // AbortController of that upload
    var live = null;          // the streamed phrase (recStart's g) until its words are in
    var clockTimer = null, capTimer = null;
    var keepTimer = null, keepHidden = false;   // keep(): when the warm microphone goes back
    var standing = null;      // the wake word's standby (standby()); null = not running
    var holding = false;      // hold(): the standby wants the microphone kept (it may be paused)
    function available() {
      return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
                (window.AudioContext || window.webkitAudioContext));
    }
    // A stream still worth reusing: active, every track live and not muted
    // (iOS mutes the track of a page it suspended; another app may hold the mic).
    function micLive(s) {
      if (!s || s.active === false) return false;
      try {
        return s.getAudioTracks().every(function (t) { return t.readyState !== "ended" && !t.muted; });
      } catch (e) { return true; }
    }
    function micStop(s) {
      if (s) { try { s.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {} }
    }
    // One ask at a time: the standby and a tap's recording asking together
    // share it (two streams would leave one open, the mic indicator on).
    var asking = null;
    function getStream() {
      clearTimeout(keepTimer);
      if (micLive(stream)) return Promise.resolve(stream);
      if (asking) return asking;
      micStop(stream);
      stream = null;
      asking = navigator.mediaDevices.getUserMedia({ audio: true }).then(
        function (s) { asking = null; stream = s; return s; },
        function (err) { asking = null; throw err; });
      return asking;
    }
    // The microphone stays warm VOICE_KEEP_STREAM_MS after its last phrase,
    // so the next tap starts at once (no getUserMedia: on a phone that is
    // 100 ms to a second). The browser shows its mic-in-use indicator for
    // that long. While a turn is on its way back (sending / working /
    // speaking: the reply opens listening by itself, with no tap to ask for
    // the mic again) the grace starts over; once the page is hidden it is
    // VOICE_KEEP_HIDDEN_MS (release(ms)) and never starts over.
    // voice.keepMs is VOICE_KEEP_STREAM_MS (tests shorten it). While the
    // wake word's standby runs or wants the microphone (hold()), the page in
    // front, the grace only starts over: the stream is never given back.
    function keep(ms, hidden) {
      clearTimeout(keepTimer);
      keepHidden = !!hidden;
      if (!stream) return;
      keepTimer = setTimeout(function () {
        if (token) return;   // in use again (start() clears the timer; this is a belt)
        if (!document.hidden && (standing || holding)) { keep(); return; }
        if (!document.hidden && (keepHidden || /^(sending|working|speaking)$/.test(voice.state))) {
          keep();
          return;
        }
        release();
      }, ms || voice.keepMs);
    }
    function errCode(err) {
      var n = (err && err.name) || "";
      if (n === "NotAllowedError" || n === "SecurityError" || n === "PermissionDeniedError") return "not-allowed";
      if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "audio-capture";
      return "start-failed";
    }
    function lang() { return String(navigator.language || "en").split(";")[0]; }
    // The server streams (GET /api/voice: the local whisper backend).
    function streams() { return !!(voice.cfg.server_ready && voice.cfg.server_ready.stt_stream); }
    // The recording graph of one clip, and its silence detector: the RMS of
    // every block drives the level meter and ends the clip after a phrase.
    // A context that is not running (made outside a tap) delivers no blocks:
    // then the cap decides, and the clip says so.
    // The phrase, made in start() before the microphone is there: its
    // stream opens alongside getUserMedia (recStart wires the audio in).
    function recNew() {
      return { ctx: null, rate: 0, blocks: [], frames: 0,
               source: null, proc: null, mute: null,
               heard: false, lastLoud: 0, floor: 0, t0: Date.now(), said: "",
               t00: Date.now(), trace: {}, debug: voiceDebugOn(),
               // The stream: mode "clip" (none, or it broke) / "opening" /
               // "stream"; blocks + queued = the audio it has not answered
               // yet; busy = a request in flight (never two); pend = settled
               // words not in the box yet; lead = the words before a clip
               // that took over; words = the caption shows words.
               mode: "clip", sid: "", queued: 0, loudQueued: false, busy: false, opening: false, chunks: 0,
               req: null, ending: false, dead: false, pend: "", partial: "", lead: "", words: false };
    }
    // The caption while nothing has come back: "Hearing you…" while the
    // detector hears speech (and VOICE_HEARING_MS after), else
    // "Listening… <n> s". Once the stream shows words, the caption is theirs.
    function recStatus(g, h) {
      if (!h.onStatus || g.words) return;
      var now = Date.now();
      var t = g.heard && now - g.lastLoud < VOICE_HEARING_MS ? "Hearing you…"
        : "Listening… " + Math.round((now - g.t0) / 1000) + " s";
      if (t !== g.said) { g.said = t; h.onStatus(t); }
    }
    function recStart(g, s, mine, h) {
      var ctx = voiceAudioCtx();
      if (!ctx || !ctx.createScriptProcessor || !ctx.createMediaStreamSource) return false;
      g.ctx = ctx;
      g.rate = ctx.sampleRate;
      g.t0 = Date.now();
      try {
        g.source = ctx.createMediaStreamSource(s);
        g.proc = ctx.createScriptProcessor(4096, 1, 1);
        g.mute = ctx.createGain();
        g.mute.gain.value = 0;
        g.source.connect(g.proc);
        g.proc.connect(g.mute);
        g.mute.connect(ctx.destination);
      } catch (e) { recStop(g); return false; }
      g.proc.onaudioprocess = function (ev) {
        if (token !== mine || rec !== g) return;
        // The engine reuses the input buffer between callbacks: copy it.
        var block = new Float32Array(ev.inputBuffer.getChannelData(0));
        if (!block.length) return;
        g.blocks.push(block);
        g.frames += block.length;
        g.queued += block.length;
        var sum = 0;
        for (var i = 0; i < block.length; i++) sum += block[i] * block[i];
        var rms = Math.sqrt(sum / block.length), now = Date.now();
        // The noise floor follows the quiet moments (and may creep up);
        // speech is well above it.
        g.floor = g.floor === 0 ? rms : Math.min(rms, g.floor * 1.05 + 0.001);
        var loud = rms > Math.max(VOICE_RMS_SPEECH, g.floor * 3);
        if (loud && !g.heard) {
          // The phrase's first word: the trace, and the tick (h.onSpeech).
          voiceTraceMark(g, "speech");
          if (h.onSpeech) h.onSpeech();
          if (token !== mine || rec !== g) return;
        }
        if (loud) { g.heard = true; g.lastLoud = now; g.loudQueued = true; }
        // Before the first word a stream holds only the last VOICE_LEAD_MS
        // (the word's soft start, under the detector): no quiet is uploaded,
        // and the first chunk goes the moment speech starts.
        while (!g.heard && g.mode !== "clip" && g.blocks.length > 1 &&
               (g.queued - g.blocks[0].length) * 1000 >= g.rate * VOICE_LEAD_MS) {
          g.queued -= g.blocks.shift().length;
        }
        if (h.onLevel) h.onLevel(voiceLevelOf(rms, g.floor));
        recStatus(g, h);
        if (g.heard && now - g.lastLoud > VOICE_SILENCE_MS) finish();
        else if (g.mode === "stream") streamPump(g, mine);
      };
      return true;
    }
    function recStop(g) {
      clearInterval(clockTimer);
      clockTimer = null;
      if (!g) return;
      if (g.proc) g.proc.onaudioprocess = null;
      [g.source, g.proc, g.mute].forEach(function (n) { if (n) { try { n.disconnect(); } catch (e) {} } });
      g.source = g.proc = g.mute = null;
    }
    function start(h) {
      stop();
      standbyStop();   // a belt: the state machine pauses the standby before it listens
      if (!available()) { if (h.onError) h.onError("unavailable"); return false; }
      var mine = { h: h };
      token = mine;
      var remaining = h.until ? h.until - Date.now() : VOICE_CLIP_MAX_MS;
      if (remaining < 1500) {
        // The window is about to close: let it (the machine decides what is next).
        setTimeout(function () { if (token === mine) { token = null; if (h.onEnd) h.onEnd(); } }, Math.max(0, remaining));
        return true;
      }
      var capMs = Math.min(streams() ? VOICE_STREAM_MAX_MS : VOICE_CLIP_MAX_MS, remaining);
      // The stream opens now, alongside getUserMedia: its round trip
      // overlaps the microphone's start-up and the first syllable.
      var g = recNew();
      if (streams()) streamOpen(g, mine);
      getStream().then(function (s) {
        if (token !== mine) { if (!token) keep(); return; }   // stopped while the mic came up: its grace starts now
        voiceTraceMark(g, "mic");
        if (!recStart(g, s, mine, h)) {
          token = null;
          streamDrop(g);
          live = null;
          if (h.onError) h.onError("start-failed");
          return;
        }
        rec = g;
        voice.rec.mime = "audio/wav";
        if (h.onStatus) { g.said = "Listening…"; h.onStatus(g.said); }
        clockTimer = setInterval(function () {
          if (token !== mine || rec !== g) { clearInterval(clockTimer); return; }
          recStatus(g, h);
        }, VOICE_LEVEL_MS);
        capTimer = setTimeout(function () {
          if (token !== mine) return;
          // A detector that heard nothing has no words to send (the window
          // may listen again) — unless the stream has shown some.
          if (g.frames && !g.heard && !g.words) { discard(); if (h.onEnd) h.onEnd(); return; }
          finish();
        }, capMs);
      }).catch(function (err) {
        if (token !== mine) return;
        token = null;
        streamDrop(g);
        live = null;
        if (h.onError) h.onError(errCode(err));
      });
      return true;
    }
    // End the clip and send it: the silence detector after a phrase, the tap
    // on the mic or the strip, the cap. The words (or the error) reach
    // start()'s handlers. A streamed phrase sends its rest and ends the
    // stream instead (streamPump).
    function finish() {
      var g = rec, mine = token;
      if (!g || !mine) return false;
      rec = null;
      clearTimeout(capTimer);
      recStop(g);
      keep();
      if (mine.h.onLevel) mine.h.onLevel(null);
      if (!g.frames) {
        token = null;
        streamDrop(g);
        if (mine.h.onError) {
          mine.h.onError("nothing-recorded", g.ctx.state === "running" ? "Nothing recorded"
            : "Nothing recorded: AudioContext " + g.ctx.state + " — tap the mic once");
        }
        return true;
      }
      uploading = true;   // on its way from here: the resample and the upload, or the stream's rest
      g.ending = true;
      if (g.mode === "clip") clipSend(g, mine);
      else streamPump(g, mine);   // once the open / the chunk in flight is back
      return true;
    }
    // The phrase as one clip — or, after a stream broke, the audio it had not
    // answered, behind the words it had (g.lead).
    function clipSend(g, mine) {
      if (!g.queued) { streamDone(g, mine, g.lead); return; }
      if (mine.h.onStatus && !g.words) mine.h.onStatus("Transcribing…");
      var all = new Float32Array(g.queued), at = 0;
      g.blocks.forEach(function (b) { all.set(b, at); at += b.length; });
      g.blocks = [];
      g.queued = 0;
      voiceResampleClip(all, g.rate).then(function (pcm) {
        if (token !== mine) return;   // stopped meanwhile (discard() cleared uploading)
        var wav = voiceWavEncode(pcm, VOICE_WAV_RATE);
        upload(new Blob([wav], { type: "audio/wav" }), "audio/wav", mine, g.lead);
      });
    }
    function upload(blob, mime, mine, lead) {
      var h = mine.h;
      uploading = true;
      aborter = window.AbortController ? new AbortController() : null;
      var t0 = Date.now();
      var timer = aborter ? setTimeout(function () { try { aborter.abort(); } catch (e) {} }, VOICE_UPLOAD_TIMEOUT_MS) : null;
      function settle() { token = null; uploading = false; aborter = null; clearTimeout(timer); }
      fetch("/api/voice/transcribe?lang=" + encodeURIComponent(lang()), {
        method: "POST",
        headers: { "Content-Type": mime },
        body: blob,
        signal: aborter ? aborter.signal : undefined,
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j || {} }; });
      }).then(function (x) {
        if (token !== mine) return;
        settle();
        if (x.r.ok) {
          voice.rec.last = { ms: Number(x.j.ms) || (Date.now() - t0), backend: String(x.j.backend || "server"), error: "", at: Date.now() };
          var text = voiceJoin(lead, x.j.text);
          if (text) { if (h.onFinal) h.onFinal(text); }
          else if (h.onEnd) h.onEnd();     // a clip with no words: the window decides
          return;
        }
        var why = String(x.j.error || ("HTTP " + x.r.status));
        voice.rec.last = { ms: Date.now() - t0, backend: "", error: why, at: Date.now() };
        if (h.onError) {
          h.onError("server", x.r.status === 409
            ? "Server recognition is off: Settings → Voice"
            : "Couldn't transcribe: " + why);
        }
      }).catch(function (err) {
        if (token !== mine) return;
        settle();
        var why = err && err.name === "AbortError" ? "the server took too long" : String((err && err.message) || err);
        voice.rec.last = { ms: Date.now() - t0, backend: "", error: why, at: Date.now() };
        if (h.onError) h.onError("server", "Couldn't reach the server: " + why);
      });
    }
    // --- the stream (see "Streaming" above) ---
    // One request of g's stream, cancellable by streamDrop: {r, j} (j = the
    // JSON body, {} when there is none).
    function streamReq(g, url, opts) {
      var a = window.AbortController ? new AbortController() : null;
      var timer = a ? setTimeout(function () { try { a.abort(); } catch (e) {} }, VOICE_UPLOAD_TIMEOUT_MS) : null;
      g.req = a;
      if (a) opts.signal = a.signal;
      return fetch(url, opts).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j || {} }; });
      }).then(function (x) { clearTimeout(timer); g.req = null; return x; },
              function (err) { clearTimeout(timer); g.req = null; throw err; });
    }
    function streamWhy(x, err) {
      if (x) return String(x.j.error || ("HTTP " + x.r.status));
      return err && err.name === "AbortError" ? "the server took too long" : String((err && err.message) || err);
    }
    function streamUrl(sid, tail) { return "/api/voice/stream/" + encodeURIComponent(sid) + (tail ? "/" + tail : ""); }
    function streamDelete(sid) {
      if (!sid) return;
      try { fetch(streamUrl(sid, ""), { method: "DELETE" }).catch(function () {}); } catch (e) {}
    }
    function streamOpen(g, mine) {
      live = g;
      g.mode = "opening";
      g.busy = g.opening = true;
      streamReq(g, "/api/voice/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lang: lang() }),
      }).then(function (x) {
        g.busy = g.opening = false;
        var id = x.r.ok && x.j.id ? String(x.j.id) : "";
        if (g.dead) { streamDelete(id); return; }
        if (id) { g.sid = id; g.mode = "stream"; voiceTraceMark(g, "open"); streamPump(g, mine); return; }
        // 409: this server does not stream after all — a clip per phrase
        // until GET /api/voice says otherwise.
        if (x.r.status === 409 && voice.cfg.server_ready) voice.cfg.server_ready.stt_stream = false;
        streamFail(g, mine, streamWhy(x));
      }, function (err) {
        g.busy = g.opening = false;
        if (!g.dead) streamFail(g, mine, streamWhy(null, err));
      });
    }
    // The next request, when one is due and none is in flight: a chunk once
    // VOICE_STREAM_CHUNK_MS of new audio is in; once the phrase has ended,
    // the rest (when it holds speech — after the silence stop it is the
    // quiet), then /end. Nothing goes before the first word; the first
    // chunk goes on it (the lead-in recStart kept, VOICE_LEAD_MS, and the
    // loud block — short of the minimum only when the mic has just opened).
    function streamPump(g, mine) {
      if (g.dead || token !== mine || g.mode !== "stream" || g.busy) return;
      if (g.ending) {
        if (g.queued && g.loudQueued) streamChunk(g, mine); else streamEnd(g, mine);
        return;
      }
      if (g.heard && (!g.chunks || g.queued * 1000 >= g.rate * VOICE_STREAM_CHUNK_MS)) streamChunk(g, mine);
    }
    // The samples since the last cut, as a 16 kHz WAV (resampled as a clip
    // is; one shorter than the server's minimum padded with silence — the
    // first chunk BEFORE its audio, where the quiet was, the rest after).
    function streamChunk(g, mine) {
      var n = g.blocks.length, frames = g.queued, all = new Float32Array(frames), at = 0, t0 = Date.now();
      for (var i = 0; i < n; i++) { all.set(g.blocks[i], at); at += g.blocks[i].length; }
      var first = !g.chunks;
      g.chunks++;
      g.busy = true;
      g.loudQueued = false;
      voiceResampleClip(all, g.rate).then(function (pcm) {
        if (g.dead) return null;
        var min = Math.ceil((VOICE_WAV_RATE * VOICE_STREAM_CHUNK_MS) / 1000);
        if (pcm.length < min) { var pad = new Float32Array(min); pad.set(pcm, first ? min - pcm.length : 0); pcm = pad; }
        return streamReq(g, streamUrl(g.sid, "chunk"), {
          method: "POST",
          headers: { "Content-Type": "audio/wav" },
          body: new Blob([voiceWavEncode(pcm, VOICE_WAV_RATE)], { type: "audio/wav" }),
        });
      }).then(function (x) {
        if (!x || g.dead) return;
        g.busy = false;
        if (!x.r.ok) { streamFail(g, mine, streamWhy(x)); return; }
        g.blocks.splice(0, n);
        g.queued -= frames;
        voice.rec.chunk = { ms: Number(x.j.ms) || (Date.now() - t0), audio: Number(x.j.audio_s) || 0, error: "", at: Date.now() };
        streamWords(g, mine, x.j.committed, x.j.partial);
        streamPump(g, mine);
      }, function (err) {
        if (g.dead) return;
        g.busy = false;
        streamFail(g, mine, streamWhy(null, err));
      });
    }
    // A chunk's words: the settled ones into the box as they come (unless
    // the phrase must arrive whole), all but the held-back end
    // (voiceStreamSplit); what is held and the unstable tail, the caption.
    function streamWords(g, mine, committed, partial) {
      var h = mine.h;
      if (String(committed || "").trim()) voiceTraceMark(g, "settled");
      g.pend = voiceJoin(g.pend, committed);
      g.partial = String(partial || "").trim();
      // A stop phrase acts the moment it settles, mid-phrase or not.
      var tail = voiceTail(g.pend, false);
      if (tail && tail.kind === "stop") {
        var all = g.pend;
        g.pend = "";
        if (h.onFinal) h.onFinal(all);
        return;
      }
      if (!(h.whole && h.whole())) {
        var cut = voiceStreamSplit(g.pend, !!(h.answers && h.answers()));
        if (cut[0]) {
          g.pend = cut[1];
          if (h.onFinal) h.onFinal(cut[0]);
          if (g.dead || token !== mine) return;
        }
      }
      // The settled-but-held words, then the partial (shown greyed: it may
      // still change) — from the first chunk's answer on.
      var shown = voiceJoin(g.pend, g.partial);
      if (shown) {
        g.words = true;
        voiceTraceMark(g, "words");
        if (h.onInterim) h.onInterim(shown, g.partial);
      }
    }
    // The phrase is over: /end flushes the words the stream still holds.
    function streamEnd(g, mine) {
      var t0 = Date.now();
      g.busy = true;
      streamReq(g, streamUrl(g.sid, "end"), { method: "POST" }).then(function (x) {
        if (g.dead) return;
        g.busy = false;
        if (!x.r.ok) { streamFail(g, mine, streamWhy(x)); return; }
        g.sid = "";   // ended: nothing to DELETE
        voice.rec.last = { ms: Number(x.j.ms) || (Date.now() - t0), backend: String(x.j.backend || "stream"), error: "", at: Date.now() };
        streamDone(g, mine, voiceJoin(g.pend, x.j.committed));
      }, function (err) {
        if (g.dead) return;
        g.busy = false;
        streamFail(g, mine, streamWhy(null, err));
      });
    }
    // The stream broke (or never opened): the phrase goes on as a clip. The
    // words it settled stay (in the box, or g.lead with the unstable tail);
    // the audio it had not answered is the clip.
    function streamFail(g, mine, why) {
      voice.rec.chunk = { ms: 0, audio: 0, error: why, at: Date.now() };
      var sid = g.sid;
      g.sid = "";
      g.mode = "clip";
      streamDelete(sid);
      g.lead = voiceJoin(g.pend, g.partial);
      g.pend = g.partial = "";
      if (g.ending && token === mine) clipSend(g, mine);
    }
    // The phrase's last words (none: the window decides, as for a clip).
    function streamDone(g, mine, text) {
      if (token !== mine) return;
      var h = mine.h;
      live = null;
      token = null;
      uploading = false;
      if (text) { if (h.onFinal) h.onFinal(text); }
      else if (h.onEnd) h.onEnd();
    }
    // Abandoned (stop()): DELETE the stream — an open still on its way
    // deletes the id it gets — and drop the request in flight.
    function streamDrop(g) {
      if (!g || g.dead) return;
      g.dead = true;
      if (g.req && !g.opening) { try { g.req.abort(); } catch (e) {} }
      streamDelete(g.sid);
      g.sid = "";
    }
    function discard() {
      token = null;
      clearTimeout(capTimer);
      var g = rec;
      rec = null;
      recStop(g);
      streamDrop(live);
      live = null;
      if (uploading) { uploading = false; if (aborter) { try { aborter.abort(); } catch (e) {} } aborter = null; }
      keep();
    }
    // stop(): drop whatever is going on — the clip, the upload, the stream —
    // and report nothing. The microphone stays warm for the next turn (keep()).
    function stop() { discard(); }
    // release(): stop() and give the microphone back (voice off); with ms
    // (the page hid), give it back that long from now unless it is used again.
    function release(ms) {
      discard();
      standbyStop();
      if (ms > 0) { keep(ms, true); return; }
      clearTimeout(keepTimer);
      var s = stream;
      stream = null;
      micStop(s);
    }
    // --- the wake word's standby (voiceWakeSync drives it) ---
    // The warm stream → its own ScriptProcessor(VOICE_WAKE_BLOCK) → the
    // lone-utterance detector (voiceWakeStep), on the phone: nothing leaves
    // it until a lone utterance ends, and then only that clip (+ its
    // lead-in), resampled and encoded as a recording's clip is, handed to
    // h.onClip(blob) — if h.wanted() still says a check may go (the rate
    // limit, one in flight). h.onLevel feeds the mic's ring; h.onReady(ctx)
    // once the audio flows (or not: a context that is not running delivers
    // no blocks); h.onError(code) with start()'s codes. The blocks a clip
    // can reach are all it keeps.
    function standby(h) {
      if (standing) { standing.h = h; return true; }
      if (token || !available()) return false;
      var w = standing = { h: h, d: voiceWakeNew(), blocks: [], rate: 0, source: null, proc: null, mute: null };
      getStream().then(function (s) {
        if (standing !== w) { if (!token && !standing) keep(); return; }
        var ctx = voiceAudioCtx();
        try {
          w.rate = ctx.sampleRate;
          w.source = ctx.createMediaStreamSource(s);
          w.proc = ctx.createScriptProcessor(VOICE_WAKE_BLOCK, 1, 1);
          w.mute = ctx.createGain();
          w.mute.gain.value = 0;
          w.source.connect(w.proc);
          w.proc.connect(w.mute);
          w.mute.connect(ctx.destination);
        } catch (e) {
          standbyDrop(w);
          standing = null;
          if (h.onError) h.onError("start-failed");
          return;
        }
        w.proc.onaudioprocess = function (ev) { if (standing === w) standbyBlock(w, ev.inputBuffer.getChannelData(0)); };
        if (w.h.onReady) w.h.onReady(ctx);
      }).catch(function (err) {
        if (standing !== w) return;
        standing = null;
        if (w.h.onError) w.h.onError(errCode(err));
      });
      return true;
    }
    function standbyBlock(w, data) {
      var block = new Float32Array(data);   // the engine reuses the buffer
      if (!block.length) return;
      var sum = 0;
      for (var i = 0; i < block.length; i++) sum += block[i] * block[i];
      var rms = Math.sqrt(sum / block.length), h = w.h;
      w.blocks.push({ at: w.d.t, x: block });
      var r = voiceWakeStep(w.d, rms, (block.length * 1000) / w.rate);
      var reach = VOICE_WAKE_MAX_MS + VOICE_WAKE_QUIET_MS + VOICE_WAKE_LEAD_MS + 500;
      while (w.blocks.length && w.blocks[0].at < w.d.t - reach) w.blocks.shift();
      if (h.onLevel) h.onLevel(voiceLevelOf(rms, w.d.floor));
      if (!r || r.kind !== "clip" || (h.wanted && !h.wanted())) return;
      voiceResampleClip(voiceWakeSlice(w.blocks, r.from, r.to, w.rate), w.rate).then(function (pcm) {
        if (standing !== w) return;
        if (w.h.onClip) w.h.onClip(new Blob([voiceWavEncode(pcm, VOICE_WAV_RATE)], { type: "audio/wav" }), r);
      });
    }
    function standbyDrop(w) {
      if (w.proc) w.proc.onaudioprocess = null;
      [w.source, w.proc, w.mute].forEach(function (n) { if (n) { try { n.disconnect(); } catch (e) {} } });
      w.source = w.proc = w.mute = null;
    }
    // Pause / stop the standby: the stream stays warm (keep()'s grace; hold()
    // keeps it while the standby wants it back).
    function standbyStop() {
      var w = standing;
      standing = null;
      if (!w) return;
      standbyDrop(w);
      if (!token) keep();
    }
    function standbyOn() { return !!standing; }
    // The standby wants the microphone (on, the page in front), running or
    // paused: the grace never gives it back meanwhile. Off: the grace that
    // is already running (keep() re-arms it while held; a hidden page's is
    // VOICE_KEEP_HIDDEN_MS) gives it back when it ends.
    function hold(on) { holding = !!on; }
    function active() { return !!token; }
    function pending() { return uploading; }
    function warm() { return !!stream; }
    return { name: "server", available: available, start: start, stop: stop, finish: finish,
             active: active, pending: pending, release: release, warm: warm,
             standby: standby, standbyStop: standbyStop, standbyOn: standbyOn, hold: hold };
  }

  // --- ServerTTS: POST /api/voice/speak → Web Audio --------------------------
  // The server's voice (piper, an OpenAI-compatible host…) played through ONE
  // AudioContext — never an <audio> element, which kills iOS recognition when
  // it finishes. prime() creates/resumes that context synchronously inside
  // the tap (iOS and Chrome keep a context made outside a gesture suspended)
  // and primes the phone's voice too: it is the fallback for every utterance
  // the server cannot say (409/502, network, a suspended context), and what
  // went wrong shows in Diagnostics. Text goes in sentence groups of at most
  // VOICE_SPEAK_MAX_CHARS (the route's cap); each resolves on `ended` or the
  // clip's own duration plus a grace, like PhoneTTS; cancel() stops the
  // current source and drops the queue.
  var VOICE_SPEAK_MAX_CHARS = 1200;
  // The one AudioContext of the page, made (or resumed) inside a gesture:
  // the server adapters play and meter through it. Synchronous on purpose.
  function voiceAudioCtx() {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    if (!voice.audioCtx) { try { voice.audioCtx = new AC(); } catch (e) { return null; } }
    if (voice.audioCtx.state === "suspended") { try { voice.audioCtx.resume(); } catch (e) {} }
    return voice.audioCtx;
  }
  // Sentences packed into groups of at most `max` characters, in order.
  function sentenceGroups(text, max) {
    var out = [], cur = "";
    sentenceChunks(text).forEach(function (s) {
      if (cur && (cur + " " + s).length > max) { out.push(cur); cur = s; }
      else cur = cur ? cur + " " + s : s;
      while (cur.length > max) { out.push(cur.slice(0, max)); cur = cur.slice(max).trim(); }
    });
    if (cur) out.push(cur);
    return out;
  }
  function ServerTTS(phone) {
    var gen = 0;          // cancel() bumps it: an older speak()'s chain stops
    var playing = null;   // the BufferSource being played
    function available() { return !!(window.AudioContext || window.webkitAudioContext) && !!window.fetch; }
    // SYNCHRONOUS, inside the tap: the context first, then the phone's voice
    // (the fallback must be unlocked by the same gesture).
    function prime() {
      voiceAudioCtx();
      phone.prime();
    }
    function decode(ctx, buf) {
      return new Promise(function (resolve, reject) {
        try {
          var p = ctx.decodeAudioData(buf, resolve, reject);   // callback form: older Safari
          if (p && p.then) p.then(resolve, reject);
        } catch (e) { reject(e); }
      });
    }
    function play(ctx, audio) {
      return new Promise(function (resolve) {
        var src;
        try { src = ctx.createBufferSource(); src.buffer = audio; } catch (e) { resolve(); return; }
        var rate = voice.prefs.rate || 1;
        try { src.playbackRate.value = rate; } catch (e) {}
        try { src.connect(ctx.destination); } catch (e) {}
        var done = false, timer = null;
        function finish() {
          if (done) return;
          done = true;
          clearTimeout(timer);
          if (playing === src) playing = null;
          resolve();
        }
        src.onended = finish;
        timer = setTimeout(finish, ((Number(audio && audio.duration) || 1) * 1000) / rate + VOICE_CHUNK_GRACE_MS);
        playing = src;
        try { src.start(0); } catch (e) { finish(); }
      });
    }
    function note(ms, backend, error) {
      voice.rec.lastSpeak = { ms: ms, backend: backend || "", error: error || "", at: Date.now() };
    }
    function sayOne(part, g) {
      var ctx = voiceAudioCtx();
      if (!ctx || ctx.state !== "running") {
        note(0, "", "AudioContext " + (ctx ? ctx.state : "missing") + " — tap the mic once; the phone's voice spoke");
        return phone.speak(part);
      }
      var t0 = Date.now(), backend = "server";
      return fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: part }),
      }).then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            throw new Error((j && j.error) || ("HTTP " + r.status));
          });
        }
        backend = r.headers.get("X-Voice-Backend") || "server";
        return r.arrayBuffer();
      }).then(function (buf) {
        return decode(ctx, buf);
      }).then(function (audio) {
        note(Date.now() - t0, backend, "");
        if (g !== gen) return;
        return play(ctx, audio);
      }).catch(function (err) {
        // 409 (turned off meanwhile), 502 (the backend), the network, a clip
        // that would not decode: the phone's voice says this one.
        note(Date.now() - t0, "", String((err && err.message) || err));
        if (g !== gen) return;
        return phone.speak(part);
      });
    }
    function speak(text) {
      var parts = sentenceGroups(text, VOICE_SPEAK_MAX_CHARS);
      if (!available() || !parts.length) return Promise.resolve();
      var g = gen, i = 0;
      return new Promise(function (resolve) {
        function next() {
          if (g !== gen || i >= parts.length) { resolve(); return; }
          sayOne(parts[i++], g).then(next, next);
        }
        next();
      });
    }
    function cancel() {
      gen++;
      var s = playing;
      playing = null;
      if (s) { try { s.onended = null; s.stop(0); } catch (e) {} }
      phone.cancel();
    }
    return { name: "server", available: available, prime: prime, speak: speak, cancel: cancel, voices: phone.voices };
  }

  // Split for speaking: one chunk per sentence, long sentences cut at commas
  // past ~180 chars (some engines stop mid-way on long utterances).
  function sentenceChunks(text) {
    var s = String(text || "").replace(/\s+/g, " ").trim();
    if (!s) return [];
    var out = [];
    // A sentence ends at . ! or ? (plus closing quotes) followed by a space
    // or the end — never inside a token: "note.txt", "v2.1" and "e.g." are
    // said whole, not as "note." "txt.".
    var pieces = [], start = 0;
    for (var i = 0; i < s.length; i++) {
      if (".!?".indexOf(s.charAt(i)) < 0) continue;
      var j = i + 1;
      while (j < s.length && ".!?\"')]".indexOf(s.charAt(j)) >= 0) j++;
      if (j >= s.length || s.charAt(j) === " ") { pieces.push(s.slice(start, j)); start = j; i = j - 1; }
    }
    if (start < s.length) pieces.push(s.slice(start));
    pieces.forEach(function (sent) {
      sent = sent.trim();
      if (!sent) return;
      while (sent.length > 180) {
        var cut = Math.max(sent.lastIndexOf(", ", 180), sent.lastIndexOf("; ", 180));
        if (cut < 40) cut = sent.lastIndexOf(" ", 180);
        if (cut < 40) cut = 180;
        out.push(sent.slice(0, cut + 1).trim());
        sent = sent.slice(cut + 1).trim();
      }
      if (sent) out.push(sent);
    });
    return out;
  }
  // Markdown → words a voice can say: no code, links, headings, bullets,
  // tables or emphasis marks.
  function stripMarkdown(text) {
    var s = String(text || "");
    s = s.replace(/```[\s\S]*?```/g, " ");
    s = s.replace(/`[^`\n]*`/g, " ");
    s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, " ");
    s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");
    s = s.replace(/https?:\/\/\S+/g, " ");
    s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");
    s = s.replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, "");
    s = s.replace(/^\s*>\s?/gm, "");
    s = s.replace(/^\s*\|.*\|\s*$/gm, " ");
    s = s.replace(/(\*{1,3}|_{1,3}|~~)(\S[^*_~]*?)\1/g, "$2");
    s = s.replace(/[*_#>|]+/g, " ");
    return s.replace(/\s+/g, " ").trim();
  }
  function endSentence(text) {
    var s = String(text || "").trim();
    return !s || /[.!?]$/.test(s) ? s : s + ".";
  }
  // sessionState's label as a voice says it ("Question" is a chip, not a sentence).
  function voiceStateWord(ss) {
    if (ss.reason === "question") return "waiting on a question";
    if (ss.reason === "precommit_failed") return "stuck on a failed pre-commit";
    if (ss.reason === "checks_failing") return "waiting on failing checks";
    if (ss.reason === "setup_failed") return "stuck on a failed setup";
    return String(ss.label || "").toLowerCase();
  }
  function firstSentence(text) {
    var s = stripMarkdown(text);
    var m = /^(.*?[.!?])(?:\s|$)/.exec(s);
    var out = m ? m[1] : s;
    if (out.length > 220) out = out.slice(0, 217).replace(/\s+\S*$/, "") + "…";
    return out;
  }

  var voice = {
    state: "off",
    session: "",                 // the title voice is on for (== current while on)
    stt: null,
    tts: null,
    prefs: { rate: 1, voiceName: "", handsFree: false, ptt: "toggle", replies: "mine", wake: false },   // replies: off | mine | all; wake: listen for "MindFlock"
    dictated: false,             // a dictated phrase is in the box (since it was last emptied / sent)
    expectSay: null,             // the last prompt sent asked for a Say line (null: none sent here yet)
    lastPrompt: null,            // Diagnostics: {dictated, say} of that prompt
    lastRoute: "",               // Diagnostics: what /api/voice/route last said (or why it did not)
    cfg: { stt: "phone", tts: "phone", approve_dialogs: true, loaded: false,
           server_ready: {}, backends: { stt: [], tts: [] }, system: null, say_all: null },
    sayAll: { synced: false, denied: false },   // the say-all marker: set from the pref once this load; a 403 said once
    audioCtx: null,              // the page's one AudioContext (voiceAudioCtx)
    rec: { mime: "", last: null, lastSpeak: null, resampler: "", chunk: null, trace: null },   // Diagnostics: recorder type, last transcribe / speak, resampler, last stream chunk, last start-up trace
    keepMs: VOICE_KEEP_STREAM_MS,   // how long the server recognizer keeps the microphone warm (tests shorten it)
    buzzAt: 0,                   // the last haptic cue (the listening buzz, the first-word tick)
    sttHinted: false,            // "server recognition isn't ready" said once
    saved: null,                 // {session, on} from localStorage, for "Tap to resume voice"
    lastSay: { text: "", ts: 0 }, // never speak the same say twice
    asked: false,                // that say asked a question: "yes" / "no" answer it (voiceAsks)
    lastLine: "",                // "say that again": the last line said, as said
    heard: {},                   // Everything: title → the Say line read for its last turn (voiceTurnLine)
    lastError: "",
    pendingSay: null,            // a reply that arrived mid-dictation, spoken when idle
    box: null,                   // where dictation goes: null = the compose box; the ask sheet's field while it listens (vbox)
    dialog: null,
    dialogKey: "",               // the dialog id read out (or "none"), so a poll never re-reads it
    dialogTries: 0,
    dialogBusy: false,
    wake: null,
    events: null,
    eventsState: "off",
    eventsSeq: 0,
    bootId: "",                  // the server's boot id from hello: a change means "forget eventsSeq"
    eventsBackoff: 1000,
    eventsTimer: null,
    diagTimer: null,
    primed: false,
    armed: false,                // the ?voice=1 "Tap to hear" was offered
    resumeOffered: false,
    hearPending: null,
    deepLink: new URLSearchParams(location.search).get("voice") === "1",
    onEvent: null,               // set below; tests drive it with a frame
  };
  voice.stt = PhoneSTT();
  voice.tts = PhoneTTS();
  var voiceAdapters = { phoneStt: voice.stt, phoneTts: voice.tts, serverStt: null, serverTts: null };
  voiceAdapters.serverStt = ServerSTT();
  voiceAdapters.serverTts = ServerTTS(voice.tts);
  voice.levelOf = voiceLevelOf;       // the ring's math, pure (tests call it)
  voice.levelStep = voiceLevelStep;
  window.mfVoice = voice;

  // --- persistence (per browser; nothing else is stored) --------------------
  function voiceLoadPrefs() {
    try {
      var j = JSON.parse(localStorage.getItem(VOICE_STORE) || "{}");
      if (!j || typeof j !== "object") return;
      if (j.rate) voice.prefs.rate = Math.min(2, Math.max(0.5, Number(j.rate) || 1));
      if (typeof j.voiceName === "string") voice.prefs.voiceName = j.voiceName;
      voice.prefs.handsFree = !!j.handsFree;
      voice.prefs.ptt = j.ptt === "hold" ? "hold" : "toggle";
      voice.prefs.replies = voiceRepliesPref(j.replies);
      voice.prefs.wake = j.wake === true;
      voice.saved = { session: String(j.session || ""), on: !!j.on };
    } catch (e) {}
  }
  // The Spoken replies setting from storage: an older page stored a boolean.
  function voiceRepliesPref(v) {
    if (v === false || v === "off") return "off";
    return v === "all" ? "all" : "mine";
  }
  function voicePersist() {
    try {
      localStorage.setItem(VOICE_STORE, JSON.stringify({
        session: voice.session,
        on: voice.state !== "off",
        rate: voice.prefs.rate,
        voiceName: voice.prefs.voiceName,
        handsFree: voice.prefs.handsFree,
        ptt: voice.prefs.ptt,
        replies: voice.prefs.replies,
        wake: voice.prefs.wake,
      }));
    } catch (e) {}
  }

  // --- the strip, the mic, captions -----------------------------------------
  var VOICE_WORDS = {
    idle: "Your turn", listening: "Listening…", sending: "Sending…",
    working: "Working…", speaking: "Speaking…", awaiting: "Listening…", dialog: "Waiting on you",
  };
  function voiceBtnState(s) {
    if (s === "off") return "off";
    if (s === "listening" || s === "awaiting" || s === "dialog") return "listening";
    if (s === "speaking") return "speaking";
    if (s === "working" || s === "sending") return "working";
    return "idle";
  }
  function voiceSetState(s, caption) {
    voice.state = s;
    voiceBtn.dataset.state = voiceBtnState(s);
    voiceBtn.setAttribute("aria-label",
      s === "off" ? "Talk to this session" : s === "dialog" ? "Stop listening"
      : voiceIsListening() ? (voiceListen_.held ? "Release to keep it" : "Tap to keep it") : voiceTalkHint());
    voiceStrip.classList.toggle("hidden", s === "off" && !voice.hearPending && !voiceSpeech.playing);
    voiceStateEl.textContent = VOICE_WORDS[s] || "";
    if (caption !== undefined) voiceCaption(caption);
    if (!voiceIsListening()) voiceLevel(null);
    voiceCancelBtn.classList.toggle("hidden", s === "off" || s === "idle");
    if (!voiceSheet.classList.contains("hidden")) voiceOnToggle.checked = s !== "off";
    fitSoon();   // the strip takes terminal rows
    askVoiceMirror();
    // Out of listening: the lines that waited for the mic go on.
    if (voiceSpeech.q.length && !voiceSpeech.playing && !voiceIsListening()) {
      clearTimeout(voiceSpeech.timer);
      voiceSpeech.timer = setTimeout(voiceSpeechPump, 0);
    }
    voiceWakeSync();   // the wake word's standby follows the state
  }
  // tail: the end of text still settling (a stream's partial) — greyed
  // after the settled words; the caption's text is the whole either way.
  function voiceCaption(text, tail) {
    text = text || "";
    tail = tail ? String(tail) : "";
    if (!tail || text.length < tail.length || text.slice(text.length - tail.length) !== tail) {
      voiceCaptionEl.textContent = text;
    } else {
      voiceCaptionEl.textContent = text.slice(0, text.length - tail.length);
      var span = document.createElement("span");
      span.className = "voice-partial";
      span.textContent = tail;
      voiceCaptionEl.appendChild(span);
    }
    askVoiceMirror();
  }
  function voiceError(msg) {
    voice.lastError = msg || "";
    voiceErrEl.textContent = msg || "";
    if (voiceErrEl.classList.contains("hidden") === !msg) return;
    voiceErrEl.classList.toggle("hidden", !msg);
    fitSoon();   // the line takes terminal rows, as the strip does
  }
  function voiceIsListening() {
    return voice.state === "listening" || voice.state === "awaiting" || voice.state === "dialog";
  }
  // The box dictation fills: the compose box, or the ask sheet's answer
  // field while its mic listens (voice.box). Only the compose box autosizes.
  function vbox() { return voice.box || composeEl; }
  function vboxSized() { if (vbox() === composeEl) autosizeCompose(); }
  // The gesture, named: what starts talking, and what ends it (keeping the words).
  function voiceTalkHint() { return voice.prefs.ptt === "hold" ? "Hold the mic to talk" : "Tap to talk"; }
  function voiceDictHint() { return voiceListen_.held ? VOICE_HINT_HOLD : VOICE_HINT_TAP; }
  // The level meter under the strip and the ring round the mic (the ask
  // sheet's too) while the server adapter records: v is the latest block's
  // level (voiceLevelOf, 0..1); null hides the meter and drops the ring.
  // A requestAnimationFrame loop eases the shown level toward it
  // (voiceLevelStep) and writes one CSS variable, --lvl, which the CSS turns
  // into transforms — no layout. It runs only while there is a level to show.
  // ring: the ring alone, never the meter (the wake word's standby).
  var voiceRing = { shown: 0, target: 0, at: 0, raf: 0, painted: "", hearing: false };
  function voiceLevel(v, ring) {
    var R = voiceRing;
    if (v === null || v === undefined || v < 0) {
      if (!voiceLevelEl.classList.contains("hidden")) { voiceLevelEl.classList.add("hidden"); fitSoon(); }
      if (R.raf) { cancelAnimationFrame(R.raf); R.raf = 0; }
      R.shown = R.target = 0;
      voiceRingPaint(0);
      return;
    }
    if (!ring && voiceLevelEl.classList.contains("hidden")) { voiceLevelEl.classList.remove("hidden"); fitSoon(); }
    R.target = Math.min(1, Math.max(0, Number(v) || 0));
    if (!R.raf && window.requestAnimationFrame) { R.at = Date.now(); R.raf = requestAnimationFrame(voiceRingFrame); }
  }
  function voiceRingFrame() {
    var R = voiceRing, now = Date.now();
    R.raf = 0;
    R.shown = voiceLevelStep(R.shown, R.target, now - R.at);
    R.at = now;
    if (R.shown < 0.005 && R.target === 0) R.shown = 0;
    voiceRingPaint(R.shown);
    if (R.shown > 0 || R.target > 0) R.raf = requestAnimationFrame(voiceRingFrame);
  }
  // --lvl on the mic, the ask sheet's mic and the meter (the bar inherits
  // it); data-hearing while the level shows a voice (the CSS stops the
  // "waiting" pulse then). Written only when it changes.
  function voiceRingPaint(v) {
    var R = voiceRing, s = v.toFixed(2), hearing = v >= 0.05;
    if (s !== R.painted) {
      R.painted = s;
      [voiceBtn, voiceLevelEl, askMicBtn].forEach(function (el) { if (el) el.style.setProperty("--lvl", s); });
    }
    if (hearing !== R.hearing) {
      R.hearing = hearing;
      [voiceBtn, askMicBtn].forEach(function (el) { if (el) el.toggleAttribute("data-hearing", hearing); });
    }
  }
  // A phrase's first word (the server adapter's onSpeech): a short tick on
  // Android — not when the listening buzz has just gone (two in a row read
  // as one long buzz, and that one already said "go").
  var VOICE_TICK_GAP_MS = 700;
  function voiceSpeechTick() {
    if (!ANDROID || !navigator.vibrate || Date.now() - voice.buzzAt < VOICE_TICK_GAP_MS) return;
    voice.buzzAt = Date.now();
    try { navigator.vibrate(12); } catch (e) {}
  }
  // The tap on the mic or the strip while a DIALOG is listening: a clip being
  // recorded is finished and transcribed first (its words count), then
  // listening stops.
  function voiceListenDone() {
    var s = voice.stt;
    if (s.finish && s.active() && !(s.pending && s.pending()) && s.finish()) { voiceListen_.stopAfter = true; return; }
    voiceListenStop();
  }
  // Listening ends with words in the box but no "over" (the silence stop):
  // nothing is sent, nothing is lost — Send sends them, the mic goes on
  // adding to them.
  function voiceListenStop() {
    voiceListen_.until = 0;
    voiceListen_.stopAfter = false;
    voiceListen_.overAfter = false;
    voiceIdle(vbox().value.trim() ? "Kept, not sent — Send sends it, or tap the mic to go on" : undefined);
  }
  // "over" (said, or the push-to-talk gesture): listening stops, the words
  // stay in the box, nothing is sent — "submit" or the Send button sends.
  function voiceListenKept() {
    voiceListen_.until = 0;
    voiceListen_.stopAfter = false;
    voiceListen_.overAfter = false;
    voiceIdle(vbox().value.trim() ? VOICE_KEPT : undefined);
  }
  // Push to talk ends a prompt like "over": the second tap (toggle),
  // letting go (hold). What was said is still on its way — the phone's last
  // phrase (end(): the recognizer hands it over, VOICE_OVER_WAIT_MS at
  // most), a server clip being transcribed (pending(), however long it
  // takes), a route answer — so listening stops once nothing more is
  // coming, the words in the box, unsent (a late "submit" still sends).
  function voiceListenOver() {
    var L = voiceListen_, s = voice.stt;
    if (L.mode === "dialog") { voiceListenDone(); return; }
    L.overAfter = true;
    L.until = 0;                 // no more restarts
    L.overBy = Date.now() + VOICE_OVER_WAIT_MS;
    if (s.active() && !(s.pending && s.pending())) {
      if (s.finish) s.finish(); else if (s.end) s.end();
    }
    voiceListenFlushSoon();
  }
  function voiceListenFlushSoon() {
    var L = voiceListen_, s = voice.stt;
    clearTimeout(L.timer);
    if (!voiceIsListening() || !L.overAfter) return;
    if (L.routing || (s.pending && s.pending()) || (s.active() && Date.now() < L.overBy)) {
      L.timer = setTimeout(voiceListenFlushSoon, 100);
      return;
    }
    voiceListenFlush();
  }
  // The words are in: stop, keep them. Never a send.
  function voiceListenFlush() {
    var L = voiceListen_;
    L.overAfter = false;
    clearTimeout(L.timer);
    voice.stt.stop();
    voiceListenKept();
  }
  function voiceIdle(caption) {
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    if (voice.state === "off") return;
    voiceSetState("idle", caption === undefined ? voiceTalkHint() : caption);
    if (voice.pendingSay) {
      var p = voice.pendingSay;
      voice.pendingSay = null;
      voiceReply(p);
    }
  }

  // --- speaking: one voice at a time ------------------------------------------
  // speakLine QUEUES a line; the queue says one at a time, in arrival
  // order, so two sessions never talk over each other. The promise resolves
  // true once the line has been said — false when it never was (then the
  // caller does nothing): Cancel, voice off and the hidden page drop every
  // line (voiceSpeechStop(true)); a tap on the mic or a move to another
  // session stops the line being said and drops the conversation's queued
  // lines, while the lines beside it wait (voiceSpeechStop(false)); a hand
  // on the ask sheet drops the lines about the ask shown, others wait
  // (voiceSpeechStopFor); the mic while a line is said cuts it before it
  // listens (voiceBargeIn); "stop talking" drops them all, the mic going
  // on (voiceHush); a line older than VOICE_LINE_MAX_AGE_MS when its turn
  // comes is dropped.
  //   keepState  a line BESIDE the conversation ("Sent to X.", the ack,
  //              another session's ask or turn): the strip keeps its state
  //   title      the session the line is about: its name goes first when
  //              the phone is on another one (voiceLineText)
  // Recognition is stopped while a line is said (the phone must not
  // transcribe its own voice) and the queue holds while the mic listens.
  var voiceSpeakSeq = 0;   // bumped by every stop: a conversation line queued before it is not said
  var voiceSpeech = { q: [], playing: null, drained: [], timer: null };
  voice.speech = voiceSpeech;   // tests read the queue
  // at: when the line came to be (default now) — a turn replayed after a
  // reconnect is as old as its frame, so the age rule can drop it.
  function speakLine(text, keepState, title, at) {
    text = String(text || "").trim();
    if (!text) return Promise.resolve(true);
    var bg = !!keepState || voice.state === "off";
    if (!bg) {
      clearTimeout(voiceListen_.timer);
      voice.stt.stop();
      voiceSetState("speaking");
    }
    return new Promise(function (resolve) {
      voiceSpeech.q.push({ text: text, title: String(title || ""), at: Math.min(Date.now(), Number(at) || Date.now()), bg: bg, seq: voiceSpeakSeq, resolve: resolve });
      voiceSpeechPump();
    });
  }
  // The queue's next step, pure: {next: the line to say now (or null),
  // drop: the lines too old to say}. Strict arrival order; nothing while a
  // line is playing or the mic listens (o.hold); a line older than o.maxAge
  // when its turn comes is dropped, never said late.
  function voiceQueueStep(q, now, o) {
    var drop = [];
    if (o.playing || o.hold) return { next: null, drop: drop };
    for (var i = 0; i < q.length; i++) {
      if (now - q[i].at > o.maxAge) { drop.push(q[i]); continue; }
      return { next: q[i], drop: drop };
    }
    return { next: null, drop: drop };
  }
  // A queued line as said, pure: the session's name first when the line is
  // about another session than `here` (and does not start with the name
  // already); the phone's own session, and a line about none, bare.
  function voiceLineText(line, here, name) {
    var text = String(line.text || "").trim();
    if (!line.title || line.title === here) return text;
    var n = String(name(line.title) || line.title);
    return voiceNorm(text).indexOf(voiceNorm(n)) === 0 ? text : n + ": " + text;
  }
  function voiceSpeechTake(line) {
    var i = voiceSpeech.q.indexOf(line);
    if (i >= 0) voiceSpeech.q.splice(i, 1);
  }
  function voiceSpeechPump() {
    var S = voiceSpeech;
    clearTimeout(S.timer);
    var step = voiceQueueStep(S.q, Date.now(), { playing: !!S.playing, hold: voiceIsListening(), maxAge: VOICE_LINE_MAX_AGE_MS });
    step.drop.forEach(function (x) {
      voiceSpeechTake(x);
      try { console.debug("mindflock voice: dropped a line older than " + VOICE_LINE_MAX_AGE_MS / 1000 + " s:", x.text); } catch (e) {}
      x.resolve(false);
    });
    var line = step.next;
    if (!line) {
      if (!S.playing && !S.q.length) voiceSpeechDrained();
      return;
    }
    voiceSpeechTake(line);
    if (!line.bg && line.seq !== voiceSpeakSeq) { line.resolve(false); voiceSpeechPump(); return; }
    S.playing = line;
    voiceHushShow();
    var tok = line.tok = (line.tok || 0) + 1;   // a line stopped and said again: the old play's end is not this one's
    var said = voiceLineText(line, current, nameOf);
    voice.lastLine = said;
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    voiceCaption(said);
    // Chrome swallows a speak() issued in the same tick as a cancel().
    setTimeout(function () {
      if (S.playing !== line || line.tok !== tok) return;
      var end = function () { voiceSpeechEnd(line, tok); };
      voice.tts.speak(said).then(end, end);
    }, 60);
  }
  function voiceSpeechEnd(line, tok) {
    var S = voiceSpeech;
    if (S.playing !== line || line.tok !== tok) return;
    S.playing = null;
    voiceHushShow();
    line.resolve(true);
    // After the caller's own next step (a listen, idle), which runs first.
    clearTimeout(S.timer);
    S.timer = setTimeout(voiceSpeechPump, 0);
  }
  // all: every line goes. Else the line being said stops and the
  // conversation's queued lines go; a line beside it (one that was being
  // said too) waits for its turn again.
  function voiceSpeechStop(all) {
    var S = voiceSpeech, p = S.playing;
    voiceSpeakSeq++;
    S.playing = null;
    S.drained = [];
    voice.tts.cancel();
    var keep = [];
    S.q.forEach(function (x) { if (all || !x.bg) x.resolve(false); else keep.push(x); });
    if (p && p.bg && !all) keep.unshift(p); else if (p) p.resolve(false);
    S.q = keep;
    clearTimeout(S.timer);
    if (keep.length) S.timer = setTimeout(voiceSpeechPump, 0);
    voiceHushShow();
  }
  // Pure: is this queued line about `title`? Its own lines, and the
  // conversation's (not beside it) while voice is on that session.
  function voiceLineAbout(line, title, session) {
    return !!title && (line.title === title || (!line.bg && session === title));
  }
  // The lines about one session go — the one being said stops, the queued
  // ones are dropped; every other line keeps its place. True when a line
  // was being said (and was cut).
  function voiceSpeechStopFor(title) {
    var S = voiceSpeech, p = S.playing, cut = false;
    if (title && title === voice.session) voiceSpeakSeq++;   // a listen waiting on the queue is not this one's now
    var keep = [];
    S.q.forEach(function (x) { if (voiceLineAbout(x, title, voice.session)) x.resolve(false); else keep.push(x); });
    S.q = keep;
    if (p && voiceLineAbout(p, title, voice.session)) {
      cut = true;
      S.playing = null;
      voice.tts.cancel();
      p.resolve(false);
      clearTimeout(S.timer);
      S.timer = setTimeout(voiceSpeechPump, 0);
    }
    voiceHushShow();
    return cut;
  }
  // "Stop talking" in the strip while a line is being said — with voice
  // off too (Everything reads every session): then the strip shows for it.
  function voiceHushShow() {
    var on = !!voiceSpeech.playing;
    voiceWakeSpeaking(on);
    if (voiceHushBtn.classList.contains("hidden") === !on) return;
    voiceHushBtn.classList.toggle("hidden", !on);
    voiceStrip.classList.toggle("hidden", voice.state === "off" && !voice.hearPending && !on);
    fitSoon();
  }
  // "Stop talking" (said — the router's kind hush, or the local table — or
  // the strip's button, or a tap on the strip while it talks): what is
  // being said stops and every queued line goes, nothing is said instead,
  // and a mic that is listening goes on listening. The ack waits no more.
  function voiceHush() {
    voiceSpeechStop(true);
    voiceAckStop();
    if (voice.state === "off") return;
    if (voice.state === "speaking") { voiceIdle("Quiet."); return; }
    // Listening: its caption comes back (the hint, the dialog's question);
    // else not the line just cut.
    var was = voiceIsListening() ? String(voiceCaptionEl.textContent || "") : voice.state === "idle" ? voiceTalkHint() : "";
    voiceCaption("Quiet.");
    setTimeout(function () {
      if (voice.state !== "off" && voiceCaptionEl.textContent === "Quiet.") voiceCaption(was);
    }, VOICE_QUIET_CAPTION_MS);
  }
  // Barge-in: the mic (or a hold) while a line is being said. The speaker
  // is cut BEFORE the mic opens, the lines about that line's session (or
  // `title`'s: the ask sheet's) go, and the listen that follows opens
  // VOICE_BARGE_MUTE_MS later, so the phone does not hear its own voice's
  // tail. False when nothing was being said.
  function voiceBargeIn(title) {
    var p = voiceSpeech.playing;
    if (!p) return false;
    voiceSpeechStopFor(title || p.title || voice.session);
    if (voiceSpeech.playing === p) {   // a line about something else: cut too, not said again
      voiceSpeech.playing = null;
      voice.tts.cancel();
      p.resolve(false);
    }
    voiceListen_.muteUntil = Date.now() + VOICE_BARGE_MUTE_MS;
    voiceHushShow();
    return true;
  }
  // fn once nothing is queued or being said (now, if so).
  function voiceSpeechDrain(fn) {
    if (!voiceSpeech.playing && !voiceSpeech.q.length) { fn(); return; }
    voiceSpeech.drained.push(fn);
  }
  function voiceSpeechDrained() {
    var fns = voiceSpeech.drained;
    voiceSpeech.drained = [];
    fns.forEach(function (fn) { fn(); });
  }
  // A listen that follows a read-out (a reply's question, a dialog, the
  // retry, hands-free): once the queue has drained — a line still to come
  // would otherwise wait for this listen to end — and only if nothing took
  // over meanwhile.
  function voiceListenAfter(mode, windowMs) {
    var seq = voiceSpeakSeq;
    voiceSpeechDrain(function () {
      if (seq !== voiceSpeakSeq || (voice.state !== "speaking" && voice.state !== "idle")) return;
      voiceListen(mode, windowMs);
    });
  }

  // --- listening ----------------------------------------------------------------
  // held: a hold-to-talk press is what is listening (letting go stops);
  // overAfter/overBy: push to talk ended it, listening stops once the words
  // are in; routing/routeQ/gen: a phrase is with /api/voice/route, the
  // phrases heard meanwhile wait their turn, and an answer for an older
  // listen is dropped.
  var voiceListen_ = { netErrs: 0, mode: "", until: 0, restarts: 0, timer: null, reprompted: false,
                       heard: false, startedAt: 0, rapid: 0, stopAfter: false, held: false,
                       overAfter: false, overBy: 0, routing: false, routeQ: [], draining: false, gen: 0,
                       muteUntil: 0 };
  voice.listen = voiceListen_;   // tests age the window with it
  function voiceListen(mode, windowMs, held) {
    var L = voiceListen_;
    L.mode = mode;
    L.until = Date.now() + windowMs;   // to the first word; every word moves it VOICE_DICT_IDLE_MS out
    L.restarts = 0;
    L.rapid = 0;
    L.netErrs = 0;
    L.heard = false;
    L.stopAfter = false;
    L.held = !!held;
    L.overAfter = false;
    L.routing = false;
    L.routeQ = [];
    L.draining = false;
    L.gen++;
    clearTimeout(L.timer);
    voiceSpeechStop(false);   // the line being said stops; the queue holds while the mic listens
    voiceSetState(mode === "dialog" ? "dialog" : mode === "answer" ? "awaiting" : "listening",
                  mode === "dialog" && voice.dialog ? (voice.dialog.question || "") : voiceDictHint());
    voiceError("");
    if (!voice.stt.available()) {
      voiceError("No speech recognition in this browser.");
      if (IOS) voiceShowSafariBanner();
      voiceIdle();
      return;
    }
    if (ANDROID && navigator.vibrate) { voice.buzzAt = Date.now(); try { navigator.vibrate(30); } catch (e) {} }
    // A barge-in cut the speaker a moment ago: the mic opens once its
    // tail has died away (a hold let go meanwhile has ended this listen).
    var mute = L.muteUntil - Date.now(), gen = L.gen;
    L.muteUntil = 0;
    if (mute > 0) {
      setTimeout(function () { if (voiceListen_.gen === gen && voiceIsListening() && !voice.stt.active()) voiceRecStart(); }, mute);
    } else {
      voiceRecStart();
    }
    // A recognizer that never ends is still bound by the window — but a clip
    // on its way to the server is not expired: its words are coming. The
    // window moves out with every word, so the clock re-arms until it is due.
    L.timer = setTimeout(function expire() {
      if (voiceListen_.mode !== mode || !voiceIsListening()) return;
      var left = voiceListen_.until - Date.now();
      if (left > 0) { voiceListen_.timer = setTimeout(expire, left + 500); return; }
      if ((voice.stt.pending && voice.stt.pending()) || voiceListen_.routing) { voiceListen_.timer = setTimeout(expire, 500); return; }
      voiceListenExpired();
    }, windowMs + 500);
  }
  function voiceRecStart() {
    var mode = voiceListen_.mode;
    voiceListen_.startedAt = Date.now();
    voice.stt.start({
      until: voiceListen_.until,
      // A streamed phrase arrives whole while it may be a command (the box
      // is empty: the router decides) or a dialog's answer; else its words
      // go into the box as they settle.
      whole: function () { return mode === "dialog" || (!voice.box && !composeEl.value.trim()); },
      // The session is asking: an answer at the end of the words acts, so
      // the stream holds back what may start one. Not into the ask sheet's
      // field: there every word is the answer, sent on "submit".
      answers: function () { return !voice.box && !!voiceAsks(voice.session); },
      onInterim: function (t, tail) { if (voiceIsListening()) { voiceHeardSome(); voiceCaption(t || voiceDictHint(), t ? tail : ""); } },
      onStatus: function (t) { if (voiceIsListening()) voiceCaption(mode === "dialog" ? t : t + " · " + voiceDictHint()); },
      onLevel: function (v) { voiceLevel(voiceIsListening() ? v : null); },
      onSpeech: function () { if (voiceIsListening()) voiceSpeechTick(); },
      onFinal: function (t) { if (voiceIsListening()) { voiceHeardSome(); voiceHeard(t, mode); } },
      onError: function (code, detail) {
        if (!voiceIsListening()) return;
        if (code === "not-allowed" || code === "service-not-allowed") {
          voiceError("Microphone permission denied — allow the mic for this site.");
          voiceListen_.until = 0;
          voiceIdle();
        } else if (code === "audio-capture") {
          voiceError("No microphone found.");
          voiceListen_.until = 0;
          voiceIdle();
        } else if (code === "unavailable" || code === "start-failed") {
          voiceError("Speech recognition did not start (" + code + ").");
          voiceListen_.until = 0;
          voiceIdle();
        } else if (code === "network") {
          // A Chromium without Google's speech key (Arc, Brave, Vivaldi,
          // Opera…) fails EVERY start this way and hears nothing: the first
          // is retried like a blip (onEnd restarts), the second in a window
          // stops the loop and says why, instead of eight silent restarts.
          if (++voiceListen_.netErrs >= 2) {
            voiceError("Speech recognition failed (network): this browser cannot reach its speech service. Use Chrome, Edge or Safari, or turn on server recognition (Settings → Voice).");
            voiceListen_.until = 0;
            voiceIdle();
          }
        } else if (code === "server" || code === "nothing-recorded") {
          // The server adapter: the clip went nowhere and its words are
          // gone — say why (a 409 is the hint to turn recognition on) and
          // wait for the next tap.
          voiceError(detail || "Couldn't transcribe.");
          voiceListen_.until = 0;
          voiceIdle();
        }
        // no-speech / aborted / a first network: onEnd follows and the window decides.
      },
      onEnd: function () {
        if (!voiceIsListening()) return;
        if (voice.stt.active()) return;   // already up again (voiceListenContinue)
        var L = voiceListen_;
        if (L.stopAfter) { voiceListenStop(); return; }
        if (L.overAfter) { voiceListenFlushSoon(); return; }
        // Phones end the recognizer after every phrase: start it again for
        // as long as the window is open. One that dies within a second of
        // starting, VOICE_RAPID_RESTART_MAX times running, is broken, not
        // quiet — say so instead of looping.
        L.rapid = Date.now() - L.startedAt < 1000 ? L.rapid + 1 : 0;
        if (L.rapid >= VOICE_RAPID_RESTART_MAX) {
          voiceError("Speech recognition keeps stopping — try again, or another browser.");
          L.until = 0;
          voiceIdle();
          return;
        }
        if (Date.now() < L.until) {
          L.restarts++;
          setTimeout(function () {
            if (voiceIsListening() && voiceListen_.mode === mode) voiceRecStart();
          }, 250);
        } else {
          voiceListenExpired();
        }
      },
    });
  }
  // A word (interim or final) moves the window out: dictation ends only on
  // "over", "submit", the mic, Cancel, or VOICE_DICT_IDLE_MS of silence.
  function voiceHeardSome() {
    var L = voiceListen_;
    L.heard = true;
    L.until = Math.max(L.until, Date.now() + VOICE_DICT_IDLE_MS);
  }
  // After a phrase: keep listening. An adapter that ended with the phrase (a
  // clip, iOS) is started again; Chrome's continuous session is still up.
  function voiceListenContinue() {
    if (!voiceIsListening() || voiceListen_.draining) return;
    if (voiceListen_.stopAfter) { voiceListenStop(); return; }
    if (voiceListen_.overAfter) { voiceListenFlushSoon(); return; }
    if (!voice.stt.active()) voiceRecStart();
  }
  function voiceListenExpired() {
    var mode = voiceListen_.mode;
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    // Words but no "over" and then silence: kept in the box, never sent.
    if (mode !== "dialog" && vbox().value.trim()) { voiceListenStop(); return; }
    // The ask sheet read its question and nothing came back: once more.
    if (mode === "prompt" && voice.box && !voiceListen_.reprompted && askVoiceLine()) {
      voiceListen_.reprompted = true;
      speakLine("Still there? " + askVoiceLine()).then(function (ok) {
        if (ok && voice.box) voiceListenAfter("prompt", VOICE_ANSWER_MS);
      });
      return;
    }
    if (mode === "answer" && !voiceListen_.reprompted && voice.lastSay.text) {
      voiceListen_.reprompted = true;
      speakLine("Still there? " + voice.lastSay.text).then(function (ok) {
        if (ok) voiceListenAfter("answer", VOICE_ANSWER_MS);
      });
      return;
    }
    if (mode === "dialog") { voiceDialogRetry(); return; }
    voiceIdle();
  }
  // One phrase. Radio rules: a trailing "over" stops listening with the
  // words in the box, a trailing "submit" sends the box, a trailing
  // "disregard" empties it and listens on, anything else is appended and
  // listening goes on. A command ("switch to…", "new session…", "voice
  // off"…) is one only while the box is empty — the server's router says
  // which (voiceRoute), the local table when it cannot; mid-message those
  // are words. A dialog answer ("yes", "two") needs no "over"; an "over" or
  // a "submit" is stripped when said.
  function voiceHeard(text, mode) {
    var L = voiceListen_;
    if (L.routing) { L.routeQ.push([text, mode]); return; }   // after the phrase being routed
    // "Stop talking", in any mode and into any box: the voice stops, the
    // mic listens on — never typed, never a dialog's "no".
    if (voiceIsHush(voiceDictParse(text).text || text)) { voiceHush(); voiceListenContinue(); return; }
    // A stop phrase at the end, in any mode (before a dialog's "no" can
    // read "stop" as a refusal): the words before it kept, listening over.
    var stop = voiceTail(text, false);
    if (stop) { voiceStopPhrase(stop.text, mode); return; }
    // A select question the ask sheet shows: its words, its buttons.
    if (mode === "dialog" && askSelVoiceOn()) { askSelVoice(text); return; }
    var p = voiceDictParse(text);
    if (mode === "dialog") {
      if (p.kind === "disregard" || !p.text) { voiceListenContinue(); return; }
      clearTimeout(L.timer);
      voice.stt.stop();
      voiceDialogHeard(p.text);
      return;
    }
    if (p.kind === "disregard") {
      vbox().value = "";
      vboxSized();
      voice.dictated = false;
      voiceCaption("Disregarded — go on");
      voiceListenContinue();
      return;
    }
    // Into the ask sheet's field every phrase is the answer's words: no
    // router, no commands — "submit" sends it, "over" keeps it.
    if (voice.box) { voiceHeardWords(p); return; }
    if (p.text && !composeEl.value.trim()) {
      var gen = L.gen;
      L.routing = true;
      voiceRoute(p.text).then(function (res) {
        if (L.gen !== gen) return;      // a newer listen (or none): this answer is for nobody
        L.routing = false;
        if (!voiceIsListening()) { L.routeQ = []; return; }
        L.draining = true;            // the queued phrases first, then listen on / send
        if (voiceRouted(res, p)) { L.draining = false; L.routeQ = []; return; }   // a command: listening is over
        voiceRouteDrain();
      });
      return;
    }
    voiceHeardRest(p);
  }
  // A phrase that is not a command: a whole-phrase answer to ANOTHER
  // session ("session three yes") answers it, voice staying here; while
  // this session is asking, an answer at the end answers it (the words
  // before it join the box); else it is dictation. True when it acted.
  function voiceHeardRest(p) {
    if (p.text && voiceAnswerOther(p.text)) return true;
    var a = p.text && voiceAsks(voice.session) ? voiceTail(p.text, true) : null;
    if (a && a.kind === "answer") {
      if (a.text) voiceAppend(a.text);
      voiceStopForCommand();
      voiceAnswerLocal(voice.session, a);
      return true;
    }
    voiceHeardWords(p);
    return false;
  }
  function voiceAppend(text) {
    var box = vbox();
    var cur = box.value.trim();
    box.value = cur ? cur + " " + text : text;
    vboxSized();
    voice.dictated = true;
  }
  // A stop phrase: listening ends, the words said before it join the box
  // (radio words still count: "fix it over, that's all").
  function voiceStopPhrase(before, mode) {
    voiceStopForCommand();
    var p = voiceDictParse(before);
    if (mode !== "dialog" && p.kind !== "disregard" && p.text) voiceAppend(p.text);
    if (mode === "dialog") { voiceIdle(nameOf(voice.session) + " is waiting on you"); return; }
    voiceListenKept();
  }
  // The words of a phrase that is not a command: into the box, then
  // "submit" (send) or "over" (stop, keep).
  function voiceHeardWords(p) {
    if (p.text) voiceAppend(p.text);
    // "submit" on a select question's "Type something" / "Chat about this":
    // the sheet sends it (the option's digit first).
    if (p.kind === "submit" && voice.box === askTextEl && ask.sel.input && askIsSelect(ask.dialog)) {
      if (!askTextEl.value.trim()) { voiceCaption("Nothing to send yet"); voiceListenContinue(); return; }
      voice.box = null;
      voiceStopForCommand();
      ask.sel.voice = true;
      voiceSetState("sending", askTextEl.value.trim());
      askSelSendText();
      return;
    }
    if (p.kind === "submit") {
      if (vbox().value.trim()) { voiceSend(vbox().value); return; }
      voiceCaption("Nothing to send yet");
      voiceListenContinue();
      return;
    }
    if (p.kind === "over") {
      clearTimeout(voiceListen_.timer);
      voice.stt.stop();
      voiceListenKept();
      return;
    }
    voiceCaption(voiceDictHint());
    voiceListenContinue();
  }
  // The phrases heard while one was being routed, in order; listening goes
  // on (or push to talk sends) after the last.
  function voiceRouteDrain() {
    var L = voiceListen_;
    L.draining = true;
    while (L.routeQ.length && !L.routing && voiceIsListening()) {
      var x = L.routeQ.shift();
      voiceHeard(x[0], x[1]);
    }
    L.draining = false;
    if (!voiceIsListening()) { L.routeQ = []; return; }
    if (!L.routing) voiceListenContinue();
  }
  // "fix the bug over" → {kind: "over", text: "fix the bug"}; "… submit"
  // → {kind: "submit", text}; "… disregard" (or "disregard that") →
  // {kind: "disregard"}; else {kind: "", text}. Case and punctuation do not
  // matter ("Over." from iOS); only the LAST word counts — an "over" in the
  // middle of a sentence is a word.
  function voiceDictParse(text) {
    var t = String(text || "").trim();
    var m = /^(.*?)[\s,.!?]*\b(?:(over|submit)|(disregard)(?:\s+(?:that|it|all|everything))?)[.!?\s]*$/i.exec(t);
    if (!m) return { kind: "", text: t };
    return { kind: (m[2] || m[3]).toLowerCase(), text: m[1].trim() };
  }

  // --- control phrases: they act at the END of any phrase -------------------
  // A stop phrase ("stop recording", "that's all", "mic off"…) ends listening
  // wherever it is said, the words before it kept in the box. While the
  // session is asking (voiceAsks: a dialog, or a Say line with a question),
  // an answer ("yes", "no thanks", "always", "option two") at the end of a
  // phrase answers it. Both are matched on the last VOICE_TAIL_MAX words
  // ("yes and don't ask me again" is six), and a stream holds back up to
  // VOICE_HOLD_MAX words that may start one (voiceStreamSplit), so a
  // control phrase never lands in the box. A number answers only as
  // the whole phrase or with its marker ("option two"): "add two" is words.
  var VOICE_TAIL_MAX = 6;
  var VOICE_HOLD_MAX = 3;
  var VOICE_STOP = /^(?:stop (?:listening|recording|dictating)|(?:mic|microphone) off|that'?s all|that is all|(?:i'?m )?done talking)$/;
  var VOICE_ANS_YES = /^(?:yes|yeah|yep|yup|sure|ok|okay|allow|approve|go ahead|do it)(?: (?:it|that|please))?$/;
  var VOICE_ANS_NO = /^(?:no|nope|nah|deny|reject|don'?t|skip)(?: (?:it|that|thanks|thank you))?$/;
  var VOICE_ANS_NUM = /^(?:option|number|choice) (\S+)$|^(?:the )?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth)(?: one| option)?$/;
  // What the held-back words may be the start of (voiceMayStart).
  var VOICE_HOLD_STOP = ["stop listening", "stop recording", "stop dictating", "mic off", "microphone off",
                         "that's all", "thats all", "that is all", "done talking", "i'm done talking", "im done talking"];
  var VOICE_HOLD_ANSWER = ["yes always", "yes and always", "yeah always", "ok always", "always allow", "don't ask again",
                           "do not ask again", "never ask again", "no thanks", "no thank you", "go ahead", "do it",
                           "option", "number", "choice", "the first", "the second", "the third", "answer", "reply"];
  var VOICE_NUM_WORDS = { ten: 10, eleven: 11, twelve: 12 };
  // "two" / "2" / "second" → 2; 0 when it is not a number.
  function voiceNumber(word) {
    var w = String(word || "").toLowerCase();
    if (/^\d+$/.test(w)) return parseInt(w, 10);
    return VOICE_NUM_VALUE[w] || VOICE_NUM_WORDS[w] || 0;
  }
  // Could these (normalized) words be the start of a control phrase?
  function voiceMayStart(words, answers) {
    var t = voiceNorm(words);
    if (!t) return false;
    var lists = answers ? VOICE_HOLD_STOP.concat(VOICE_HOLD_ANSWER) : VOICE_HOLD_STOP;
    for (var i = 0; i < lists.length; i++) if ((lists[i] + " ").indexOf(t + " ") === 0) return true;
    return answers ? !!voiceAnswerWords(t, true) : false;
  }
  // An answer phrase (normalized, the whole of `t`) → {choice, number?}, or
  // null. "answer yes" / "reply no" are answers too. `whole`: t is the whole
  // phrase, so a bare number counts.
  function voiceAnswerWords(t, whole) {
    t = String(t || "").replace(/^(?:answer|reply|say|pick|choose|select) /, "").replace(/ please$/, "");
    if (VOICE_ALWAYS.test(t)) return { choice: "always" };
    if (VOICE_ANS_NO.test(t)) return { choice: "no" };
    if (VOICE_ANS_YES.test(t)) return { choice: "yes" };
    var m = VOICE_ANS_NUM.exec(t) || (whole ? /^(\S+)$/.exec(t) : null);
    var n = m ? voiceNumber(m[1] || m[2]) : 0;
    return n ? { choice: "number", number: n } : null;
  }
  // The control phrase a phrase ENDS with: {kind: "stop"} or (answers: the
  // session is asking) {kind: "answer", choice, number?}, plus `text` (the
  // words before it, as said) and `said` (the phrase itself, as said).
  // The longest match wins: "no thanks" over "thanks".
  function voiceTail(text, answers) {
    var words = String(text || "").trim().split(/\s+/).filter(Boolean);
    for (var k = Math.min(VOICE_TAIL_MAX, words.length); k >= 1; k--) {
      var t = voiceNorm(words.slice(words.length - k).join(" "));
      var hit = VOICE_STOP.test(t) ? { kind: "stop" } : null;
      if (!hit && answers) {
        hit = voiceAnswerWords(t, k === words.length);
        if (hit) hit.kind = "answer";
      }
      if (hit) {
        hit.text = words.slice(0, words.length - k).join(" ").replace(/[\s,;:.!?-]+$/, "");
        hit.said = words.slice(words.length - k).join(" ").replace(/[.!?]+$/, "");
        return hit;
      }
    }
    return null;
  }
  // A whole phrase that answers ANOTHER session: "session three yes",
  // "session 2, option one", "tell parser no", "answer the login fix
  // always" → {index | name, choice, number?, said}, or null. The name is
  // resolved by the caller (voiceMatchSession).
  function voiceTargetAnswer(text) {
    var t = voiceNorm(text);
    var m = /^session (?:number )?(\S+) (.+)$/.exec(t);
    if (m) {
      var n = voiceNumber(m[1]), a = n ? voiceAnswerWords(m[2], true) : null;
      if (a) { a.index = n; a.said = m[2]; return a; }
      return null;
    }
    m = /^(?:tell|answer|reply to) (.+)$/.exec(t);
    if (!m) return null;
    var words = m[1].split(" ");
    // The longest answer at the end, so "tell parser no thanks" is "no thanks".
    for (var k = Math.min(VOICE_TAIL_MAX, words.length - 1); k >= 1; k--) {
      var said = words.slice(words.length - k).join(" ");
      var hit = voiceAnswerWords(said, true);
      if (hit) {
        hit.name = words.slice(0, words.length - k).join(" ").replace(/ (?:to|with)$/, "");
        hit.said = said;
        return hit;
      }
    }
    return null;
  }

  // --- voice commands (matched on the WHOLE transcript, case- and
  // punctuation-insensitive; anything else is a prompt) -------------------
  function voiceNorm(text) {
    return String(text || "").toLowerCase().replace(/[’`]/g, "'")
      .replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
  }
  // By number and by step before by name: "go to session two" is row two.
  // focus / open / show / select <name> switch only on a session whose
  // name starts so (voiceRouted) — "open the readme" stays a prompt.
  // hush ("stop talking") is not "stop": it cuts the voice, the mic goes on.
  var VOICE_CMD = [
    { kind: "hush",     re: /^(?:stop (?:talking|speaking|reading|saying that)|be quiet|quiet|hush|shut up|(?:that'?s |that is )?enough|enough (?:talking|of that))$/ },
    { kind: "cancel",   re: /^(?:cancel|never ?mind|scratch that|forget it)$/ },
    { kind: "switch_index", re: /^(?:(?:switch|go|change|jump|move) (?:over )?to |focus (?:on )?|open |show |select )?session (?:number )?(\S+)$/ },
    { kind: "switch_rel", re: /^(?:(?:switch|go|change|jump|move) (?:over )?to |focus (?:on )?|open |show |select )?(?:the )?(next|previous|prev) (?:session|one)$/ },
    { kind: "switch",   re: /^(?:switch|go|change|jump) (?:over )?to (.+)$/ },
    { kind: "focus",    re: /^(?:focus(?: on)?|open|show|select) (.+)$/ },
    { kind: "question", re: /^(?:what(?: is|'s| was) (?:it|that) asking(?: me)?(?: for)?|what does it (?:want|need)(?: from me)?|what(?: is|'s| was) the question)$/ },
    { kind: "needs",    re: /^(?:what|who) needs (?:me|attention|my attention)$/ },
    { kind: "status",   re: /^(?:what'?s it doing|what is it doing|status|what'?s happening|what'?s going on)$/ },
    { kind: "condense", re: /^(?:summari[sz]e(?: that| it)?|what did it say|summary)$/ },
    { kind: "repeat",   re: /^(?:say that again|repeat(?: that)?)$/ },
    { kind: "off",      re: /^(?:voice off|turn voice off|turn off voice)$/ },
    { kind: "stop",     re: /^(?:stop listening|stop)$/ },
  ];
  function voiceCommand(text) {
    var n = voiceNorm(text);
    if (/^(?:ok|okay) stop$/.test(n)) return { kind: "hush", arg: "" };   // before the "okay" goes: not "stop"
    var t = n.replace(/^(?:ok|okay|hey|please) /, "").replace(/ please$/, "");
    for (var i = 0; i < VOICE_CMD.length; i++) {
      var m = VOICE_CMD[i].re.exec(t);
      if (m) return { kind: VOICE_CMD[i].kind, arg: (m[1] || "").trim() };
    }
    return null;
  }
  // "Stop talking" is the one command matched here before the router: it
  // must act in every mode (a dialog, the ask sheet's field) and never wait.
  function voiceIsHush(text) {
    var c = voiceCommand(text);
    return !!c && c.kind === "hush";
  }
  function voiceRunCommand(cmd) {
    switch (cmd.kind) {
      case "hush": voiceHush(); return;
      case "cancel": voiceIdle("Dropped"); return;
      case "switch":
      case "focus": voiceSwitch(cmd.arg); return;
      case "switch_index":
        if (voiceNumber(cmd.arg)) voiceSwitchIndex(voiceNumber(cmd.arg)); else voiceSwitch(cmd.arg);
        return;
      case "switch_rel": voiceSwitchRel(cmd.arg === "next" ? 1 : -1); return;
      case "question": voiceQuestion(voice.session); return;
      case "stop": voiceIdle(vbox().value.trim() ? VOICE_KEPT : undefined); return;
      case "needs": voiceNeedsMe(); return;
      case "status": voiceStatus(); return;
      case "condense": voiceCondense(); return;
      case "repeat":
        speakLine(voice.lastLine || "Nothing to repeat yet.").then(function (ok) { if (ok) voiceIdle(); });
        return;
      case "off":
        if (voice.session) voiceSetFlag(voice.session, false);
        voiceOff();
        return;
      default: voiceIdle(); return;
    }
  }

  // --- the router: POST /api/voice/route --------------------------------------
  // The server says what a phrase said into an empty box is: a prompt (the
  // words stay for the current session), a switch, a new session it has
  // already created, a line to say, or one of the bare commands. Anything
  // else — a non-200, the network, VOICE_ROUTE_TIMEOUT_MS, an older server
  // that answers {} — is null, and the local VOICE_CMD table decides, so
  // the page works offline too. A kind this page does not know (a newer
  // server) is a prompt: the words stay words. `session`: the session the
  // phrase is about (an answer by row number asks again with its title).
  var VOICE_ROUTE_KINDS = ["prompt", "switch", "new", "ambiguous", "no-session", "new-failed", "list",
                           "needs", "status", "cancel", "stop", "off", "repeat", "condense",
                           "switch_index", "switch_rel", "answer", "question", "hush"];
  function voiceRoute(text, session) {
    var aborter = window.AbortController ? new AbortController() : null;
    var timer = null;
    var late = new Promise(function (resolve) {
      timer = setTimeout(function () {
        if (aborter) { try { aborter.abort(); } catch (e) {} }
        voice.lastRoute = "timed out — the local table";
        resolve(null);
      }, VOICE_ROUTE_TIMEOUT_MS);
    });
    var asked = fetch("/api/voice/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, session: session || voice.session || current }),
      signal: aborter ? aborter.signal : undefined,
    }).then(function (r) {
      if (!r.ok) { voice.lastRoute = "HTTP " + r.status + " — the local table"; return null; }
      return r.json().catch(function () { return null; }).then(function (j) {
        if (!j || typeof j !== "object" || !j.kind) {
          voice.lastRoute = "no answer — the local table";
          return null;
        }
        if (VOICE_ROUTE_KINDS.indexOf(j.kind) < 0) {
          voice.lastRoute = String(j.kind) + " (unknown here: a prompt)";
          return { kind: "prompt" };
        }
        voice.lastRoute = String(j.kind) + (j.kind === "switch_index" || (j.kind === "answer" && j.index) ? " " + j.index
                        : j.kind === "switch_rel" ? " " + (Number(j.delta) > 0 ? "+" : "") + j.delta
                        : j.kind === "answer" ? " " + (j.choice || "") + (j.dialog_id ? " (dialog)" : j.no_dialog ? " (no dialog)" : "") : "");
        return j;
      });
    }).catch(function () {
      if (!voice.lastRoute || voice.lastRoute.indexOf("timed out") < 0) voice.lastRoute = "unreachable — the local table";
      return null;
    });
    return Promise.race([asked, late]).then(function (j) { clearTimeout(timer); return j; });
  }
  // The router's answer for phrase p (null: ask the local table). True when
  // the phrase was a command — listening is over, whatever it does next
  // (a switch may still be fetching the list); false when it was words.
  function voiceRouted(res, p) {
    if (!res) {
      var cmd = voiceCommand(p.text);
      if (cmd && cmd.kind === "hush") { voiceHush(); return false; }   // the mic goes on, nothing typed
      // "open the readme" is a prompt: open / show / select / focus switch
      // only to a session named so.
      if (cmd && cmd.kind === "focus" && !voiceStrictMatch(cmd.arg)) cmd = null;
      if (!cmd) return voiceHeardRest(p);   // an answer, or the words
      voiceStopForCommand();
      voiceRunCommand(cmd);
      return true;
    }
    var say = String(res.say || "").trim();
    switch (res.kind) {
      case "prompt": return voiceHeardRest(p);
      case "hush": voiceHush(); return false;   // the mic goes on, nothing typed or said
      case "switch":
      case "new":
        voiceStopForCommand();
        voiceRouteSelect(String(res.title || ""), res.kind === "new").then(function (title) {
          if (!title) { speakLine(say || "Couldn't find that session.").then(function (ok) { if (ok) voiceIdle(); }); return; }
          var inst = instByTitle(title) || {};
          speakLine(say || "Now on " + (res.name || nameOf(title)) + ".").then(function (ok) {
            if (!ok) return;
            if (inst.activity === "clarify") { voice.dialogKey = ""; voiceDialog(); } else voiceIdle();
          });
        });
        return true;
      case "switch_index":
        voiceStopForCommand();
        voiceSwitchIndex(Number(res.index) || 0);
        return true;
      case "switch_rel":
        voiceStopForCommand();
        voiceSwitchRel(Number(res.delta) || 1);
        return true;
      case "answer":
        voiceStopForCommand();
        voiceRoutedAnswer(res, p, false);
        return true;
      case "question":
        voiceStopForCommand();
        if (!say) { voiceQuestion(String(res.title || "") || voice.session); return true; }
        voiceSayThenIdle(voiceNameFirst(String(res.title || "") || voice.session, say));
        return true;
      case "needs":
      case "status":
        voiceStopForCommand();
        if (!say) { if (res.kind === "needs") voiceNeedsMe(); else voiceStatus(); return true; }
        speakLine(say).then(function (ok) { if (ok) voiceIdle(); });
        return true;
      case "ambiguous":
      case "no-session":
      case "new-failed":
      case "list":
        if (!say) return voiceRouted(null, p);   // nothing to say: the local table
        voiceStopForCommand();
        speakLine(say).then(function (ok) { if (ok) voiceIdle(); });
        return true;
      default:   // cancel, stop, off, repeat, condense
        voiceStopForCommand();
        voiceRunCommand({ kind: res.kind, arg: "" });
        return true;
    }
  }
  // The router's answer to a question or a dialog: {choice, number?,
  // index?, title?, dialog_id?, keys?, no_dialog?, say?}. With dialog_id +
  // keys it is POSTed as is (only with Approve dialogs by voice on —
  // "always" is the router's call); with only a row number (index) the
  // page names that row's session and asks again; no_dialog is said; and
  // an answer to a question is a prompt to that session. `again`: this is
  // the second ask, never a third.
  function voiceRoutedAnswer(res, p, again) {
    var say = String(res.say || "").trim();
    var title = String(res.title || "");
    if (!title && res.index) {
      title = voiceRowTitle(Number(res.index));
      if (!title) { voiceSayThenIdle("There is no session " + res.index + "."); return; }
      if (!again && !res.dialog_id && !res.no_dialog) {
        voiceSetState("sending", p.text);
        voiceRoute(p.text, title).then(function (r2) {
          if (voice.state !== "sending") return;
          if (r2 && r2.kind === "answer") { r2.title = r2.title || title; voiceRoutedAnswer(r2, p, true); return; }
          var ta = voiceTargetAnswer(p.text);
          voiceAnswerLocal(title, ta || { choice: String(res.choice || ""), said: voiceAnswerText(p.text) });
        });
        return;
      }
    }
    title = title || voice.session;
    if (res.no_dialog) { voiceSayThenIdle(say ? voiceNameFirst(title, say) : nameOf(title) + " isn't asking anything."); return; }
    if (res.dialog_id && res.keys && res.keys.length) {
      if (!voice.cfg.loaded || !voice.cfg.approve_dialogs) { voiceSayThenIdle("Answer it on the screen."); return; }
      voiceAnswerPost(title, String(res.dialog_id), res.keys.map(String),
                      res.text ? String(res.text) : res.choice === "number" ? String(res.number || res.keys[0]) : String(res.choice || "that"),
                      say, res.text);
      return;
    }
    voiceAnswerSend(title, voiceAnswerText(p.text));
  }
  // What `title` is waiting on: "dialog" (a permission prompt / clarify),
  // "question" (its last Say line asked one), or "" — what makes "yes" an
  // answer rather than a word. A clarify reading right after a voice
  // answer is the row lagging, not a new dialog.
  function voiceAsks(title) {
    var inst = instByTitle(title);
    if (!inst) return "";
    var mine = title === voice.session;
    if (mine && voice.dialog) return "dialog";
    if (inst.activity === "clarify" && !(mine && Date.now() - (voice.answeredAt || 0) < VOICE_ANSWERED_GRACE_MS)) return "dialog";
    if (inst.asking && inst.asking.kind === "dialog") return "dialog";
    if (mine && voice.asked) return "question";
    if (inst.asking && inst.asking.kind === "question") return "question";
    if (inst.activity === "idle" && /\?["')\]\s]*$/.test(String(inst.last_say || ""))) return "question";
    return "";
  }
  // A whole phrase answering ANOTHER session ("session three yes", "tell
  // parser no"): answered there, voice stays here. False when the phrase is
  // not one, or names this session (then it is this one's answer).
  function voiceAnswerOther(text) {
    var ta = voiceTargetAnswer(text);
    if (!ta) return false;
    var title = ta.index ? voiceRowTitle(ta.index) : "";
    if (ta.name) { var m = voiceMatchSession(ta.name); title = m.hit ? m.hit.title : ""; }
    if (ta.index && !title) { voiceStopForCommand(); voiceSayThenIdle("There is no session " + ta.index + "."); return true; }
    if (!title || title === voice.session) return false;
    voiceStopForCommand();
    voiceAnswerLocal(title, ta);
    return true;
  }
  // "session two yes" / "tell parser no thanks" → "yes" / "no thanks": what
  // the session is told; any other phrase is told as said.
  function voiceAnswerText(text) {
    var ta = voiceTargetAnswer(text);
    return ta ? ta.said : String(text || "").trim();
  }
  // An answer the page decides ({choice, said}: the router was not asked,
  // or could not say): a question's answer is a prompt; a dialog's is the
  // option voiceParseAnswer picks from GET /dialog — "always" only when
  // said, a number never on a permission prompt, as when it is read out.
  function voiceAnswerLocal(title, a) {
    var asks = voiceAsks(title), name = nameOf(title);
    if (asks === "question") { voiceAnswerSend(title, a.said); return; }
    voiceSetState("sending", a.said);
    fetch("/api/instances/" + encodeURIComponent(title) + "/dialog?quiet=1")
      .then(function (r) { return r.status === 204 ? null : r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        if (voice.state !== "sending") return;
        if (!d || !d.parsed || !(d.options || []).length) {
          voiceSayThenIdle(asks ? name + " is waiting on you: open the screen." : name + " isn't asking anything.");
          return;
        }
        if (!voice.cfg.loaded || !voice.cfg.approve_dialogs) { voiceSayThenIdle("Answer it on the screen."); return; }
        var opt = voiceParseAnswer(a.said, d.options);
        if (!opt) { voiceSayThenIdle("I didn't catch that: " + voiceDialogChoices(d).replace(/^.*\bSay /, "say ")); return; }
        if (title === voice.session) { voice.dialog = d; voice.dialogKey = String(d.id); }
        voiceAnswerPost(title, String(d.id), askOptKeys(opt), voiceKindWord(opt), "");
      });
  }
  // A question's answer goes as a prompt: to this session with the words
  // already in the box before it (one reply), to another session without
  // touching the box.
  function voiceAnswerSend(title, words) {
    voice.dictated = true;
    if (title === voice.session) voiceSend(voiceJoin(composeEl.value, words));
    else voiceSend(words, title);
  }
  // "Answered yes." from the router, said with the session's name first.
  function voiceNameFirst(title, say) {
    var name = nameOf(title);
    return voiceNorm(say).indexOf(voiceNorm(name)) === 0 ? say : name + ": " + say;
  }
  function voiceSayThenIdle(line) {
    speakLine(line).then(function (ok) { if (ok) voiceIdle(vbox().value.trim() ? VOICE_KEPT : undefined); });
  }
  // A command ends the listen: the recognizer, its clock, the window.
  function voiceStopForCommand() {
    clearTimeout(voiceListen_.timer);
    voiceListen_.until = 0;
    voice.stt.stop();
  }
  // Select the session the router named — voiceOnSelect moves voice along.
  // A session it has just created may not be in the list yet: fetch the list
  // once, and if it still is not there, claimPending selects it the tick it
  // shows up. Resolves the title ("" when there is no such session).
  function voiceRouteSelect(title, fresh) {
    if (!title) return Promise.resolve("");
    if (instByTitle(title)) { select(title); return Promise.resolve(title); }
    return poll().then(function () {
      if (instByTitle(title)) { select(title); return title; }
      if (!fresh) return "";
      pendingNew = title;
      pendingNewUntil = Date.now() + PENDING_NEW_MS;
      return title;
    });
  }

  // The Send button while voice is on: the draft goes through POST /send so
  // the ack + working + reply loop runs (an empty box stays a bare Enter
  // down the PTY, as before).
  function voiceTakesSend() {
    if (voice.state === "off" || !composeEl.value.trim()) return false;
    voice.box = null;   // the compose box's Send
    if (voice.state === "sending") return true;   // one tap, one prompt
    voiceSend(composeEl.value);
    return true;
  }

  // --- send + the "what it is doing" ack --------------------------------------
  // `to`: another session's question answered by voice ("tell parser use
  // the old schema"…): sent there, the box and voice stay here, and its
  // reply is that session's — no ack, no "working" here.
  function voiceSend(text, to) {
    text = String(text || "").trim();
    if (!text) { voiceIdle(); return; }
    var here = voice.session || current;
    var title = to || here;
    var other = title !== here;
    var name = nameOf(title);
    // The Say line is asked for per prompt: a dictated one with Spoken
    // replies on — a typed prompt costs no Say tokens — or any with
    // Everything.
    var dictated = !!voice.dictated;
    var say = voice.prefs.replies === "all" || (voice.prefs.replies === "mine" && dictated);
    var box = vbox();            // the ask sheet's field, or the compose box
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    voiceSetState("sending", text);
    voiceError("");
    // The baseline: what last_reply said BEFORE this prompt, so the ack poll
    // can tell the new turn's first words from the old turn's last ones.
    voiceLastReply(title).then(function (base) {
      if (voice.state !== "sending") return;
      return fetch("/api/instances/" + encodeURIComponent(title) + "/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, dialog_safe: true, say: say }),
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (voice.state !== "sending") return;
          if (r.ok && other) {
            var to2 = j && j.queued ? "Queued for " + name + "." : "Sent to " + name + ".";
            speakLine(to2).then(function (ok) { if (ok) voiceIdle(vbox().value.trim() ? VOICE_KEPT : undefined); });
            return;
          }
          if (r.ok) {
            box.value = "";
            if (box === composeEl) autosizeCompose();
            if (box !== composeEl) { voice.box = null; askSentByVoice(title); }
            voice.dictated = false;
            voice.expectSay = say;     // its reply is read out only if it was asked for
            voice.lastPrompt = { dictated: dictated, say: say };
            voice.pendingSay = null;   // anything that arrived before this prompt is old news
            voice.asked = false;       // answered
            // A new turn: its reply is news even when it repeats the last
            // one word for word ("All tests pass." twice). The dedupe below
            // only has to stop idle + turn_ended speaking ONE reply twice.
            voice.lastSay = { text: "", ts: 0 };
            voiceSetState("working", "");
            // No baseline (the read failed): no ack poll, or the first poll
            // would read out the PREVIOUS reply's opening sentence.
            // Spoken replies off: the strip says it, the phone stays quiet.
            var talk = voice.prefs.replies !== "off";
            if (base !== null && talk) voiceAckStart(title, base);
            var sent = j && j.queued ? "Queued for " + name + " — it is on a prompt." : "Sent to " + name + ".";
            if (talk) speakLine(sent, true); else voiceCaption(sent);
            return;
          }
          var why = (j && j.error) || ("HTTP " + r.status);
          var line = "Couldn't send.";
          if (r.status === 409 && j && j.waking) line = name + " is waking up, try again in a moment.";
          else if (r.status === 409 && j && j.paused) line = name + " is paused. Resume it first.";
          else if (r.status === 409 && j && j.in_dialog) { line = name + " is showing a prompt now — answer that first."; askInDialog(title); }
          else if (r.status === 409) line = "Couldn't send: " + why;
          voiceError(why);
          speakLine(line).then(function (ok) { if (ok) voiceIdle("Edit it, then Send"); });
        });
      });
    }).catch(function (err) {
      if (voice.state !== "sending") return;
      voiceError(String((err && err.message) || err));
      speakLine("Couldn't send.").then(function (ok) { if (ok) voiceIdle("Edit it, then Send"); });
    });
  }
  function voiceLastReply(title) {
    return fetch("/api/instances/" + encodeURIComponent(title) + "/output?view=last_reply&max_chars=600")
      .then(function (r) { return r.ok ? r.json() : null; })
      // Codex has no reply view and falls back to the screen, which changes
      // constantly — not something to read a "first sentence" from.
      .then(function (j) { return j && !j.fallback ? String(j.text || "") : ""; })
      .catch(function () { return null; });
  }
  var voiceAck = { timer: null, title: "", base: "", t0: 0 };
  function voiceAckStart(title, base) {
    voiceAckStop();
    voiceAck.title = title;
    voiceAck.base = base || "";
    voiceAck.t0 = Date.now();
    voiceAck.timer = setInterval(voiceAckTick, VOICE_ACK_POLL_MS);
  }
  function voiceAckStop() {
    clearInterval(voiceAck.timer);
    voiceAck.timer = null;
  }
  function voiceAckTick() {
    if (voice.state !== "working" || document.hidden || Date.now() - voiceAck.t0 > VOICE_ACK_MAX_MS) {
      voiceAckStop();
      return;
    }
    var title = voiceAck.title;
    voiceLastReply(title).then(function (text) {
      if (voice.state !== "working" || voiceAck.title !== title || !voiceAck.timer) return;
      if (!text || text === voiceAck.base) return;
      voiceAckStop();
      var line = firstSentence(text);
      if (line) speakLine(line, true, title);
    });
  }

  // --- the reply: the session's Say: line ---------------------------------------
  function voiceFetchSay(title) {
    return fetch("/api/instances/" + encodeURIComponent(title) + "/voice")
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  // `ts` is the say's own stamp when known (GET /voice); events carry none
  // and compare by text alone, so turn_ended after idle never repeats it.
  function voiceSaidAlready(say, ts) {
    return say === voice.lastSay.text && (!ts || ts <= voice.lastSay.ts);
  }
  function voiceReply(data) {
    if (voice.state === "off") return;
    var say = String((data && data.say) || "").trim();
    if (say && voiceSaidAlready(say, Number(data.ts) || 0)) return;
    var busy = voice.state === "listening" || voice.state === "sending" ||
               voice.state === "awaiting" || voice.state === "dialog";
    if (busy) { voice.pendingSay = data; return; }
    voiceAckStop();
    var name = nameOf(voice.session);
    if (voice.expectSay === false) {
      // The prompt asked for no Say line (typed, or Spoken replies off): its
      // reply is not read out — the turn's end is just "Your turn". Noted,
      // so the poll does not bring it back.
      if (say) voice.lastSay = { text: say, ts: Number(data.at) || Number(data.ts) || Infinity };
      if (voice.state === "working") voiceIdle("Your turn");
      return;
    }
    if (!say) {
      // The turn ended with no Say: line (no hook, no reply text): one word
      // so the owner knows — only when the page was waiting on it.
      if (voice.state !== "working") return;
      speakLine(name + " finished.").then(function (ok) { if (ok) voiceAfterReply(false); });
      return;
    }
    // Server stamps only (frame ts, GET /voice ts); none known → the text
    // alone dedupes until the next send clears it.
    voice.lastSay = { text: say, ts: Number(data.at) || Number(data.ts) || Infinity };
    var q = !!(data && data.has_question);
    voice.asked = q;
    speakLine(say, false, voice.session).then(function (ok) { if (ok) voiceAfterReply(q); });
  }
  function voiceAfterReply(hasQuestion) {
    if (voice.state === "off") return;
    if (hasQuestion) {
      voiceListen_.reprompted = false;
      voiceListenAfter("answer", VOICE_ANSWER_MS);
    } else if (voice.prefs.handsFree) {
      voiceListenAfter("prompt", VOICE_HANDSFREE_MS);
    } else {
      voiceIdle();
    }
  }
  function voiceGotIdle(data, frameTs) {
    var title = voice.session;
    if (data && data.say) {
      voiceReply({ say: data.say, has_question: data.has_question, ts: 0, at: Number(frameTs) || 0 });
      return;
    }
    voiceFetchSay(title).then(function (j) {
      if (voice.session !== title) return;
      voiceReply(j ? { say: j.say, has_question: j.has_question, ts: j.ts, at: j.ts } : null);
    });
  }

  // --- the events socket: /api/events while voice is on ---------------------------
  // The socket is open while voice is on, and with Everything whatever voice is.
  function voiceEventsWanted() {
    return !document.hidden && (voice.state !== "off" || voice.prefs.replies === "all");
  }
  function voiceEventsOpen() {
    if (voice.events || !voiceEventsWanted()) return;
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + "/api/events" +
              (voice.eventsSeq ? "?since=" + voice.eventsSeq : "");
    var sock;
    try { sock = new WebSocket(url); } catch (e) { voiceEventsRetry(); return; }
    voice.events = sock;
    voice.eventsState = "connecting";
    var serverTime = 0, fresh = !voice.eventsSeq;
    sock.onopen = function () { voice.eventsState = "open"; voice.eventsBackoff = 1000; };
    sock.onmessage = function (ev) {
      var f;
      try { f = JSON.parse(ev.data); } catch (e) { return; }
      if (!f || typeof f !== "object") return;
      if (f.event === "hello") {
        serverTime = Number(f.server_time) || 0;
        if (serverTime) voice.serverSkew = Date.now() / 1000 - serverTime;   // a frame's ts on this phone's clock
        // A restarted server numbers frames from 1 again: with the old
        // ?since= it would skip every live frame until its counter caught
        // up (server.py documents this for the desktop client). Reconnect
        // without one.
        if (f.boot_id) {
          if (voice.bootId && voice.bootId !== f.boot_id && voice.eventsSeq) {
            voice.bootId = String(f.boot_id);
            voice.eventsSeq = 0;
            try { sock.close(); } catch (e) {}
            return;
          }
          voice.bootId = String(f.boot_id);
        }
        return;
      }
      if (f.seq) voice.eventsSeq = Math.max(voice.eventsSeq, Number(f.seq) || 0);
      // A first connect replays the ring buffer; the past is not news (a
      // reconnect with ?since= gets only what it missed, which is).
      if (fresh && serverTime && Number(f.ts) < serverTime - 1) return;
      voice.onEvent(f);
    };
    sock.onclose = function () {
      if (voice.events !== sock) return;
      voice.events = null;
      voice.eventsState = "closed";
      voiceEventsRetry();
    };
    sock.onerror = function () {};
  }
  // Exponential backoff, 1 s .. 30 s; the 4 s poll covers the gap.
  function voiceEventsRetry() {
    if (!voiceEventsWanted()) return;
    clearTimeout(voice.eventsTimer);
    var wait = voice.eventsBackoff;
    voice.eventsBackoff = Math.min(30000, wait * 2);
    voice.eventsTimer = setTimeout(voiceEventsOpen, wait);
  }
  function voiceEventsClose() {
    clearTimeout(voice.eventsTimer);
    var s = voice.events;
    voice.events = null;
    voice.eventsState = "off";
    if (s) { try { s.onclose = null; s.close(); } catch (e) {} }
  }
  voice.onEvent = function (f) {
    askOnEvent(f);   // every session's asks, whichever one voice is on
    voiceOnTurn(f);  // Everything: every session's finished turn
    if (voice.state === "off" || !f || f.session !== voice.session) return;
    var data = f.data || {};
    if (f.event === "session.activity_changed") {
      if (f.new === "idle") voiceGotIdle(data, f.ts);
      else if (f.new === "clarify") voiceDialog();
      else if (f.new === "working") voice.dialogKey = "";
    } else if (f.event === "session.turn_ended") {
      voiceGotIdle(data, f.ts);
    }
  };
  // Everything (prefs.replies "all"): the line a frame says should be read
  // aloud, pure — {title, say} or null. A finished turn (activity_changed
  // new="idle", turn_ended) with a Say line, from ANY session, once per
  // reply: idle and turn_ended carry the same one, so `heard` (title → the
  // say last read) stops the second; a new turn ("working") clears it.
  // Voice's own session while voice is on is voiceReply's (its question is
  // listened for), never this. voiceLineText names it unless the phone is
  // on it.
  function voiceTurnLine(f, mode, voiceOn, heard) {
    if (mode !== "all" || !f || typeof f !== "object") return null;
    var title = String(f.session || ""), d = f.data || {};
    var ended = f.event === "session.turn_ended" || (f.event === "session.activity_changed" && f.new === "idle");
    var say = String(d.say || "").trim();
    if (!title || !ended || !say || title === voiceOn || heard[title] === say) return null;
    return { title: title, say: say };
  }
  function voiceOnTurn(f) {
    if (!f) return;
    var t = String(f.session || "");
    if (f.event === "session.activity_changed" && f.new === "working" && t) delete voice.heard[t];
    var line = voiceTurnLine(f, voice.prefs.replies, voice.state !== "off" ? voice.session : "", voice.heard);
    if (!line) return;
    voice.heard[line.title] = line.say;
    var at = Number(f.ts) && typeof voice.serverSkew === "number" ? (Number(f.ts) + voice.serverSkew) * 1000 : 0;
    speakLine(line.say, true, line.title, at);
  }
  // Everything with voice off: a page speaks only after a tap — the first
  // one anywhere unlocks it.
  document.addEventListener("click", function () {
    if (!voice.primed && voice.prefs.replies === "all") voicePrime();
  }, true);
  // Everything with voice off: a typed prompt goes through POST /send too,
  // so it can ask for a Say line. A dialog on screen (409 in_dialog) or a
  // failed send types it into the terminal as before.
  function everythingTakesSend() {
    var text = composeEl.value.trim();
    if (voice.prefs.replies !== "all" || voice.state !== "off" || !current || !text) return false;
    var title = current, raw = composeEl.value;
    composeEl.value = "";
    autosizeCompose();
    fetch("/api/instances/" + encodeURIComponent(title) + "/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, dialog_safe: true, say: true }),
    }).then(function (r) {
      if (r.ok) { flashStatus("sent to " + nameOf(title)); return; }
      throw new Error("HTTP " + r.status);
    }).catch(function () {
      if (current !== title) { composeEl.value = raw; autosizeCompose(); return; }
      send(raw);
      setTimeout(function () { send("\r"); }, 150);
    });
    return true;
  }

  // The 4 s /api/instances poll: the fallback for a dead socket, and the
  // "Tap to resume voice" offer while voice is off.
  function voiceOnPoll() {
    if (voice.state === "off") return;
    var inst = instByTitle(voice.session);
    if (!inst) return;
    if (inst.activity !== "clarify") voice.dialogKey = "";
    if (voice.state !== "idle" && voice.state !== "working") return;
    if (inst.activity === "clarify") { voiceDialog(); return; }
    if (inst.activity === "idle" && inst.last_say && !voiceSaidAlready(String(inst.last_say), 0)) {
      var title = voice.session;
      voiceFetchSay(title).then(function (j) {
        if (voice.session !== title || !j) return;
        voiceReply({ say: j.say, has_question: j.has_question, ts: j.ts, at: j.ts });
      });
    }
  }

  // --- permission dialogs ---------------------------------------------------------
  var VOICE_ORD = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];
  var VOICE_ANSWERED_GRACE_MS = 8000;   // a stale clarify reading after a voice answer is not a new dialog
  var VOICE_YES = /\b(?:yes|yeah|yep|yup|allow|approve|okay|ok|go ahead|do it|sure)\b/;
  var VOICE_NO = /\b(?:no|nope|deny|reject|don'?t|cancel|skip|stop)\b/;
  // "always" must be SAID, and said as the whole answer: "always", "yes,
  // always", "always allow that", "yes and don't ask again". A word inside
  // something else never counts ("no, not always" is a no; "I always forget
  // which" is nothing), and an enthusiastic yes never widens to always.
  var VOICE_ALWAYS = /^(?:(?:yes|yeah|yep|yup|ok|okay) ?)?(?:and )?(?:always(?: allow(?: (?:this|it|that|all))?)?|don'?t ask(?: me)? again|do not ask(?: me)? again|never ask(?: me)? again)$/;
  var VOICE_NUM = /\b(one|first|1|two|second|2|three|third|3|four|fourth|4|five|fifth|5|six|sixth|6|seven|seventh|7|eight|eighth|8|nine|ninth|9)\b/;
  var VOICE_NUM_VALUE = { one: 1, first: 1, "1": 1, two: 2, second: 2, "2": 2, three: 3, third: 3, "3": 3,
    four: 4, fourth: 4, "4": 4, five: 5, fifth: 5, "5": 5, six: 6, sixth: 6, "6": 6,
    seven: 7, seventh: 7, "7": 7, eight: 8, eighth: 8, "8": 8, nine: 9, ninth: 9, "9": 9 };
  // The option an utterance picks, or null (→ "I didn't catch that").
  // Permission dialogs (any yes/no option) take WORDS only: the whole-answer
  // always forms first (they cannot contain a refusal), then any no — a no
  // anywhere is a no, whatever else was said — then a yes. Numbers never
  // pick a permission (recognisers turn "too"/"to" into "2", and "2" is the
  // standing-permission option of Claude's dialog). Dialogs made only of
  // `other` options take a number, matched to the option's own key.
  function voiceParseAnswer(text, options) {
    var t = voiceNorm(text);
    var opts = options || [];
    function byKind(k) { for (var i = 0; i < opts.length; i++) if (opts[i].kind === k) return opts[i]; return null; }
    var permission = opts.some(function (o) { return o.kind === "yes" || o.kind === "no" || o.kind === "always"; });
    if (permission) {
      if (VOICE_ALWAYS.test(t)) return byKind("always") || byKind("yes");
      if (VOICE_NO.test(t)) return byKind("no");
      if (VOICE_YES.test(t)) return byKind("yes");
      return null;
    }
    var n = VOICE_NUM.exec(t);
    if (n) {
      var key = String(VOICE_NUM_VALUE[n[1]]);
      for (var i = 0; i < opts.length; i++) if (String(opts[i].key) === key) return opts[i];
    }
    return null;
  }
  function voiceDialogChoices(d) {
    var opts = d.options || [];
    var kinds = {};
    opts.forEach(function (o) { kinds[o.kind] = true; });
    if (kinds.yes || kinds.no) return kinds.always ? "Say yes, no, or always." : "Say yes or no.";
    var n = Math.min(opts.length, 9);
    var names = [], nums = [];
    for (var i = 0; i < n; i++) { names.push(VOICE_ORD[i] + ": " + stripMarkdown(opts[i].label || "")); nums.push(VOICE_ORD[i]); }
    var list = nums.length === 1 ? nums[0] : nums.length === 2 ? nums[0] + " or " + nums[1]
             : nums.slice(0, -1).join(", ") + ", or " + nums[nums.length - 1];
    return names.join(". ") + ". Say " + list + ".";
  }
  function voiceDialogSpeech(d) {
    var parts = [stripMarkdown(d.question || "a permission prompt")];
    if (d.command) parts.push("It wants to run: " + String(d.command).slice(0, 200) + ".");
    parts.push(voiceDialogChoices(d));
    if (d.kind === "select" && d.multi && !askSelReview(d)) parts.push("Pick any, then say next.");
    return parts.join(" ");
  }
  function voiceDialog() {
    var title = voice.session;
    if (!title || voice.state === "off" || voice.dialogBusy) return;
    voice.dialogBusy = true;
    fetch("/api/instances/" + encodeURIComponent(title) + "/dialog?quiet=1")
      .then(function (r) { return r.status === 204 ? null : r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        voice.dialogBusy = false;
        if (voice.session !== title || voice.state === "off") return;
        if (voiceIsListening() || voice.state === "sending") return;
        // A question in prose (the newer server answers it here too): its
        // Say line (or the ask sheet) asks it — "open the screen" would be wrong.
        if (d && d.kind === "question") return;
        var key = d && d.parsed && (d.options || []).length ? String(d.id) : "none";
        // The row reads clarify for a poll or two after a voice answer; the
        // dialog is gone (204) and "waiting on you" would be wrong.
        if (key === "none" && Date.now() - (voice.answeredAt || 0) < VOICE_ANSWERED_GRACE_MS) return;
        if (key === voice.dialogKey) return;
        voice.dialogKey = key;
        // The sheet moved to it under a hand a moment ago: it is on the
        // screen, not read (askHandsOn).
        if (ask.open && !askMayNarrate()) return;
        voiceAckStop();
        var name = nameOf(title);
        if (key === "none") {
          voice.dialog = null;
          speakLine(name + " is waiting on you: open the screen.").then(function (ok) {
            if (ok) voiceIdle(name + " is waiting on you");
          });
          return;
        }
        voice.dialog = d;
        voice.dialogTries = 0;
        voiceReadDialog(d);
      });
  }
  function voiceReadDialog(d) {
    if (askIsSelect(d)) askShowSelect(voice.session, d);   // its steps live in the sheet
    voiceSetState("dialog", d.question || "");
    speakLine(voiceDialogSpeech(d), false, voice.session).then(function (ok) {
      if (!ok || voice.state === "off") return;
      // Off, or not yet known (GET /api/voice still in flight): read it,
      // never answer it — this is the one safety setting the page enforces.
      if (!voice.cfg.loaded || !voice.cfg.approve_dialogs) {
        speakLine("Answer it on the screen.").then(function (ok2) {
          if (ok2) voiceIdle(nameOf(voice.session) + " is waiting on you");
        });
        return;
      }
      voiceListenAfter("dialog", VOICE_DIALOG_MS);
    });
  }
  function voiceDialogRetry() {
    var d = voice.dialog;
    if (!d || voice.dialogTries >= 2) {
      voiceIdle(nameOf(voice.session) + " is waiting on you");
      return;
    }
    voice.dialogTries++;
    var choices = voiceDialogChoices(d);
    var line = /^Say yes/.test(choices) ? "I didn't catch that: say yes, no, or always."
                                        : "I didn't catch that: " + choices.replace(/^.*\bSay /, "say ");
    speakLine(line).then(function (ok) { if (ok) voiceListenAfter("dialog", VOICE_DIALOG_MS); });
  }
  function voiceKindWord(opt) {
    return opt.kind === "yes" ? "yes" : opt.kind === "no" ? "no" : opt.kind === "always" ? "always"
         : stripMarkdown(opt.label || "that");
  }
  // A phrase while a dialog listens: "session two yes" answers THAT one
  // (voice stays), "what is it asking" / "say that again" reads this one
  // again, anything else is this dialog's answer.
  function voiceDialogHeard(text) {
    if (voiceAnswerOther(text)) return;
    var c = voiceCommand(text);
    if (c && (c.kind === "question" || c.kind === "repeat") && voice.dialog) { voiceReadDialog(voice.dialog); return; }
    voiceDialogAnswer(text);
  }
  function voiceDialogAnswer(text) {
    var d = voice.dialog;
    if (!d) { voiceIdle(); return; }
    var opt = voiceParseAnswer(text, d.options);
    if (!opt) { voiceDialogRetry(); return; }
    voiceAnswerPost(voice.session, d.id, askOptKeys(opt), voiceKindWord(opt), "");
  }
  // POST /answer, pinned to the dialog it was read from, by the person.
  // This session's: "Answered yes." and it is working again. Another's
  // ("session two yes"): said with its name first, and voice stays here.
  // text: the router's free-text answer (a select question's "Type
  // something"), typed with the keys.
  function voiceAnswerPost(title, dialogId, keys, word, say, text) {
    var mine = title === voice.session, name = nameOf(title);
    voiceSetState("sending", word);
    var body = { keys: keys, dialog_id: dialogId, by: "user" };
    if (text) body.text = String(text);
    fetch("/api/instances/" + encodeURIComponent(title) + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (voice.state === "off" || (mine && voice.session !== title)) return;
        if (r.ok) {
          if (mine) {
            voice.dialog = null;
            voice.asked = false;
            voice.lastSay = { text: "", ts: 0 };   // the agent carries on: a new turn
            voice.answeredAt = Date.now();         // the row reads clarify for a poll or two yet
          }
          askAnsweredByVoice(title, dialogId);
          var line = say ? voiceNameFirst(title, say) : mine ? "Answered " + word + "." : name + ": answered " + word + ".";
          speakLine(line).then(function (ok) {
            if (!ok) return;
            if (mine) voiceSetState("working", ""); else voiceIdle(vbox().value.trim() ? VOICE_KEPT : undefined);
          });
          return;
        }
        if (r.status === 409 && j && j.dialog_changed) {
          askDialogChanged(title);
          if (!mine) { voiceSayThenIdle(name + " is asking something else now."); return; }
          voice.dialog = null;
          voice.dialogKey = "";
          voiceSetState("idle", "");
          voiceDialog();
          return;
        }
        if (r.status === 409 && j && j.dialog_answered) {
          if (mine) voice.dialog = null;
          askDialogAnswered(title);
          voiceSayThenIdle("Already answered.");
          return;
        }
        voiceError((j && j.error) || ("HTTP " + r.status));
        voiceSayThenIdle("Couldn't answer that.");
      });
    }).catch(function (err) {
      if (voice.state === "off") return;
      voiceError(String((err && err.message) || err));
      voiceSayThenIdle("Couldn't answer that.");
    });
  }

  // --- switching, "what needs me", status, summarize ------------------------------
  // Display names (nameOf) and titles: a prefix match, then word overlap,
  // then loose containment; a tie or no match reads the candidates.
  function voiceMatchSession(query) {
    var q = voiceNorm(query);
    var rows = instances.map(function (i) {
      return { title: i.title, name: nameOf(i.title), n: voiceNorm(nameOf(i.title)), t: voiceNorm(i.title) };
    });
    if (!q) return { hit: null, cands: rows };
    var pre = rows.filter(function (r) { return r.n.indexOf(q) === 0 || r.t.indexOf(q) === 0; });
    if (pre.length === 1) return { hit: pre[0], cands: [] };
    if (pre.length > 1) return { hit: null, cands: pre };
    var qw = q.split(" ");
    var scored = rows.map(function (r) {
      var words = (r.n + " " + r.t).split(" ");
      return { r: r, n: qw.filter(function (w) { return words.indexOf(w) >= 0; }).length };
    }).filter(function (x) { return x.n > 0; }).sort(function (a, b) { return b.n - a.n; });
    if (scored.length === 1 || (scored.length > 1 && scored[0].n > scored[1].n)) return { hit: scored[0].r, cands: [] };
    if (scored.length > 1) return { hit: null, cands: scored.map(function (x) { return x.r; }) };
    var loose = rows.filter(function (r) {
      return r.n.indexOf(q) >= 0 || r.t.indexOf(q) >= 0 || (r.n && q.indexOf(r.n) >= 0);
    });
    if (loose.length === 1) return { hit: loose[0], cands: [] };
    return { hit: null, cands: loose };
  }
  function voiceSwitch(query) {
    var m = voiceMatchSession(query);
    if (!m.hit) {
      var cands = m.cands.length ? m.cands : voiceMatchSession("").cands;
      var names = cands.slice(0, 5).map(function (r) { return r.name; }).join(", ");
      var line = m.cands.length ? "Which one: " + names + "?"
               : names ? "No session called " + query + ". Sessions: " + names + "." : "There are no sessions.";
      speakLine(line).then(function (ok) { if (ok) voiceIdle(); });
      return;
    }
    voiceSwitchTo(m.hit.title);
  }
  // "focus parser" / "open login fix": only a session whose name (or
  // title) is said, or starts so — a looser match is a prompt's words.
  function voiceStrictMatch(query) {
    var q = voiceNorm(query).replace(/^the /, "");
    var m = voiceMatchSession(q);
    return !!(q && m.hit && (m.hit.n.indexOf(q) === 0 || m.hit.t.indexOf(q) === 0));
  }
  // The Nth row of the Sessions sheet as it is shown (rowOrder), 1-based.
  function voiceRowTitle(n) {
    return n >= 1 && n <= rowOrder.length ? rowOrder[n - 1] : "";
  }
  // "session two": the second row shown.
  function voiceSwitchIndex(n) {
    var title = voiceRowTitle(n);
    if (!title) { voiceSayThenIdle("There is no session " + n + "."); return; }
    voiceSwitchTo(title);
  }
  // "next session" / "previous session": a step from this session's row,
  // round the ends.
  function voiceSwitchRel(delta) {
    var n = rowOrder.length;
    if (!n) { voiceSayThenIdle("There are no sessions."); return; }
    var i = rowOrder.indexOf(voice.session || current);
    voiceSwitchTo(rowOrder[i < 0 ? 0 : (((i + delta) % n) + n) % n]);
  }
  // Voice follows the selection (voiceOnSelect), as a tap on the row would
  // — the sheet closes the same way.
  function voiceSwitchTo(title) {
    var inst = instByTitle(title) || {};
    closeSheet(sessionsSheet);
    select(title);
    speakLine("Now on " + nameOf(title) + "." + (inst.last_say ? " " + endSentence(inst.last_say) : "")).then(function (ok) {
      if (!ok) return;
      if (inst.activity === "clarify") { voice.dialogKey = ""; voiceDialog(); } else voiceIdle();
    });
  }
  // "What is it asking?": this session's dialog is read out again (and
  // listened for); else the question its Say line asked.
  function voiceQuestion(title) {
    var inst = instByTitle(title), name = nameOf(title);
    if (title === voice.session && inst && inst.activity === "clarify") {
      voice.dialogKey = "";
      voiceSetState("idle", "");
      voiceDialog();
      return;
    }
    var q = voiceAsks(title) === "question"
      ? String((inst && inst.last_say) || (title === voice.session ? voice.lastSay.text : "")) : "";
    voiceSayThenIdle(q ? name + ": " + endSentence(stripMarkdown(q))
                     : inst && inst.activity === "clarify" ? name + " is waiting on you: open the screen."
                     : name + " isn't asking anything.");
  }
  function voiceNeedsMe() {
    var rows = instances.filter(function (i) { return sessionState(i).inBell; })
      .sort(function (a, b) { return attnRank(a) - attnRank(b); }).slice(0, 3);
    var line = rows.length ? rows.map(function (i) {
      var ss = sessionState(i), name = nameOf(i.title);
      if (ss.reason === "question") return endSentence(name + " is asking: " + (i.last_say || "a question"));
      return endSentence(name + " is " + voiceStateWord(ss));
    }).join(" ") : "Nothing needs you.";
    speakLine(line).then(function (ok) { if (ok) voiceIdle(); });
  }
  function voiceStatus() {
    var inst = instByTitle(voice.session);
    var name = nameOf(voice.session);
    var line;
    if (!inst) {
      line = name + " is not listed.";
    } else {
      var ss = sessionState(inst);
      var since = Number(inst.activity_since) || 0;
      var mins = since ? Math.round((Date.now() / 1000 - since) / 60) : 0;
      var dur = inst.activity === "working" && mins >= 1 ? " for " + mins + " minute" + (mins === 1 ? "" : "s") : "";
      line = name + " is " + voiceStateWord(ss) + dur + "." +
             (inst.last_turn ? " " + endSentence(stripMarkdown(inst.last_turn)) : "");
    }
    speakLine(line).then(function (ok) { if (ok) voiceIdle(); });
  }
  // One model call on the server (10–45 s): the newest reply in two sentences.
  function voiceCondense() {
    var title = voice.session;
    voiceSetState("working", "Summarizing…");
    fetch("/api/instances/" + encodeURIComponent(title) + "/voice/condense", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (voice.state === "off" || voice.session !== title) return;
        var line = r.ok && j && j.text ? j.text
                 : r.status === 504 ? "That took too long."
                 : (j && j.error) || "Nothing to summarize.";
        speakLine(line).then(function (ok) { if (ok) voiceIdle(); });
      });
    }).catch(function () {
      if (voice.state === "off") return;
      speakLine("Couldn't summarize.").then(function (ok) { if (ok) voiceIdle(); });
    });
  }

  // --- on / off, the flag, the wake lock, config --------------------------------
  function voiceSetFlag(title, on) {
    return fetch("/api/instances/" + encodeURIComponent(title) + "/voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: !!on }),
    }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function voiceLoadCfg() {
    return fetch("/api/voice")
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (j) {
        // 404 / unreachable: the defaults the first time, else what we had
        // (a blink of the network must not swap adapters mid-conversation).
        if (j && typeof j === "object") {
          voice.cfg.stt = j.stt === "server" ? "server" : "phone";
          voice.cfg.tts = j.tts === "server" ? "server" : "phone";
          voice.cfg.approve_dialogs = j.approve_dialogs !== false;
          voice.cfg.server_ready = j.server_ready && typeof j.server_ready === "object" ? j.server_ready : {};
          voice.cfg.backends = j.backends && typeof j.backends === "object" ? j.backends : { stt: [], tts: [] };
          voice.cfg.system = j.system && typeof j.system === "object" ? j.system : null;
          if (typeof j.say_all === "boolean") voiceSayAllSync(j.say_all);
        } else if (!voice.cfg.loaded) {
          voice.cfg.server_ready = {};
        }
        voice.cfg.loaded = true;
        voiceApplyAdapters();
        voiceSheetCfg();
        return voice.cfg;
      });
  }
  // The server's say-all marker against the Spoken replies pref: on load
  // (the first GET that carries it) a phone on Everything turns the marker
  // ON when it is off, once. It never turns it off on load — another
  // device may be the one listening to everything; off is only ever an
  // explicit switch away. An older server (no say_all) is left alone.
  function voiceSayAllSync(serverOn) {
    voice.cfg.say_all = serverOn;
    if (voice.sayAll.synced) return;
    voice.sayAll.synced = true;
    if (voice.prefs.replies === "all" && !serverOn) voiceSayAllPost(true);
  }
  function voiceSayAllPost(on) {
    voice.sayAll.synced = true;
    return fetch("/api/voice/say-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: !!on }),
    }).then(function (r) {
      if (r.status === 403) {
        if (!voice.sayAll.denied) {
          voice.sayAll.denied = true;
          flashStatus("This device can't change whether every prompt asks for a Say line — Everything still reads what this phone sends");
        }
        return null;
      }
      return r.ok ? r.json().catch(function () { return null; }) : null;
    }).then(function (j) {
      if (j && typeof j.say_all === "boolean") voice.cfg.say_all = j.say_all;
    }).catch(function () {});
  }
  // Which adapter does what, from GET /api/voice: the server's when Settings
  // say "server" AND the server reports ready (and this browser can record /
  // play), else the phone's. A swap mid-listen restarts the recognizer; a
  // server that is asked for but not ready is said once, in the strip.
  function voiceApplyAdapters() {
    var A = voiceAdapters, ready = voice.cfg.server_ready || {};
    var stt = voice.cfg.stt === "server" && ready.stt && A.serverStt.available() ? A.serverStt : A.phoneStt;
    var tts = voice.cfg.tts === "server" && ready.tts && A.serverTts.available() ? A.serverTts : A.phoneTts;
    if (voice.tts !== tts) {
      voice.tts.cancel();
      voice.tts = tts;
    }
    if (voice.stt !== stt) {
      var was = voice.stt.active();
      voice.stt.stop();
      voice.stt = stt;
      if (was && voiceIsListening()) voiceRecStart();
      if (stt === A.serverStt) {
        // The home-screen app can listen after all: no Safari needed.
        safariBanner.classList.add("hidden");
        if (voice.state === "idle" && !voiceIsListening()) voiceCaption(voiceTalkHint());
        fitSoon();
      }
    }
    if (voice.cfg.stt === "server" && !ready.stt && !voice.sttHinted && voice.state !== "off") {
      voice.sttHinted = true;
      voiceError("Server recognition isn't ready — " +
        (ready.stt_detail || ready.detail || "see Settings → Voice") + ". Using the phone's.");
    }
    safariAltEl.classList.toggle("hidden", voice.cfg.stt === "server");
  }
  // Give the microphone back: every adapter that may hold it — now, or (ms:
  // the page hid) the server recognizer's warm stream in ms unless it is
  // used again first.
  function voiceSttRelease(ms) {
    var A = voiceAdapters;
    [A.phoneStt, A.serverStt].forEach(function (a) { if (a && a.release) a.release(ms); });
  }
  function voiceWakeLock() {
    if (!navigator.wakeLock || voice.wake || document.hidden) return;
    try {
      navigator.wakeLock.request("screen").then(function (lock) {
        voice.wake = lock;
        lock.addEventListener("release", function () { if (voice.wake === lock) voice.wake = null; });
      }).catch(function () {});
    } catch (e) {}
  }
  function voiceWakeRelease() {
    var l = voice.wake;
    voice.wake = null;
    if (l) { try { l.release(); } catch (e) {} }
  }
  // Inside the gesture, before anything async: the empty utterance that
  // unlocks speech, then the audio session so the silent switch is ignored.
  function voicePrime() {
    voice.tts.prime();
    // The server adapters play and meter through one AudioContext, which
    // iOS and Chrome run only when it was made (or resumed) in a gesture.
    if (voice.cfg.stt === "server" || voice.cfg.tts === "server" || voice.prefs.wake) voiceAudioCtx();
    voice.primed = true;
    try {
      if (navigator.audioSession && navigator.audioSession.type !== "playback") navigator.audioSession.type = "playback";
    } catch (e) {}
  }
  function voiceTurnOn(title, listenAfter) {
    voice.session = title;
    var inst = instByTitle(title) || {};
    // What the session last said is on the screen already: heard, not news,
    // whatever its stamp (a phone clock is not the server's): until the
    // first send, that text is never spoken.
    voice.lastSay = { text: String(inst.last_say || ""), ts: Infinity };
    voice.pendingSay = null;
    voice.expectSay = null;
    voice.dialogKey = "";
    voiceHearHide();
    voiceSetState("idle", voiceTalkHint());
    voicePersist();
    voiceWakeLock();
    voiceEventsOpen();
    voiceLoadCfg();
    if (!inst.voice) voiceSetFlag(title, true);
    if (listenAfter) voiceListen("prompt", VOICE_TAP_MS);
  }
  function voiceMoveTo(title) {
    var inst = instByTitle(title) || {};
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    voiceSpeechStop(false);
    voiceAckStop();
    voice.session = title;
    voice.lastSay = { text: String(inst.last_say || ""), ts: Infinity };
    voice.pendingSay = null;
    voice.expectSay = null;      // the last prompt went to another session
    voice.asked = false;
    voice.dialog = null;
    voice.dialogKey = "";
    voiceSetState("idle", voiceTalkHint());
    voicePersist();
    if (!inst.voice) voiceSetFlag(title, true);
    if (!voiceSheet.classList.contains("hidden")) voiceSheetSession.textContent = nameOf(title);
  }
  function voiceOff() {
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    voiceSttRelease();
    voiceSpeechStop(true);
    voiceAckStop();
    // Everything keeps the socket: other sessions' turns are still read.
    if (voice.prefs.replies !== "all") {
      voiceEventsClose();
      voice.eventsSeq = 0;
    }
    voiceWakeRelease();
    voice.pendingSay = null;
    voice.dialog = null;
    voice.box = null;
    voiceSetState("off", "");
    voiceError("");
    voicePersist();
  }

  // --- the wake word: "MindFlock" on its own starts listening --------------------
  // prefs.wake (the sheet's "Listen for “MindFlock”", off by default). While
  // voice is on, the page is in front and nothing else has the mic or the
  // speaker — not listening, nothing being recorded or on its way, no line
  // being said nor VOICE_WAKE_AFTER_SPEECH_MS after one (voiceWakeGate) — the
  // server recognizer's warm stream feeds the standby (ServerSTT.standby): a
  // level detector on the phone (voiceWakeStep), no server traffic. A lone
  // utterance — quiet before it, VOICE_WAKE_MIN_MS..VOICE_WAKE_MAX_MS of
  // sound, quiet after — is cut out (+VOICE_WAKE_LEAD_MS of lead-in) and sent
  // as a 16 kHz WAV to POST /api/voice/wake: one in flight, one per
  // VOICE_WAKE_GAP_MS. The server says whether it was the word alone (a
  // sentence never is). wake → the chime (voiceWakeChime), "Yes?", a tick,
  // then listening exactly as a tap does (toggle); anything else → nothing.
  // 409 (no recognizer) / 404 (an older server) / the mic refused → the pref
  // goes off and the strip says why. The mic's ring shows the standby
  // (data-standby) and still moves with the room. Hidden → the standby stops
  // and the stream goes back as for any hidden page; visible → it starts
  // again (a new getUserMedia when the browser cut the stream); a context the
  // browser will not resume outside a gesture (iOS) waits for one tap
  // anywhere. While the phone's own recognizer listens, the stream is given
  // back: that recognizer needs the mic to itself.
  var VOICE_WAKE_GAP_MS = 1500;           // one check per this long
  var VOICE_WAKE_AFTER_SPEECH_MS = 500;   // no standby while the page speaks and this long after
  var VOICE_WAKE_CHIME_MS = 130;          // the chime's length: listening opens after it
  var VOICE_WAKE_TIMEOUT_MS = 10000;      // a check's own cap
  var VOICE_WAKE_GESTURE_MS = 5000;       // a refused mic this long after a tap is a refusal, else "tap once"
  var voiceWake_ = { inflight: false, at: 0, speaking: false, spokeAt: 0, timer: null, needTap: false,
                     tapArmed: false, gestureAt: 0, gen: 0, checks: 0, wakes: 0, chimes: 0, last: null, note: "" };
  voice.wakeState = voiceWake_;   // tests read it
  // Any tap: a refused microphone right after one is the person's answer.
  document.addEventListener("click", function () { voiceWake_.gestureAt = Date.now(); }, true);
  // Pure: why a lone utterance is not checked now ("" = it is). g: on,
  // hidden, listening, recording, speaking, spokeAgo, inflight, sinceCheck.
  function voiceWakeGate(g) {
    if (!g.on) return "off";
    if (g.hidden) return "hidden";
    if (g.listening) return "listening";
    if (g.recording) return "recording";
    if (g.speaking || g.spokeAgo < VOICE_WAKE_AFTER_SPEECH_MS) return "speaking";
    if (g.inflight) return "inflight";
    if (g.sinceCheck < VOICE_WAKE_GAP_MS) return "rate";
    return "";
  }
  voice.wakeGate = voiceWakeGate;
  function voiceWakeLive() {
    var A = voiceAdapters, W = voiceWake_, now = Date.now();
    return voiceWakeGate({
      on: !!voice.prefs.wake && voice.state !== "off" && !W.needTap,
      hidden: document.hidden,
      listening: voiceIsListening(),
      recording: A.serverStt.active() || A.serverStt.pending() || A.phoneStt.active(),
      speaking: !!voiceSpeech.playing,
      spokeAgo: now - W.spokeAt,
      inflight: W.inflight,
      sinceCheck: now - W.at,
    });
  }
  // The standby follows the gate: it runs unless the gate is shut for more
  // than the check itself (in flight, the rate limit: the detector goes on).
  function voiceWakeSync() {
    var A = voiceAdapters.serverStt, W = voiceWake_;
    var want = !!voice.prefs.wake && voice.state !== "off" && !document.hidden;
    if (want && !A.available()) { voiceWakeOff("No microphone for a web page here — listening for “MindFlock” is off."); return; }
    var ready = voice.cfg.server_ready || {};
    if (want && ready.stt === false) {
      voiceWakeOff("Listening for “MindFlock” is off: the server has no speech recognition ready (Settings → Voice).");
      return;
    }
    A.hold(want);
    var why = want ? voiceWakeLive() : "off";
    var run = why === "" || why === "inflight" || why === "rate";
    clearTimeout(W.timer);
    if (why === "speaking" && !voiceSpeech.playing) {
      W.timer = setTimeout(voiceWakeSync, Math.max(0, VOICE_WAKE_AFTER_SPEECH_MS - (Date.now() - W.spokeAt)) + 20);
    }
    if (run && !A.standbyOn()) A.standby(voiceWakeHandlers);
    else if (!run && A.standbyOn()) {
      A.standbyStop();
      if (!voiceIsListening()) voiceLevel(null);
    }
    if (want && why === "listening" && voice.stt !== A && A.warm()) A.release();
    if (voiceBtn.hasAttribute("data-standby") !== run) voiceBtn.toggleAttribute("data-standby", run);
    voiceWakeNoteShow();
  }
  var voiceWakeHandlers = {
    onLevel: function (v) { if (!voiceIsListening()) voiceLevel(v, true); },
    wanted: function () { return voiceWakeLive() === ""; },
    onClip: function (blob) { voiceWakeCheck(blob); },
    onReady: function (ctx) { voiceWakeCtxCheck(ctx); },
    onError: function (code) {
      // Refused right after a tap: refused. Long after (the page came back,
      // no gesture): the browser wants one — a tap anywhere tries again.
      if (code === "not-allowed" && Date.now() - voiceWake_.gestureAt > VOICE_WAKE_GESTURE_MS) { voiceWakeNeedTap(); return; }
      voiceWakeOff(code === "not-allowed" ? "Microphone permission denied — listening for “MindFlock” is off."
        : code === "audio-capture" ? "No microphone found — listening for “MindFlock” is off."
        : "The microphone did not start (" + code + ") — listening for “MindFlock” is off.");
    },
  };
  // The audio flows only through a running context: one the browser keeps
  // suspended (iOS, back from the background, no gesture) waits for a tap.
  function voiceWakeCtxCheck(ctx) {
    if (!ctx || ctx.state === "running") return;
    var done = false;
    function check() {
      if (done) return;
      done = true;
      if (ctx.state !== "running" && voice.prefs.wake && voice.state !== "off") voiceWakeNeedTap();
    }
    try { ctx.resume().then(check, check); } catch (e) {}
    setTimeout(check, 600);
  }
  function voiceWakeNeedTap() {
    var W = voiceWake_;
    W.needTap = true;
    voiceWakeSync();
    if (voice.state === "idle") voiceCaption("Tap anywhere to listen for “MindFlock” again");
    if (W.tapArmed) return;
    W.tapArmed = true;
    document.addEventListener("click", function once() {
      document.removeEventListener("click", once, true);
      W.tapArmed = false;
      voiceAudioCtx();   // inside the gesture
      W.gestureAt = Date.now();
      W.needTap = false;
      voiceWakeSync();
    }, true);
  }
  // Pref off, with the reason in the strip and the sheet.
  function voiceWakeOff(msg) {
    voice.prefs.wake = false;
    voiceWake_.note = msg || "";
    voiceWake_.needTap = false;
    voicePersist();
    voiceWakeEl.checked = false;
    if (msg && voice.state !== "off") voiceError(msg);
    voiceWakeSync();
  }
  function voiceWakeSpeaking(on) {
    var W = voiceWake_;
    if (W.speaking === on) return;
    if (!on) W.spokeAt = Date.now();
    W.speaking = on;
    voiceWakeSync();
  }
  function voiceWakeCheck(blob) {
    var W = voiceWake_;
    if (voiceWakeLive()) return;   // something took over while the clip was encoded
    W.inflight = true;
    W.at = Date.now();
    W.checks++;
    var t0 = W.at, a = window.AbortController ? new AbortController() : null;
    var timer = a ? setTimeout(function () { try { a.abort(); } catch (e) {} }, VOICE_WAKE_TIMEOUT_MS) : null;
    fetch("/api/voice/wake?lang=" + encodeURIComponent(String(navigator.language || "en").split(";")[0]), {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: blob,
      signal: a ? a.signal : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j || {} }; });
    }).then(function (x) {
      clearTimeout(timer);
      W.inflight = false;
      if (x.r.status === 409 || x.r.status === 404 || x.r.status === 405) {
        W.last = { error: String(x.j.error || ("HTTP " + x.r.status)), at: Date.now() };
        voiceWakeOff(x.r.status === 409
          ? "Listening for “MindFlock” is off: the server has no speech recognition ready (Settings → Voice)."
          : "Listening for “MindFlock” is off: this server is too old for it.");
        return;
      }
      if (!x.r.ok) { W.last = { error: String(x.j.error || ("HTTP " + x.r.status)), at: Date.now() }; return; }
      W.last = { text: String(x.j.text || ""), wake: !!x.j.wake, ms: Number(x.j.ms) || (Date.now() - t0), error: "", at: Date.now() };
      if (x.j.wake) voiceWoke();
    }).catch(function (err) {
      clearTimeout(timer);
      W.inflight = false;
      W.last = { error: err && err.name === "AbortError" ? "the server took too long" : String((err && err.message) || err), at: Date.now() };
    });
  }
  // The word: the chime, "Yes?", a tick — then listening, as a tap in
  // toggle mode (a line being said is cut first). A tap or a send that came
  // first wins.
  function voiceWoke() {
    var W = voiceWake_;
    if (voice.state === "off" || voice.state === "sending" || voiceIsListening() || document.hidden) return;
    W.wakes++;
    voiceWakeChime();
    voice.buzzAt = Date.now();
    if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) {} }
    voiceCaption("Yes?");
    var gen = ++W.gen;
    setTimeout(function () {
      if (W.gen !== gen || voice.state === "off" || voice.state === "sending" || voiceIsListening()) return;
      voiceBargeIn();
      voice.box = null;
      voiceListen("prompt", VOICE_TAP_MS);
    }, VOICE_WAKE_CHIME_MS);
  }
  // Two short rising tones (~120 ms) through the page's AudioContext — made
  // or resumed by the tap that turned voice / the standby on.
  function voiceWakeChime() {
    var ctx = voice.audioCtx;
    if (!ctx || ctx.state !== "running") return false;
    try {
      var t = ctx.currentTime;
      [[660, 0], [990, 0.06]].forEach(function (n) {
        var o = ctx.createOscillator(), g = ctx.createGain(), at = t + n[1];
        o.type = "sine";
        o.frequency.value = n[0];
        g.gain.setValueAtTime(0.0001, at);
        g.gain.exponentialRampToValueAtTime(0.25, at + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, at + 0.06);
        o.connect(g);
        g.connect(ctx.destination);
        o.start(at);
        o.stop(at + 0.065);
      });
      voiceWake_.chimes++;
      return true;
    } catch (e) { return false; }
  }
  // The line under the sheet's switch: what the standby is doing, or why it
  // went off.
  function voiceWakeNoteText() {
    var W = voiceWake_;
    if (!voice.prefs.wake) return W.note;
    if (W.needTap) return "Paused — tap anywhere to listen again";
    if (voice.state === "off") return "Starts with voice mode";
    return voiceBtn.hasAttribute("data-standby") ? "Listening for “MindFlock” — the mic is open" : "Paused while the phone listens or speaks";
  }
  function voiceWakeNoteShow() {
    var t = voiceWakeNoteText();
    if (voiceWakeNote.textContent !== t) voiceWakeNote.textContent = t;
    voiceWakeNote.classList.toggle("hidden", !t);
  }

  // --- the gestures -------------------------------------------------------------------
  // click (a real activation on iOS and Android), never touchstart. Push to
  // talk, prefs.ptt: "toggle" — a tap starts listening, the next one (on the
  // mic or the strip) stops it, the words kept ("over"); "hold" —
  // pointerdown on the mic starts, letting go stops (voiceHoldStart /
  // voiceHoldEnd), and the click that release
  // fires is swallowed. The click still does everything a hold does not:
  // voice off → on, and a tap from a keyboard or a screen reader.
  var voiceHold = { on: false, swallowUntil: 0 };
  function voiceTap() {
    voicePrime();   // FIRST, synchronously — see the header
    if (voiceHold.swallowUntil > Date.now()) { voiceHold.swallowUntil = 0; return; }   // a hold's own click
    if (!current) { flashStatus("no session to talk to — pick one"); return; }
    var s = voice.state;
    if (!voice.stt.available()) {
      // No recognizer (an iPhone home-screen app, Firefox…): speech OUT still
      // works, so voice mode turns on as read-aloud — replies and dialogs
      // are spoken, the sheet's Diagnostics say why nothing listens — and
      // iOS gets the Safari banner with the address to carry over.
      if (IOS) voiceShowSafariBanner();
      if (s === "off") {
        voiceTurnOn(current, false);
        voiceCaption("No speech recognition here — replies are still read aloud");
      } else {
        voiceError("No speech recognition in this browser" + (IOS ? " — open the page in a Safari tab." : "."));
      }
      return;
    }
    // Hold to talk: the first tap turns voice on; the next press talks.
    if (s === "off") { voiceBargeIn(); voice.box = null; voiceTurnOn(current, voice.prefs.ptt !== "hold"); return; }
    if (voiceIsListening()) { voiceListenOver(); return; }
    if (s === "sending") return;
    // idle, working or speaking: talk — a line being said is cut first (barge-in)
    voiceBargeIn();
    voice.box = null;
    voiceListen("prompt", VOICE_TAP_MS);
  }
  function voiceHoldStart(ev) {
    voicePrime();   // FIRST, synchronously — see the header
    voiceHold.swallowUntil = 0;   // a new press: the last release's click came (or never will)
    if (voice.prefs.ptt !== "hold" || (ev && ev.button)) return;
    var s = voice.state;
    if (s === "off" || s === "sending" || voiceIsListening() || !current || !voice.stt.available()) return;   // the click handles these
    voiceHold.on = true;
    try { voiceBtn.setPointerCapture(ev.pointerId); } catch (e) {}   // the release lands here even off the button
    voiceBargeIn();
    voice.box = null;
    voiceListen("prompt", VOICE_DICT_IDLE_MS, true);   // the finger says when it ends
  }
  function voiceHoldEnd(ev) {
    voicePrime();   // FIRST, synchronously: a touch activates on the release
    if (!voiceHold.on) return;
    voiceHold.on = false;
    // pointerup is followed by a click that is not a tap; pointercancel is not.
    if (!ev || ev.type !== "pointercancel") voiceHold.swallowUntil = Date.now() + 1000;
    if (voiceIsListening() && voiceListen_.held) voiceListenOver();
  }
  // Cancel: while dictating the words go too; otherwise whatever is being
  // said or awaited stops.
  function voiceCancelTap() {
    voiceSpeechStop(true);
    voiceAckStop();
    if (voiceIsListening()) { vbox().value = ""; vboxSized(); voice.dictated = false; }
    voiceListen_.until = 0;
    voiceListen_.overAfter = false;
    voiceListen_.gen++;   // a route answer on its way is for nobody now
    voiceIdle();
  }
  // A box emptied by hand holds nothing dictated.
  composeEl.addEventListener("input", function () { if (!composeEl.value.trim()) voice.dictated = false; });
  voiceBtn.addEventListener("click", voiceTap);
  voiceBtn.addEventListener("pointerdown", voiceHoldStart);
  voiceBtn.addEventListener("pointerup", voiceHoldEnd);
  voiceBtn.addEventListener("pointercancel", voiceHoldEnd);
  // A long press is a hold, not the phone's context menu.
  voiceBtn.addEventListener("contextmenu", function (ev) { if (voice.prefs.ptt === "hold") ev.preventDefault(); });
  voiceCancelBtn.addEventListener("click", voiceCancelTap);
  voiceHushBtn.addEventListener("click", voiceHush);
  // The strip is the mic's big twin: a tap there while listening stops
  // too; while it talks ("Speaking…", the line), it stops talking.
  voiceStrip.addEventListener("click", function (ev) {
    if (ev.target === voiceStrip || ev.target === voiceStateEl || voiceCaptionEl.contains(ev.target) ||
        ev.target === voiceLevelEl || ev.target === voiceLevelBar) {
      if (voiceIsListening()) voiceListenOver();
      else if (voiceSpeech.playing) voiceHush();
    }
  });

  // The one big button a deep link / a resume needs: a page cannot speak
  // until it has been tapped once.
  function voiceHearShow(label, run) {
    voice.hearPending = run;
    voiceHearBtn.textContent = label;
    voiceHearBtn.classList.remove("hidden");
    voiceStrip.classList.remove("hidden");
    fitSoon();
  }
  function voiceHearHide() {
    voice.hearPending = null;
    voiceHearBtn.classList.add("hidden");
    if (voice.state === "off") voiceStrip.classList.add("hidden");
    fitSoon();
  }
  voiceHearBtn.addEventListener("click", function () {
    voicePrime();   // in the gesture, first
    var run = voice.hearPending;
    voiceHearHide();
    if (run) run();
  });
  function voiceHearNow(title) {
    var inst = instByTitle(title) || {};
    if (inst.activity === "clarify") { voice.dialogKey = ""; voiceDialog(); return; }
    voiceFetchSay(title).then(function (j) {
      if (voice.session !== title || voice.state === "off") return;
      if (j && j.say) {
        voice.lastSay = { text: "", ts: 0 };   // the deep link's whole point is hearing it
        voiceReply({ say: j.say, has_question: j.has_question, ts: j.ts, at: j.ts });
      } else {
        // Nothing to hear (no turn yet, or no words came out of it): say so
        // rather than go quiet, and listen — the tap was for a conversation.
        var line = inst.activity === "working" ? nameOf(title) + " is still working."
                 : nameOf(title) + " has nothing new to say.";
        speakLine(line).then(function (ok) { if (ok) voiceListenAfter("prompt", VOICE_TAP_MS); });
      }
    });
  }
  // select() calls this: voice follows the selected session; ?voice=1 and a
  // remembered voice session get their one-tap offer on the first selection.
  function voiceOnSelect(title) {
    if (!title) return;
    if (voice.state !== "off") {
      if (title !== voice.session) voiceMoveTo(title);
      return;
    }
    if (voice.deepLink && !voice.armed) {
      voice.armed = true;
      if (IOS && !voice.stt.available()) voiceShowSafariBanner();
      voiceHearShow("Tap to hear " + nameOf(title), function () {
        voiceTurnOn(title, false);
        voiceHearNow(title);
      });
      return;
    }
    if (voice.saved && voice.saved.on && !voice.resumeOffered) {
      voice.resumeOffered = true;
      voiceHearShow("Tap to resume voice", function () {
        voiceTurnOn(title, false);
        voiceHearNow(title);
      });
    }
  }

  // --- hidden / visible -----------------------------------------------------------------
  document.addEventListener("visibilitychange", function () {
    if (voice.state === "off") {
      // Everything with voice off: the socket and the queue follow the page.
      if (document.hidden) { voiceSpeechStop(true); voiceEventsClose(); }
      else if (voice.prefs.replies === "all") { voice.eventsBackoff = 1000; voiceEventsOpen(); }
      return;
    }
    if (document.hidden) {
      clearTimeout(voiceListen_.timer);
      voice.stt.stop();
      // A quick look elsewhere comes back to a warm mic; a long one does not.
      voiceSttRelease(VOICE_KEEP_HIDDEN_MS);
      voiceSpeechStop(true);
      voiceAckStop();
      voiceEventsClose();
      voiceWakeRelease();
      if (voice.state !== "working") voiceSetState("idle", voiceTalkHint());
      voiceWakeSync();   // the standby stops; its stream went back with the rest
      return;
    }
    voiceWakeLock();
    voiceWakeSync();   // the standby again (the stream re-asked for if the browser cut it)
    voice.eventsBackoff = 1000;
    voiceEventsOpen();
    voiceLoadCfg();   // Settings may have moved speech to the server meanwhile
    var title = voice.session;
    voiceFetchSay(title).then(function (j) {
      if (!j || voice.session !== title || voice.state === "off") return;
      if (j.say && !voiceSaidAlready(String(j.say), Number(j.ts)))
        voiceReply({ say: j.say, has_question: j.has_question, ts: j.ts, at: j.ts });
    });
  });

  // --- the Safari banner (iOS without a recognizer) -------------------------------
  function voiceShowSafariBanner() {
    safariUrlEl.value = current
      ? location.origin + "/m?s=" + encodeURIComponent(current) + "&voice=1"
      : location.href;
    // The other way out: server recognition works in the home-screen app.
    safariAltEl.classList.toggle("hidden", voice.cfg.stt === "server");
    safariBanner.classList.remove("hidden");
    fitSoon();
  }
  safariCopyBtn.addEventListener("click", function () {
    var url = safariUrlEl.value;
    function fallback() { safariUrlEl.focus(); safariUrlEl.select(); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { flashStatus("copied — paste it into Safari"); }, fallback);
    } else {
      fallback();
    }
  });
  safariCloseBtn.addEventListener("click", function () { safariBanner.classList.add("hidden"); fitSoon(); });

  // --- the voice sheet ------------------------------------------------------------------
  function voiceSheetOpen() {
    voiceOnToggle.checked = voice.state !== "off";
    voiceSheetSession.textContent = current ? nameOf(current) : "this session";
    voiceHandsEl.checked = !!voice.prefs.handsFree;
    voicePttEl.value = voice.prefs.ptt;
    voiceRepliesEl.value = voice.prefs.replies;
    voiceWakeEl.checked = !!voice.prefs.wake;
    voiceWakeNoteShow();
    voiceRateEl.value = String(voice.prefs.rate);
    voiceRateVal.textContent = voice.prefs.rate.toFixed(1) + "×";
    voiceFillVoices();
    voiceSheetCfg();
    voiceLoadCfg();
    voiceDiag();
    clearInterval(voice.diagTimer);
    voice.diagTimer = setInterval(voiceDiag, 1000);
    openSheet(voiceSheet);
  }
  function voiceSheetClose() {
    clearInterval(voice.diagTimer);
    voice.diagTimer = null;
    closeSheet(voiceSheet);
  }
  function voiceSheetCfg() {
    voiceApproveLine.textContent = "Approve dialogs by voice: " +
      (voice.cfg.approve_dialogs ? "on" : "off") + " (Settings → Your phone)";
  }
  // The voices this phone offers a web page (iOS: the compact built-in ones),
  // this language first.
  function voiceFillVoices() {
    var vs = (voice.tts.voices ? voice.tts.voices() : []).slice();
    var lang = (navigator.language || "en").toLowerCase();
    var primary = lang.split("-")[0];
    function rank(v) {
      var l = (v.lang || "").toLowerCase().replace("_", "-");
      return l === lang ? 0 : l.indexOf(primary) === 0 ? 1 : 2;
    }
    vs.sort(function (a, b) { return rank(a) - rank(b) || String(a.name).localeCompare(String(b.name)); });
    voiceVoiceEl.innerHTML = "";
    var def = document.createElement("option");
    def.value = "";
    def.textContent = "Default voice";
    voiceVoiceEl.appendChild(def);
    vs.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v.name;
      o.textContent = v.name + " (" + v.lang + ")";
      voiceVoiceEl.appendChild(o);
    });
    voiceVoiceEl.value = voice.prefs.voiceName || "";
    if (voiceVoiceEl.value !== (voice.prefs.voiceName || "")) voiceVoiceEl.value = "";
  }
  if (window.speechSynthesis && window.speechSynthesis.addEventListener) {
    try {
      window.speechSynthesis.addEventListener("voiceschanged", function () {
        if (!voiceSheet.classList.contains("hidden")) voiceFillVoices();
      });
    } catch (e) {}
  }
  // Diagnostics: what THIS phone can do, stated plainly, so a silent failure
  // has a name (the owner's Phase-0 spike surface).
  function voiceDiag() {
    var vs = voice.tts.voices ? voice.tts.voices() : [];
    // The server's built-in recognizer / voice (Windows, macOS): an older
    // server sends no `system`, and then the line is left out.
    var sys = voice.cfg.system;
    var lines = [
      "Recognition: " + (voice.stt.available()
        ? "available (" + (window.SpeechRecognition ? "SpeechRecognition" : "webkitSpeechRecognition") + ")"
        : "NOT available" + (IOS ? " — iOS has it only in a Safari tab" : "")),
      "Speech: " + (voice.tts.available()
        ? "available, " + vs.length + " voice" + (vs.length === 1 ? "" : "s") + " loaded"
        : "NOT available"),
      "Mode: " + (STANDALONE ? "home-screen app (standalone)" : "browser tab") +
        (IOS ? ", iOS" : ANDROID ? ", Android" : ""),
      "Wake lock: " + (navigator.wakeLock ? (voice.wake ? "held" : "supported") : "not supported"),
      "Audio session: " + (navigator.audioSession ? String(navigator.audioSession.type) : "not supported"),
      "Events socket: " + voice.eventsState + (voice.eventsSeq ? " (seq " + voice.eventsSeq + ")" : ""),
      "Adapters: recognition " + voice.stt.name + ", voice " + voice.tts.name,
      "Backends: recognition " + voiceDiagBackend("stt") + ", voice " + voiceDiagBackend("tts"),
      sys && sys.platform ? "Built-in engine: " + sys.platform + (sys.detail ? " — " + sys.detail : "") : "",
      "Recorder: " + (voiceAdapters.serverStt.available()
        ? "WAV 16 kHz (ScriptProcessor)" + (voice.rec.mime ? "" : ", no clip yet")
        : "not supported (no Web Audio or microphone)"),
      "Resampler: " + (voice.rec.resampler ||
        ((window.OfflineAudioContext || window.webkitOfflineAudioContext)
          ? "OfflineAudioContext (the browser's), no clip yet" : "low-pass decimator (no OfflineAudioContext)")),
      "Silence stop: " + VOICE_SILENCE_MS + " ms after speech (clip cap " + VOICE_CLIP_MAX_MS / 1000 + " s)",
      voice.cfg.stt === "server" ? "Recognition stream: " + voiceDiagStream() : "",
      voice.cfg.stt === "server" ? "Microphone: " + (voiceAdapters.serverStt.warm()
        ? "warm (kept " + Math.round(voice.keepMs / 1000) + " s after the last phrase)" : "not held") : "",
      voice.rec.trace ? "Last start: " + voiceDiagTrace(voice.rec.trace) : "",
      "AudioContext: " + (voice.audioCtx ? String(voice.audioCtx.state)
        : (window.AudioContext || window.webkitAudioContext) ? "not created — the server adapters make it on a tap" : "not supported"),
      "Last transcribe: " + voiceDiagLast(voice.rec.last),
      "Last speak: " + voiceDiagLast(voice.rec.lastSpeak),
      "Primed: " + (voice.primed ? "yes" : "no — tap the mic once"),
      "Push to talk: " + (voice.prefs.ptt === "hold" ? "hold" : "tap"),
      "Replies: " + ({ off: "off", mine: "to what you said by voice", all: "everything" })[voice.prefs.replies],
      "Wake word: " + voiceDiagWake(),
      "Last prompt: " + (voice.lastPrompt
        ? (voice.lastPrompt.dictated ? "dictated" : "typed") + ", say " + (voice.lastPrompt.say ? "yes" : "no")
        : "none yet"),
      "Router: " + (voice.lastRoute || "not asked yet"),
      "Session numbers: " + (rowOrder.length
        ? rowOrder.map(function (t, i) { return (i + 1) + " " + nameOf(t); }).join(", ") +
          " — the Sessions sheet's order, held while it is open"
        : "none yet"),
      "State: " + voice.state + (voice.session ? " · " + nameOf(voice.session) : ""),
    ].filter(Boolean);
    if (voice.lastError) lines.push("Last error: " + voice.lastError);
    voiceDiagEl.textContent = lines.join("\n");
  }
  // "server (ready: openai)" / "server (not ready: openai: no API key)" / "phone"
  function voiceDiagBackend(kind) {
    var where = voice.cfg[kind], ready = voice.cfg.server_ready || {};
    if (where !== "server") return where;
    return ready[kind] ? "server (ready: " + (ready[kind + "_backend"] || "?") + ")"
                       : "server (not ready: " + (ready[kind + "_detail"] || ready.detail || "?") + ")";
  }
  // "words as you speak (last chunk 180 ms, 0.6 s of audio)" / "one clip per phrase"
  function voiceDiagStream() {
    var ready = voice.cfg.server_ready || {}, c = voice.rec.chunk;
    var last = !c ? "no chunk yet"
      : c.error ? "last chunk failed — " + c.error
      : "last chunk " + c.ms + " ms" + (c.audio ? ", " + c.audio.toFixed(1) + " s of audio" : "");
    return ready.stt_stream ? "words as you speak (" + last + ")" : "one clip per phrase" + (c ? " (" + last + ")" : "");
  }
  // "mic 3 ms, stream open 41 ms, speech 420 ms, words 610 ms, settled 905 ms after the start"
  function voiceDiagTrace(t) {
    var names = { mic: "mic", open: "stream open", speech: "speech", words: "words", settled: "settled" };
    var parts = ["mic", "open", "speech", "words", "settled"].filter(function (k) { return k in t; })
      .map(function (k) { return names[k] + " " + t[k] + " ms"; });
    return parts.length ? parts.join(", ") + " after the start" : "nothing yet";
  }
  // "standby, mic open (3 checks; last “Mindflock.” → wake, 240 ms)" / "off"
  function voiceDiagWake() {
    var W = voiceWake_, x = W.last;
    if (!voice.prefs.wake) return "off" + (W.note ? " — " + W.note : "");
    var now = voiceBtn.hasAttribute("data-standby") ? "standby, mic open" : W.needTap ? "waiting for a tap" : "paused";
    var last = !x ? "no check yet" : x.error ? "last check failed — " + x.error
      : "last “" + x.text + "” → " + (x.wake ? "wake" : "not the word") + ", " + x.ms + " ms";
    return now + " (" + W.checks + " check" + (W.checks === 1 ? "" : "s") + ", " + W.wakes + " wake" + (W.wakes === 1 ? "" : "s") + "; " + last + ")";
  }
  function voiceDiagLast(x) {
    if (!x) return "none yet";
    return x.error ? "failed — " + x.error : x.ms + " ms via " + x.backend;
  }
  voiceGearBtn.addEventListener("click", voiceSheetOpen);
  voiceDoneBtn.addEventListener("click", voiceSheetClose);
  voiceOnToggle.addEventListener("change", function () {
    voicePrime();   // a tap on the checkbox is a gesture too
    if (voiceOnToggle.checked) {
      if (current) voiceTurnOn(current, false); else voiceOnToggle.checked = false;
    } else {
      if (voice.session) voiceSetFlag(voice.session, false);
      voiceOff();
    }
  });
  voiceHandsEl.addEventListener("change", function () {
    voice.prefs.handsFree = voiceHandsEl.checked;
    voicePersist();
  });
  voicePttEl.addEventListener("change", function () {
    voice.prefs.ptt = voicePttEl.value === "hold" ? "hold" : "toggle";
    voicePersist();
    if (voice.state === "idle") voiceSetState("idle", voiceTalkHint());
  });
  voiceRepliesEl.addEventListener("change", function () {
    var was = voice.prefs.replies;
    voice.prefs.replies = voiceRepliesPref(voiceRepliesEl.value);
    voicePersist();
    // Every prompt anywhere asks for a Say line while Everything is on.
    if ((was === "all") !== (voice.prefs.replies === "all")) voiceSayAllPost(voice.prefs.replies === "all");
    // Everything listens to every session, voice on or off.
    if (voice.prefs.replies === "all") voiceEventsOpen();
    else if (voice.state === "off") voiceEventsClose();
  });
  // Listen for "MindFlock": this tap makes (or resumes) the AudioContext the
  // standby records through and the chime plays through.
  voiceWakeEl.addEventListener("change", function () {
    voice.prefs.wake = voiceWakeEl.checked;
    voicePrime();   // a tap on the checkbox is a gesture too: the context now
    voiceWake_.note = "";
    voiceWake_.needTap = false;
    voicePersist();
    if (!voice.prefs.wake) voiceError("");
    voiceWakeSync();
  });
  voiceRateEl.addEventListener("input", function () {
    voice.prefs.rate = Number(voiceRateEl.value) || 1;
    voiceRateVal.textContent = voice.prefs.rate.toFixed(1) + "×";
    voicePersist();
  });
  voiceVoiceEl.addEventListener("change", function () {
    voice.prefs.voiceName = voiceVoiceEl.value;
    voicePersist();
  });
  voiceLoadPrefs();
  voiceLoadCfg();   // before the first tap: voicePrime needs to know about the server
  voiceEventsOpen();   // Everything: every session's turns, voice on or off
  voiceBtn.dataset.state = "off";

  // =========================================================================
  // ASK — a session is asking you something
  // =========================================================================
  // The rows' `asking` — {kind: "dialog" | "question", text, ts, dialog_id,
  // parsed, source}, null when nothing is asked; from an older server, a
  // clarify activity — feed one sheet in the middle of the screen: whose ask
  // (the Sessions sheet's number and name), what kind (Permission /
  // Question / Plan approval), the question and its command, and the way
  // to answer it right there:
  //   a parsed dialog   → its options; a tap posts /answer {keys,
  //                       dialog_id, by: "user"} (409 dialog_changed →
  //                       the new prompt; dialog_answered → "Already
  //                       answered"), as the desktop's answer strip does
  //   a select question → Claude's AskUserQuestion, step by step in the
  //   (kind "select")     SAME sheet (askSel*): the step row like Claude's
  //                       tabs, single-select buttons (a tap posts the
  //                       digit and reads the next step), multi-select
  //                       checkboxes toggled here + Next (the digits whose
  //                       state differs from the server's `checked`, then
  //                       nav.next, in one /answer), Back (nav.prev), the
  //                       review's answers + Submit answers / Cancel, "Type
  //                       something" (digit, then {text, keys: ["Enter"]})
  //                       and "Chat about this" (digit, then /send). The
  //                       question over (204) is answered; a 409
  //                       dialog_changed is just the next step. The
  //                       review's Submit answers (primary) / Cancel and
  //                       a multi-select's Next / Submit (its `button`)
  //                       sit in the step's row, Back on the left
  //   an unparsed one   → "Open the session": only the terminal answers it
  //   a prose question  → a field + Send: POST /send {text, dialog_safe}
  //                       (409 in_dialog → the dialog it is showing now)
  // The mic answers the one shown: a dialog by the voice dialog mode
  // (words only, approve_dialogs respected), a question by dictating into
  // the field with the radio words (vbox); while it reads, the mic cuts
  // the reading and listens (barge-in). A hand on the sheet (askHandsOn)
  // stops what is being read about the ask shown and voice's listen for
  // its answer, and for ASK_HAND_QUIET_MS the step it moves to is not read
  // aloud. Later — the button, a tap
  // outside, Back, Esc — puts the one shown off: a question is dismissed
  // on the server (POST /asking/dismiss), a dialog snoozed here by its id.
  //
  // It opens by itself (notifications.ask_popup, on unless Settings turn
  // it off) for a DIALOG it has not shown yet — never one put off or
  // answered, and not while you are typing into that session's compose box
  // (it waits for you). A question in prose never opens it by itself (the
  // word "question" in a reply is not a prompt): it keeps its Question
  // chip and notification, and opens on its row's tap or notification.
  // Several dialogs → one sheet, "1 of N", oldest first (a prose question
  // is in the stepper only while it is the one shown). Voice reading a
  // select question shows it here too. A push's tap (/m?s=<title>&voice=1) opens it
  // for that session whatever the setting, with "Hear it and answer" (a page
  // cannot speak before a tap). The count of asking sessions is in the
  // tab's title; a new ask buzzes a visible phone and, hidden, raises a
  // notification when this browser allows one ("Notify me when a session
  // asks", in the Sessions sheet).
  var askEl = document.getElementById("asking-sheet");
  var askNumEl = document.getElementById("asking-num");
  var askNameEl = document.getElementById("asking-name");
  var askNavEl = document.getElementById("asking-nav");
  var askStepEl = document.getElementById("asking-step");
  var askPrevBtn = document.getElementById("asking-prev");
  var askNextBtn = document.getElementById("asking-next");
  var askKindEl = document.getElementById("asking-kind");
  var askQuestionEl = document.getElementById("asking-question");
  var askCommandEl = document.getElementById("asking-command");
  var askNoteEl = document.getElementById("asking-note");
  var askOptionsEl = document.getElementById("asking-options");
  var askStepsEl = document.getElementById("asking-steps");
  var askReviewEl = document.getElementById("asking-review");
  var askSelEl = document.getElementById("asking-sel");
  var askSelPrevBtn = document.getElementById("asking-sel-prev");
  var askSelNextBtn = document.getElementById("asking-sel-next");
  var askSelActsEl = document.getElementById("asking-sel-acts");
  var askReplyEl = document.getElementById("asking-reply");
  var askTextEl = document.getElementById("asking-text");
  var askVoiceEl = document.getElementById("asking-voice");
  var askHearBtn = document.getElementById("asking-hear");
  var askErrEl = document.getElementById("asking-error");
  var askNotifyInline = document.getElementById("asking-notify");
  var askMicBtn = document.getElementById("asking-mic");
  var askLaterBtn = document.getElementById("asking-later");
  var askOpenBtn = document.getElementById("asking-open");
  var askSendBtn = document.getElementById("asking-send");
  var askNotifyBtn = document.getElementById("ask-notify-btn");
  var askNotifyNote = document.getElementById("ask-notify-note");

  var ASK_HIDDEN_POLL_MS = 10000;        // the hidden page's poll while notifications are on
  var ASK_HINT_MS = 15000;               // an event's question waits this long for the rows to agree
  var ASK_NOTIFY_STORE = "cs_ask_notify"; // localStorage: "off" mutes this page's notifications
  var ASK_SW = "/mobile-sw.js";          // the service worker Android Chrome needs to show one
  var ASK_BUZZ = [70, 60, 70];           // a new ask on a visible phone (the listening cue is one 30 ms tick)
  var ASK_HAND_QUIET_MS = 3000;          // a step (or ask) the sheet moves to this soon after a tap is not read aloud
  var ask = {
    popup: true,         // notifications.ask_popup (GET /api/notify/ntfy)
    ntfyActive: false,   // ntfy pushes already reach a phone (said next to the notify button)
    all: [],             // every ask the rows (and events) show, in sheet order
    open: false,
    entry: null,         // the ask shown (a copy: it stays readable when the row lets go of it)
    origin: "",          // what opened it: auto | row | push | link | notification
    gone: false,         // the shown ask is not asking any more
    dialog: null,        // GET /dialog for the shown dialog: null = not read yet, false = nothing (204)
    dialogSeq: 0,
    // A select question's state here: the step it is for (id:step), the
    // multi-select boxes as ticked here, the free option being filled in
    // ("Type something" / "Chat about this"), the dialog ids it went
    // through (all answered when it is over), and whether voice made the
    // last move (then voice reads the next step).
    sel: { id: "", checked: {}, input: null, ids: {}, voice: false },
    busy: false,
    note: "",
    handAt: 0,           // the last tap / keystroke on the sheet (askHandsOn)
    hints: {},           // title → {text, ts, kind, at}: a session.question ahead of the rows
    later: {}, answered: {}, seen: {}, notified: {}, spoken: {},
    polls: 0,
    lastPollAt: 0,
    deepDone: !deepLink, // the ?s= link's sheet: opened (or given up on)
    deepTries: 0,
    pollAsked: 0,        // when an event last fetched the rows (one per second at most)
    history: false, popping: false,
    notifyErr: "",
    notifications: [],   // Diagnostics / tests: what was raised
    baseTitle: document.title,
  };
  window.mfAsk = ask;
  ask.notifyOn = (function () { try { return localStorage.getItem(ASK_NOTIFY_STORE) !== "off"; } catch (e) { return true; } })();

  // --- what is asking --------------------------------------------------------
  function askHas(o, k) { return !!o && Object.prototype.hasOwnProperty.call(o, k); }
  function askOptKeys(opt) {
    return opt.keys && opt.keys.length ? opt.keys.map(String) : [String(opt.key)];
  }
  // A row's ask, or null. The server's `asking` is the truth once the row
  // carries the field (even null); a session.question event stands in for
  // it until the next poll; an older server's clarify is a dialog.
  function askEntryOf(inst) {
    var t = inst.title, a = inst.asking, h = ask.hints[t];
    if (a && (a.kind === "dialog" || a.kind === "question")) {
      var ts = Number(a.ts) || 0;
      return { title: t, kind: a.kind, text: String(a.text || ""), ts: ts, dialogId: String(a.dialog_id || ""),
               parsed: a.parsed !== false, source: String(a.source || ""),
               key: a.kind === "dialog" ? "d:" + t + ":" + (a.dialog_id || ts) : "q:" + t + ":" + ts };
    }
    if (h && (!askHas(inst, "asking") || h.at > ask.lastPollAt) && Date.now() - h.at < ASK_HINT_MS && inst.activity !== "working")
      return { title: t, kind: "question", text: h.text, ts: h.ts, dialogId: "", parsed: false, source: "",
               key: "q:" + t + ":" + h.ts };
    if (!askHas(inst, "asking") && inst.activity === "clarify") {
      var since = Number(inst.activity_since) || 0;
      return { title: t, kind: "dialog", text: "", ts: since, dialogId: "", parsed: true, source: "",
               key: "c:" + t + ":" + since };
    }
    return null;
  }
  function askOrder(a, b) {
    if (a.kind !== b.kind) return a.kind === "dialog" ? -1 : 1;
    return (a.ts - b.ts) || (rowOrder.indexOf(a.title) - rowOrder.indexOf(b.title));
  }
  // The sheet's list: the dialogs — a question in prose only while it is
  // the one shown (first, then) —, answered ones out, put-off ones out (except the one on
  // screen, so the counter does not jump under the thumb).
  function askList() {
    var list = ask.all.filter(function (e) {
      var shown = !!ask.entry && e.key === ask.entry.key;
      return !ask.answered[e.key] && (e.kind === "dialog" || shown) && (!ask.later[e.key] || shown);
    });
    // The prose question you opened leads: the dialogs come after it.
    list.sort(function (a, b) { return (a.kind === "question" ? 0 : 1) - (b.kind === "question" ? 0 : 1); });
    return list;
  }
  // The line an ask reads as: the question, else the dialog's command,
  // else what it is.
  function askLineText(e) {
    var d = ask.entry && ask.entry.key === e.key && ask.dialog ? ask.dialog : null;
    var q = String((d && d.question) || e.text || "").trim();
    if (q) return q;
    return e.kind === "dialog" ? "It's waiting on a prompt." : "It asked you something.";
  }
  function askRowText(inst) {
    var a = inst && inst.asking;
    return a && (a.kind === "dialog" || a.kind === "question") && a.text ? String(a.text) : "";
  }
  // Typing into that session's compose box: its ask waits.
  function askTypingInto(title) {
    return document.activeElement === composeEl && title === current && !!composeEl.value.trim();
  }

  // --- the rows → asks -------------------------------------------------------
  function askOnPoll() {
    ask.polls++;
    ask.lastPollAt = Date.now();
    askRefresh();
  }
  function askRefresh() {
    var all = [], live = {};
    instances.forEach(function (inst) {
      var e = askEntryOf(inst);
      if (e) { all.push(e); live[e.key] = true; }
    });
    all.sort(askOrder);
    ask.all = all;
    [ask.later, ask.answered, ask.seen, ask.notified, ask.spoken].forEach(function (m) {
      for (var k in m) if (!live[k] && !(ask.entry && k === ask.entry.key)) delete m[k];
    });
    for (var t in ask.hints) if (Date.now() - ask.hints[t].at >= ASK_HINT_MS) delete ask.hints[t];
    askTitleCount(all.length);
    var first = ask.polls <= 1;   // the asks already there at load: shown, not announced
    all.forEach(function (e) {
      if (ask.answered[e.key] || ask.later[e.key] || ask.notified[e.key]) return;
      ask.notified[e.key] = true;
      // The sheet stepping through this session's select question: its
      // next step is not news.
      if (first || (ask.open && ask.entry && ask.entry.title === e.title && ask.entry.kind === e.kind)) return;
      askAnnounce(e);
      askMaybeSpeak(e);
    });
    if (ask.open) { askRelist(); return; }
    if (!ask.deepDone) { askDeepLink(); if (ask.open) return; }
    if (!ask.popup) return;
    var fresh = all.filter(function (e) {
      return e.kind === "dialog" && !ask.answered[e.key] && !ask.later[e.key] && !ask.seen[e.key] && !askTypingInto(e.title);
    });
    if (fresh.length) askShow(fresh[0], "auto");
  }
  // The push's /m?s=<title>[&voice=1]: that session's sheet, whatever the
  // pop-up setting or a Later — the tap asked for it. A link that does not
  // resolve yet (the device prefix waits on /api/remote/hello) gets a few
  // polls; an ask answered meanwhile opens nothing.
  function askDeepLink() {
    var titles = instances.map(function (x) { return x.title; });
    var t = resolveDeepLink(deepLink, titles);
    if (!t) { if (++ask.deepTries > 3) ask.deepDone = true; return; }
    ask.deepDone = true;
    var e = ask.all.filter(function (x) { return x.title === t; })[0];
    if (e) askShow(e, voice.deepLink ? "push" : "link");
  }
  function askTitleCount(n) {
    var want = n ? "(" + n + ") " + ask.baseTitle : ask.baseTitle;
    if (document.title !== want) document.title = want;
  }
  // A new ask: a buzz on a visible phone (not while the mic listens — its
  // own tick says "listening"), a notification on a hidden one.
  function askAnnounce(e) {
    if (document.visibilityState !== "visible") { askNotify(e); return; }
    if (voiceIsListening() || !navigator.vibrate) return;
    try { navigator.vibrate(ASK_BUZZ); } catch (x) {}
  }

  // --- events (the voice socket, while voice is on) ----------------------------
  // session.question: a hint until the rows agree (they carry it on the
  // next poll, fetched now); clarify, or an idle / turn_ended with a
  // question: fetch the rows now rather than at the next 4 s tick.
  function askOnEvent(f) {
    if (!f || typeof f !== "object") return;
    var d = f.data || {};
    var title = String(f.session || d.title || f.title || "");
    if (!title) return;
    var ev = f.event, q = null;
    if (ev === "session.question") q = { text: d.text || f.text, ts: d.ts || f.ts, kind: d.kind || f.kind };
    else if ((ev === "session.activity_changed" && f.new === "idle") || ev === "session.turn_ended") {
      if (d.question) q = typeof d.question === "object" ? d.question : { text: d.question, ts: f.ts };
    } else if (ev === "session.activity_changed" && f.new === "working") {
      delete ask.hints[title];
      return;
    } else if (!(ev === "session.activity_changed" && f.new === "clarify")) {
      return;
    }
    if (q && q.text && (q.kind || "question") === "question")
      ask.hints[title] = { text: String(q.text), ts: Number(q.ts) || Number(f.ts) || 0, at: Date.now() };
    askRefresh();
    if (Date.now() - ask.pollAsked > 1000) { ask.pollAsked = Date.now(); poll(); }
  }

  // --- the sheet ------------------------------------------------------------------
  function askShow(e, origin) {
    var wasOpen = ask.open;
    ask.open = true;
    ask.origin = origin;
    askSetEntry(e);
    askRelist();
    if (!wasOpen) {
      openSheet(askEl);
      askHistoryPush();
      askEl.querySelector(".sheet-box").scrollTop = 0;
    }
    askMaybeSpeak(ask.entry);
  }
  // Show this ask (fresh state for it): its dialog is read, the field kept
  // only for the same session's question.
  function askSetEntry(e) {
    var prev = ask.entry;
    if (prev && prev.key === e.key) return;
    askVoiceRelease();
    // Voice listening for the dialog stepped away from: not its answer now.
    if (prev && voice.session === prev.title && voice.state === "dialog") voiceIdle(nameOf(prev.title) + " is waiting on you");
    ask.entry = { title: e.title, kind: e.kind, text: e.text, ts: e.ts, dialogId: e.dialogId,
                  parsed: e.parsed, source: e.source, key: e.key };
    ask.gone = false;
    ask.dialog = null;
    ask.sel = { id: "", checked: {}, input: null, ids: {}, voice: false };
    ask.busy = false;
    ask.note = "";
    askErr("");
    if (!prev || prev.title !== e.title) askTextEl.value = "";
    if (e.kind === "dialog") askFetchDialog();
  }
  // The list moved under the open sheet (a poll, an answer): the ask shown
  // stays if it can; gone, the next one; none left, the sheet closes — unless
  // words are waiting in the field (they still go, as a prompt).
  function askRelist() {
    if (!ask.open) return;
    var list = askList();
    list.forEach(function (x) { ask.seen[x.key] = true; });
    var e = ask.entry;
    var at = e ? askIndex(list, e.key) : -1;
    if (e && at < 0) {
      // The same session still asking the same kind (a dialog's id arrived)
      // is the same ask.
      var same = list.filter(function (x) { return x.title === e.title && x.kind === e.kind; })[0];
      if (same && !ask.answered[e.key]) { ask.entry.key = same.key; ask.entry.dialogId = same.dialogId; at = askIndex(list, same.key); }
    }
    // A select question the rows have let go of (they lag a step, or it
    // was answered elsewhere): the screen says which.
    if (e && at < 0 && askIsSelect(ask.dialog) && !ask.answered[e.key]) {
      if (!ask.busy) askSelRefetch("");
      return;
    }
    if (e && at < 0 && !ask.answered[e.key] && e.kind === "question" && askTextEl.value.trim()) {
      ask.gone = true;
      askRender();
      return;
    }
    if (e && at < 0) {
      if (!list.length) { askClose(); return; }
      askSetEntry(list[0]);
      askMaybeSpeak(ask.entry);
    } else if (!e) {
      if (!list.length) { askClose(); return; }
      askSetEntry(list[0]);
    } else {
      ask.entry.text = list[at].text || ask.entry.text;
    }
    askRender();
  }
  function askIndex(list, key) {
    for (var i = 0; i < list.length; i++) if (list[i].key === key) return i;
    return -1;
  }
  function askStep(delta) {
    var list = askList();
    if (!ask.entry || list.length < 2) return;
    var at = askIndex(list, ask.entry.key);
    var next = list[(Math.max(0, at) + delta + list.length) % list.length];
    askSetEntry(next);
    askRender();
  }
  function askClose() {
    askVoiceRelease();
    ask.open = false;
    ask.entry = null;
    ask.dialog = null;
    ask.busy = false;
    closeSheet(askEl);
    askHistoryPop();
  }
  // Answered (or sent): out of the list, the next ask if there is one.
  function askDone(e, msg) {
    ask.answered[e.key] = true;
    if (msg) flashStatus(nameOf(e.title) + ": " + msg);
    if (!ask.open || !ask.entry || ask.entry.key !== e.key) return;
    askAdvance();
  }
  function askAdvance() {
    var list = askList().filter(function (x) { return !ask.entry || x.key !== ask.entry.key; });
    if (!list.length) { askClose(); return; }
    askSetEntry(list[0]);
    askRender();
    askMaybeSpeak(ask.entry);
  }
  function askErr(msg) {
    askErrEl.textContent = msg || "";
    askErrEl.classList.toggle("hidden", !msg);
  }

  // GET /dialog for the dialog shown: its options (or 204: nothing any
  // more; or, from the newer server, a question in prose after all).
  function askFetchDialog(note) {
    var e = ask.entry;
    if (!e) return;
    var seq = ++ask.dialogSeq;
    ask.dialog = null;
    if (note) ask.note = note;
    fetch("/api/instances/" + encodeURIComponent(e.title) + "/dialog?quiet=1")
      .then(function (r) { return r.status === 204 ? false : r.ok ? r.json() : false; })
      .catch(function () { return false; })
      .then(function (d) {
        if (seq !== ask.dialogSeq || !ask.entry || ask.entry.key !== e.key) return;
        if (d && d.kind === "question") {
          ask.entry.kind = "question";
          ask.entry.text = String(d.question || ask.entry.text || "");
          ask.dialog = null;
        } else {
          ask.dialog = d && typeof d === "object" ? d : false;
        }
        askSelSync();
        askRender();
      });
    askRender();
  }

  // What kind of ask, in a word: a dialog with yes/no options or a command
  // is a Permission, a heading about a plan is Plan approval, the rest are
  // Questions.
  function askKindWord(e, d) {
    if (e.kind !== "dialog" || askIsSelect(d)) return "Question";
    if (d && /\bplan\b/i.test(String(d.heading || ""))) return "Plan approval";
    var opts = (d && d.options) || [];
    if ((d && d.command) || opts.some(function (o) { return o.kind === "yes" || o.kind === "no" || o.kind === "always"; }))
      return "Permission";
    return "Question";
  }
  function askRender() {
    var e = ask.entry;
    if (!e) return;
    var d = e.kind === "dialog" ? ask.dialog : null;
    var list = askList();
    var at = askIndex(list, e.key);
    var n = rowOrder.indexOf(e.title);
    askNumEl.textContent = n >= 0 ? String(n + 1) : "";
    askNameEl.textContent = nameOf(e.title);
    askNavEl.classList.toggle("hidden", list.length < 2 && !(ask.gone && list.length));
    askStepEl.textContent = (at >= 0 ? at + 1 : "–") + " of " + list.length;
    askKindEl.textContent = askKindWord(e, d);
    var src = String((d && d.source) || e.source || "");
    if (src) {
      var s = document.createElement("span");
      s.className = "asking-source";
      s.textContent = " · from " + src;
      askKindEl.appendChild(s);
    }
    var reading = e.kind === "dialog" && ask.dialog === null;
    askQuestionEl.textContent = reading && !e.text ? "Reading the prompt…" : askLineText(e);
    askCommandEl.textContent = d && d.command ? String(d.command) : "";
    askCommandEl.classList.toggle("hidden", !(d && d.command));
    // The way to answer.
    askOptionsEl.innerHTML = "";
    var parsed = !!(d && d.parsed && (d.options || []).length);
    var nothing = e.kind === "dialog" && ask.dialog === false;
    var note = ask.note;
    if (ask.gone) note = "It isn't asking any more — Send still sends this to it.";
    else if (nothing && !note) note = "It isn't waiting on that prompt any more.";
    else if (d && !parsed) note = note || "This prompt needs the terminal — open the session to answer it.";
    var sel = parsed && askIsSelect(d);
    askStepsEl.classList.toggle("hidden", !sel);
    askReviewEl.classList.toggle("hidden", !sel);
    askSelEl.classList.toggle("hidden", !sel);
    if (sel) {
      askRenderSelect(d);
    } else if (parsed) {
      d.options.forEach(function (opt, i) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "asking-opt";
        b.dataset.kind = opt.kind || "other";
        b.disabled = ask.busy;
        var k = document.createElement("span");
        k.className = "asking-opt-key";
        k.textContent = String(opt.key || i + 1);
        var l = document.createElement("span");
        l.className = "asking-opt-label";
        l.textContent = String(opt.label || opt.key || "");
        b.appendChild(k);
        b.appendChild(l);
        b.addEventListener("click", function () { askHandsOn(); askAnswer(opt); });
        askOptionsEl.appendChild(b);
      });
    }
    askNoteEl.textContent = note || "";
    askNoteEl.classList.toggle("hidden", !note);
    var free = sel && ask.sel.input;
    var prose = e.kind === "question" || !!free;
    askReplyEl.classList.toggle("hidden", !prose);
    askTextEl.placeholder = free ? (free.kind === "chat" ? "What to tell it" : "Your answer") : "Your answer";
    askSendBtn.disabled = ask.busy;
    askOpenBtn.classList.toggle("primary", !!(d && !parsed) || nothing);
    askLaterBtn.textContent = nothing || (ask.gone && !askTextEl.value.trim()) ? "Close" : "Later";
    askMicBtn.classList.toggle("hidden", !(parsed || prose));
    // A push's sheet: the page has not been tapped yet, so it cannot speak —
    // one big button hears it and listens for the answer.
    var hear = ask.origin === "push" && voice.state === "off" && (parsed || prose);
    askHearBtn.textContent = "🔊 Hear it and answer by voice";
    askHearBtn.classList.toggle("hidden", !hear);
    askNotifyInline.classList.toggle("hidden", !(askNotifySupported() && Notification.permission === "default"));
    askVoiceMirror();
  }
  askPrevBtn.addEventListener("click", function () { askHandsOn(); askStep(-1); });
  askNextBtn.addEventListener("click", function () { askHandsOn(); askStep(1); });

  // --- Claude's select questions ---------------------------------------------------
  // GET /dialog's kind "select" (AskUserQuestion): {id, heading, question,
  // multi, steps: [{label, done, submit?}], step, options: [{key, label,
  // desc, kind: other | text | chat | yes | no, checked, keys}], review?:
  // [{question, answer}], nav: {next, prev, submit}}. Answered a step at a
  // time in this sheet; it never closes mid-way.
  function askIsSelect(d) {
    return !!d && typeof d === "object" && d.kind === "select" && !!d.parsed && !!(d.options || []).length;
  }
  function askSelReview(d) {
    var st = (d && d.steps || [])[Number(d && d.step) || 0];
    return !!(st && st.submit);
  }
  // The local state follows the step shown: a new step starts from the
  // server's boxes; the same one (a toggle keeps the id) keeps the taps.
  function askSelSync() {
    var d = ask.dialog;
    if (!askIsSelect(d)) return;
    ask.sel.ids[String(d.id)] = true;
    var id = String(d.id) + ":" + (Number(d.step) || 0);
    if (ask.sel.id === id) return;
    if (ask.sel.input) askTextEl.value = "";
    ask.sel.id = id;
    ask.sel.checked = {};
    ask.sel.input = null;
    (d.options || []).forEach(function (o) { if (o.checked === true) ask.sel.checked[o.key] = true; });
  }
  function askSelFree(opt) { return opt.kind === "text" || opt.kind === "chat"; }
  function askRenderSelect(d) {
    var steps = d.steps || [], at = Number(d.step) || 0, review = askSelReview(d);
    var multi = !!d.multi && !review;
    askStepsEl.innerHTML = "";
    steps.forEach(function (st, i) {
      var t = document.createElement("span");
      t.className = "asking-step-tab" + (st.done ? " done" : "") + (i === at ? " active" : "");
      t.textContent = String(st.label || (st.submit ? "Submit" : "Question " + (i + 1)));
      if (i === at) t.setAttribute("aria-current", "step");
      askStepsEl.appendChild(t);
    });
    askStepsEl.classList.toggle("hidden", steps.length < 2);
    askReviewEl.innerHTML = "";
    (review ? d.review || [] : []).forEach(function (r) {
      var dt = document.createElement("dt");
      dt.textContent = String(r.question || "");
      var dd = document.createElement("dd");
      dd.textContent = String(r.answer || "—");
      askReviewEl.appendChild(dt);
      askReviewEl.appendChild(dd);
    });
    askReviewEl.classList.toggle("hidden", !askReviewEl.firstChild);
    // The review: its answers, then ONE primary Submit answers with a
    // plain Cancel beside it in the step's row — buttons, not numbered rows.
    askSelActsEl.innerHTML = "";
    if (review) askSelActs(d.options).forEach(function (b) { askSelActsEl.appendChild(b); });
    askSelActsEl.classList.toggle("hidden", !review);
    (review ? [] : d.options).forEach(function (opt, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "asking-opt";
      b.dataset.kind = opt.kind || "other";
      b.dataset.key = String(opt.key);
      b.disabled = ask.busy;
      var box = multi && !askSelFree(opt);
      if (box) {
        b.setAttribute("role", "checkbox");
        b.setAttribute("aria-checked", ask.sel.checked[opt.key] ? "true" : "false");
        var c = document.createElement("span");
        c.className = "asking-opt-box";
        c.textContent = ask.sel.checked[opt.key] ? "✓" : "";
        b.appendChild(c);
      } else if (askSelFree(opt)) {
        b.setAttribute("aria-pressed", ask.sel.input && ask.sel.input.key === opt.key ? "true" : "false");
      }
      var k = document.createElement("span");
      k.className = "asking-opt-key";
      k.textContent = String(opt.key || i + 1);
      var body = document.createElement("span");
      body.className = "asking-opt-body";
      var l = document.createElement("span");
      l.className = "asking-opt-label";
      l.textContent = String(opt.label || opt.key || "");
      body.appendChild(l);
      if (opt.desc) {
        var ds = document.createElement("span");
        ds.className = "asking-opt-desc";
        ds.textContent = String(opt.desc);
        body.appendChild(ds);
      }
      b.appendChild(k);
      b.appendChild(body);
      b.addEventListener("click", function () { askHandsOn(); askSelTap(opt); });
      askOptionsEl.appendChild(b);
    });
    var nav = d.nav || {};
    askSelPrevBtn.classList.toggle("hidden", !(at > 0 && (nav.prev || []).length));
    askSelNextBtn.classList.toggle("hidden", !multi);
    // The multi-select's own row says what it does: "Next" or "Submit".
    askSelNextBtn.textContent = String(d.button || "Next") + " ›";
    // Beside Cancel + Submit answers, Back is just its arrow (a narrow phone fits all three).
    askSelPrevBtn.textContent = review ? "‹" : "‹ Back";
    askSelPrevBtn.setAttribute("aria-label", "Back");
    askSelPrevBtn.disabled = askSelNextBtn.disabled = ask.busy;
    askSelEl.classList.toggle("hidden", askSelPrevBtn.classList.contains("hidden") && askSelNextBtn.classList.contains("hidden") && !review);
  }
  // The review's options as the row's buttons: the yes one (Submit
  // answers; else the first) primary and last, on the right.
  function askSelActs(options) {
    var primary = options.filter(function (o) { return o.kind === "yes"; })[0] || options[0];
    return options.filter(function (o) { return o !== primary; }).concat(primary ? [primary] : []).map(function (opt) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "asking-act" + (opt === primary ? " asking-act-primary" : "");
      b.dataset.kind = opt.kind || "other";
      b.dataset.key = String(opt.key);
      b.disabled = ask.busy;
      b.textContent = stripMarkdown(String(opt.label || opt.key || ""));
      b.addEventListener("click", function () { askHandsOn(); askSelTap(opt); });
      return b;
    });
  }
  // A tap: a free option opens the field (its words go with it); a
  // multi-select box toggles HERE (Next sends them); anything else — a
  // single-select pick, the review's Submit answers / Cancel — is posted.
  function askSelTap(opt) {
    var d = ask.dialog;
    if (!askIsSelect(d) || ask.busy) return;
    if (askSelFree(opt)) {
      ask.sel.input = opt;
      askRender();
      try { askTextEl.focus(); } catch (e) {}
      return;
    }
    if (d.multi && !askSelReview(d)) {
      ask.sel.checked[opt.key] = !ask.sel.checked[opt.key];
      askRender();
      return;
    }
    ask.sel.input = null;
    askSelPost(askOptKeys(opt));
  }
  // Next: the boxes ticked here that differ from the server's, then
  // nav.next — one /answer, so the toggles and the move land together.
  // toReview: nav.next as many times as it takes to reach the review tab
  // ("submit" said before it).
  function askSelNext(toReview) {
    var d = ask.dialog;
    if (!askIsSelect(d)) return;
    var keys = [];
    if (d.multi && !askSelReview(d)) {
      d.options.forEach(function (o) {
        if ((o.checked === true || o.checked === false) && !!ask.sel.checked[o.key] !== o.checked) keys = keys.concat(askOptKeys(o));
      });
    }
    var nav = d.nav || {}, next = (nav.next || []).length ? nav.next.map(String) : ["Right"];
    var steps = d.steps || [], at = Number(d.step) || 0, n = 1;
    if (toReview) for (var i = at + 1; i < steps.length; i++) if (steps[i].submit) { n = Math.max(1, i - at); break; }
    for (var j = 0; j < n; j++) keys = keys.concat(next);
    askSelPost(keys);
  }
  function askSelPrev() {
    var d = ask.dialog;
    if (!askIsSelect(d)) return;
    var nav = d.nav || {};
    askSelPost((nav.prev || []).length ? nav.prev.map(String) : ["Left"]);
  }
  askSelPrevBtn.addEventListener("click", function () { askHandsOn(); askSelPrev(); });
  askSelNextBtn.addEventListener("click", function () { askHandsOn(); askSelNext(); });
  // POST /answer → {r, j}; the network failing reads as status 0.
  function askPostAnswer(title, body) {
    return fetch("/api/instances/" + encodeURIComponent(title) + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j || {} }; });
    }).catch(function (err) {
      return { r: { ok: false, status: 0 }, j: { error: String((err && err.message) || err) } };
    });
  }
  // One move in a select question, pinned to the step shown; then the step
  // after it is read into the same sheet. A 409 dialog_changed (or a
  // dialog_answered: a second move inside the server's hold) is the flow
  // moving on, not an error.
  function askSelPost(keys, text) {
    var e = ask.entry, d = ask.dialog;
    if (!e || !askIsSelect(d) || ask.busy) return;
    ask.busy = true;
    askErr("");
    if (!ask.sel.voice) askSelVoiceStop(e.title);
    askRender();
    var body = { keys: keys, dialog_id: String(d.id), by: "user" };
    if (text) body.text = String(text);
    askPostAnswer(e.title, body).then(function (x) {
      if (ask.entry !== e) return;
      var moved = x.r.status === 409 && (x.j.dialog_changed || x.j.dialog_answered);
      if (x.r.ok || moved) { askSelRefetch(x.r.ok ? String(d.id) : ""); return; }
      ask.busy = false;
      askErr("Couldn't answer: " + (x.j.error || "HTTP " + x.r.status));
      askRender();
      askSelVoiceFailed(e);
    });
  }
  // After a move: read the dialog again. The same id a moment later is the
  // screen not redrawn yet (a few more tries); nothing (204) is the
  // question over — answered; a select is the next step, here; anything
  // else is shown as it is.
  function askSelRefetch(was, tries) {
    var e = ask.entry;
    if (!e) return;
    var seq = ++ask.dialogSeq;
    ask.busy = true;
    fetch("/api/instances/" + encodeURIComponent(e.title) + "/dialog?quiet=1")
      .then(function (r) { return r.status === 204 ? false : r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        if (seq !== ask.dialogSeq || ask.entry !== e) return;
        tries = tries || 0;
        if (((d && was && String(d.id) === was) || d === null) && tries < 4) {
          setTimeout(function () { if (seq === ask.dialogSeq && ask.entry === e) askSelRefetch(was, tries + 1); }, 350);
          return;
        }
        ask.busy = false;
        ask.note = "";
        if (d === null) {
          askErr("Couldn't read the next step — try again, or open the session.");
          askRender();
          askSelVoiceFailed(e);
          return;
        }
        if (!d || typeof d !== "object") { askSelDone(e, "answered"); return; }
        if (d.kind === "question") {
          ask.entry.kind = "question";
          ask.entry.text = String(d.question || ask.entry.text || "");
          ask.dialog = null;
          askRender();
          return;
        }
        ask.dialog = d;
        askSelSync();
        askRender();
        // A step reached by hand is on the screen: the poll never reads it later.
        if (!askMayNarrate() && voice.session === e.title) voice.dialogKey = String(d.id);
        if (askIsSelect(d)) askSelVoiceNext(e, d);
      });
    askRender();
  }
  // The select question is over: every step it showed is answered (the
  // rows may still carry one for a poll or two), and the sheet moves on.
  function askSelDone(e, msg) {
    for (var id in ask.sel.ids) ask.answered["d:" + e.title + ":" + id] = true;
    var byVoice = ask.sel.voice;
    askSelVoiceNext(e, null);
    if (!byVoice) askVoiceAnsweredHere(e.title);
    askDone(e, msg);
  }
  // "Type something": its keys, then the words + Enter, both pinned to the
  // step (its id holds while the row is typed into). "Chat about this":
  // its keys, then the words
  // as a prompt (/send, dialog-safe — a dialog still up a moment after the
  // digit is waited out).
  function askSelSendText() {
    var e = ask.entry, d = ask.dialog, opt = ask.sel.input;
    var text = askTextEl.value.trim();
    if (!e || !askIsSelect(d) || !opt || ask.busy) return;
    if (!text) { askTextEl.focus(); return; }
    ask.busy = true;
    askErr("");
    if (!ask.sel.voice) askSelVoiceStop(e.title);
    askRender();
    askPostAnswer(e.title, { keys: askOptKeys(opt), dialog_id: String(d.id), by: "user" }).then(function (x) {
      if (ask.entry !== e) return;
      if (!x.r.ok) {
        if (x.r.status === 409 && x.j.dialog_changed) { askSelRefetch(""); return; }
        ask.busy = false;
        askErr("Couldn't answer: " + (x.j.error || "HTTP " + x.r.status));
        askRender();
        askSelVoiceFailed(e);
        return;
      }
      if (opt.kind === "chat") { askSelChat(e, text, 0); return; }
      askPostAnswer(e.title, { text: text, keys: ["Enter"], dialog_id: String(d.id), by: "user" }).then(function (y) {
        if (ask.entry !== e) return;
        if (!y.r.ok) {
          ask.busy = false;
          askErr("Couldn't type it: " + (y.j.error || "HTTP " + y.r.status));
          askRender();
          askSelVoiceFailed(e);
          return;
        }
        askTextEl.value = "";
        ask.sel.input = null;
        askSelRefetch(String(d.id));
      });
    });
  }
  function askSelChat(e, text, tries) {
    fetch("/api/instances/" + encodeURIComponent(e.title) + "/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(askSendBody(text)),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { r: r, j: j || {} }; });
    }, function (err) {
      return { r: { ok: false, status: 0 }, j: { error: String((err && err.message) || err) } };
    }).then(function (x) {
      if (ask.entry !== e) return;
      if (x.r.status === 409 && x.j.in_dialog && tries < 4) {
        setTimeout(function () { if (ask.entry === e) askSelChat(e, text, tries + 1); }, 600);
        return;
      }
      ask.busy = false;
      if (!x.r.ok) {
        askErr("Couldn't send: " + (x.j.error || "HTTP " + x.r.status));
        askRender();
        askSelVoiceFailed(e);
        return;
      }
      askTextEl.value = "";
      ask.sel.input = null;
      askSelDone(e, x.j.queued ? "queued" : "sent");
    });
  }

  // Open the sheet for one session (a row's tap, a notification's): its
  // ask, put off or not; nothing when it is not asking.
  function askOpenFor(title, origin) {
    var e = ask.all.filter(function (x) { return x.title === title && !ask.answered[x.key]; })[0];
    if (!e) return false;
    delete ask.later[e.key];
    askShow(e, origin);
    return true;
  }

  // --- answering ------------------------------------------------------------------
  function askAnswer(opt) {
    var e = ask.entry, d = ask.dialog;
    if (!e || !d || ask.busy) return;
    ask.busy = true;
    askErr("");
    askRender();
    fetch("/api/instances/" + encodeURIComponent(e.title) + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys: askOptKeys(opt), dialog_id: String(d.id), by: "user" }),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        ask.busy = false;
        if (!ask.entry || ask.entry.key !== e.key) return;
        if (r.ok) {
          askVoiceAnsweredHere(e.title);
          askDone(e, "answered " + stripMarkdown(String(opt.label || opt.key)));
          return;
        }
        if (r.status === 409 && j && j.dialog_changed) { askFetchDialog("It changed — this is the prompt it shows now."); return; }
        if (r.status === 409 && j && j.dialog_answered) {
          ask.answered[e.key] = true;
          ask.dialog = false;
          ask.note = "Already answered.";
          askRender();
          return;
        }
        if (r.status === 409 && j && j.no_dialog) { askFetchDialog(); return; }
        askErr((j && j.error) || ("Couldn't answer (HTTP " + r.status + ")"));
        askRender();
      });
    }).catch(function (err) {
      ask.busy = false;
      askErr("Couldn't answer: " + String((err && err.message) || err));
      askRender();
    });
  }
  // Send: the field's words, as a prompt. With voice on for this session
  // the voice loop sends it (its reply is then read out, and words that came
  // from the mic ask for a Say line); otherwise straight to /send.
  // /send's body for words from the sheet: a Say line asked for with
  // Everything (every prompt the phone sends, typed too).
  function askSendBody(text) {
    var body = { text: text, dialog_safe: true };
    if (voice.prefs.replies === "all") body.say = true;
    return body;
  }
  function askSend() {
    var e = ask.entry;
    var text = askTextEl.value.trim();
    if (!e || ask.busy) return;
    if (ask.sel.input && askIsSelect(ask.dialog)) { askSelSendText(); return; }
    if (!text) { askTextEl.focus(); return; }
    if (voice.state !== "off" && voice.session === e.title && voice.state !== "sending") {
      voice.box = askTextEl;
      voiceSend(text);
      return;
    }
    ask.busy = true;
    askErr("");
    askRender();
    fetch("/api/instances/" + encodeURIComponent(e.title) + "/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(askSendBody(text)),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        ask.busy = false;
        if (r.ok) {
          if (ask.entry && ask.entry.key === e.key) askTextEl.value = "";
          askDone(e, j && j.queued ? "queued" : "sent");
          if (ask.gone) askClose();
          return;
        }
        if (r.status === 409 && j && j.in_dialog) { askInDialog(e.title); return; }
        var why = (j && j.error) || ("HTTP " + r.status);
        if (r.status === 409 && j && j.waking) why = nameOf(e.title) + " is waking up — try again in a moment.";
        else if (r.status === 409 && j && j.paused) why = nameOf(e.title) + " is paused — resume it first.";
        askErr("Couldn't send: " + why);
        askRender();
      });
    }).catch(function (err) {
      ask.busy = false;
      askErr("Couldn't send: " + String((err && err.message) || err));
      askRender();
    });
  }
  // /send said a dialog is up (409 in_dialog): the sheet shows THAT, the
  // words kept in the field for after.
  function askInDialog(title) {
    var e = ask.entry;
    if (!ask.open || !e || e.title !== title) return;
    e.kind = "dialog";
    ask.gone = false;
    askFetchDialog("It's showing a prompt now — answer that first; your words are kept.");
  }
  function askLater(close) {
    var e = ask.entry;
    if (!e) { askClose(); return; }
    ask.later[e.key] = true;
    if (e.kind === "question" && !ask.gone && !ask.answered[e.key]) {
      fetch("/api/instances/" + encodeURIComponent(e.title) + "/asking/dismiss", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      }).catch(function () {});
    }
    // Voice listening for this one stops: "not now" is not "yes".
    if (voice.session === e.title && voice.state === "dialog") voiceIdle(nameOf(e.title) + " is waiting on you");
    if (close) { askClose(); return; }
    askAdvance();
  }
  function askOpenSession() {
    var e = ask.entry;
    if (!e) return;
    closeSheet(sessionsSheet);
    askClose();
    select(e.title);
  }
  askLaterBtn.addEventListener("click", function () { askHandsOn(); askLater(false); });
  askOpenBtn.addEventListener("click", askOpenSession);
  askSendBtn.addEventListener("click", function () { askHandsOn(); askSend(); });
  askTextEl.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); askHandsOn(); askSend(); }
  });
  // Typing is a hand on the sheet (the mic's words land without an input event).
  askTextEl.addEventListener("input", askHandsOn);
  askEl.addEventListener("click", function (ev) { if (ev.target === askEl) askLater(true); });
  document.addEventListener("keydown", function (ev) { if (ask.open && ev.key === "Escape") askLater(true); });
  // Back = Later: opening pushes a history entry, Back pops it; closing any
  // other way pops it ourselves (and ignores the popstate that follows).
  function askHistoryPush() {
    if (ask.history || !ask.open) return;
    // A Back of our own still on its way would pop this entry: after it.
    if (ask.popping) { setTimeout(askHistoryPush, 80); return; }
    try { history.pushState({ mfAsk: true }, ""); ask.history = true; } catch (e) {}
  }
  function askHistoryPop() {
    if (!ask.history) return;
    ask.history = false;
    ask.popping = true;
    try { history.back(); } catch (e) { ask.popping = false; }
  }
  window.addEventListener("popstate", function () {
    if (ask.popping) { ask.popping = false; return; }
    if (!ask.history) return;
    ask.history = false;
    if (ask.open) askLater(true);
  });

  // --- the voice side ---------------------------------------------------------------
  // A hand on the sheet — an option, a box, Back / Next / Submit, Send,
  // Later, the stepper, a keystroke in the field: what is being read about
  // the ask shown stops and its queued lines go (other sessions' lines
  // stay), and voice stops listening for its answer (words in the field
  // stay). For ASK_HAND_QUIET_MS after it the sheet reads nothing new
  // aloud: the step it moves to is on the screen, under the hand.
  function askHandsOn() {
    var e = ask.entry;
    ask.handAt = Date.now();
    ask.sel.voice = false;
    if (!e) return;
    voiceSpeechStopFor(e.title);
    if (voice.state === "off" || voice.session !== e.title) return;
    if (voice.state === "dialog" || voice.state === "awaiting" || voice.state === "speaking" ||
        (voiceIsListening() && voice.box === askTextEl)) {
      voiceStopForCommand();
      voiceListen_.gen++;   // a route answer on its way is for nobody now
      voiceIdle("");
    }
  }
  // Pure: may the sheet read a step aloud now, `handAt` the last hand on it?
  function askHandQuiet(handAt, now, quietMs) {
    return !handAt || now - handAt >= quietMs;
  }
  function askMayNarrate() { return askHandQuiet(ask.handAt, Date.now(), ASK_HAND_QUIET_MS); }
  // The voice loop answered (or met) this session's dialog: the sheet follows.
  function askAnsweredByVoice(title, dialogId) {
    // A select question shown here: that was a step — the next one shows.
    if (ask.open && ask.entry && ask.entry.title === title && askIsSelect(ask.dialog)) { askSelRefetch(""); return; }
    ask.all.forEach(function (x) {
      if (x.title === title && x.kind === "dialog" && (!dialogId || !x.dialogId || x.dialogId === String(dialogId))) ask.answered[x.key] = true;
    });
    if (ask.open && ask.entry && ask.entry.title === title && ask.entry.kind === "dialog") askDone(ask.entry, "");
  }
  function askDialogChanged(title) {
    if (ask.open && ask.entry && ask.entry.title === title && askIsSelect(ask.dialog)) { askSelRefetch(""); return; }
    if (ask.open && ask.entry && ask.entry.title === title && ask.entry.kind === "dialog")
      askFetchDialog("It changed — this is the prompt it shows now.");
  }
  function askDialogAnswered(title) {
    if (!ask.open || !ask.entry || ask.entry.title !== title) return;
    ask.answered[ask.entry.key] = true;
    ask.dialog = false;
    ask.note = "Already answered.";
    askRender();
  }
  // The field's words went out through the voice loop.
  function askSentByVoice(title) {
    ask.all.forEach(function (x) { if (x.title === title && x.kind === "question") ask.answered[x.key] = true; });
    if (ask.open && ask.entry && ask.entry.title === title) {
      var gone = ask.gone;
      askDone(ask.entry, "sent");
      if (gone && ask.open) askClose();
    }
  }
  // A tap answered the dialog voice was listening for: voice stops
  // listening and waits for the agent, as after a spoken answer.
  function askVoiceAnsweredHere(title) {
    if (voice.state === "off" || voice.session !== title) return;
    voice.dialog = null;
    voice.answeredAt = Date.now();
    if (voiceIsListening() || voice.state === "speaking" || voice.state === "idle") {
      voiceStopForCommand();
      voiceSpeechStop(false);
      voiceSetState("working", "");
    }
  }
  // The question the sheet reads aloud, name first — the retry's line too.
  function askVoiceLine() {
    var e = ask.entry;
    return ask.open && e && e.kind === "question" ? nameOf(e.title) + " asks: " + askLineText(e) : "";
  }
  // The sheet is not the dictation target any more (closed, another ask):
  // a listen that was filling its field stops, the words dropped with it.
  function askVoiceRelease() {
    if (voice.box !== askTextEl) return;
    voice.box = null;
    if (voiceIsListening()) {
      voiceListen_.until = 0;
      voiceListen_.overAfter = false;
      voiceListen_.gen++;
      voiceIdle();
    }
  }
  // The strip is under the backdrop: its state and caption, mirrored on
  // the sheet while voice is on for the session shown; the mic wears it.
  function askVoiceMirror() {
    if (!askEl || !ask.open || !ask.entry) return;
    var mine = voice.state !== "off" && voice.session === ask.entry.title;
    var st8 = mine ? voiceBtnState(voice.state) : "idle";
    askMicBtn.dataset.state = st8;
    askMicBtn.setAttribute("aria-label", mine && voiceIsListening() ? "Stop listening" : "Answer by voice");
    var cap = mine ? String(voiceCaptionEl.textContent || "") : "";
    var word = mine ? VOICE_WORDS[voice.state] || "" : "";
    var show = mine && voice.state !== "idle" && (word || cap);
    askVoiceEl.innerHTML = "";
    if (show) {
      var b = document.createElement("b");
      b.textContent = word;
      askVoiceEl.appendChild(b);
      if (cap) askVoiceEl.appendChild(document.createTextNode((word ? " — " : "") + cap));
    }
    askVoiceEl.classList.toggle("hidden", !show);
  }
  // Voice on for the shown session, selecting it first (voice follows the
  // selection). False when this phone cannot listen.
  function askVoiceOn(title) {
    if (current !== title) select(title);
    if (voice.state === "off") voiceTurnOn(title, false);
    else if (voice.session !== title) voiceMoveTo(title);
    if (!voice.stt.available()) {
      if (IOS) voiceShowSafariBanner();
      ask.note = "No speech recognition here" + (IOS ? " — open the page in a Safari tab." : ".");
      askRender();
      return false;
    }
    return true;
  }
  // The sheet's mic: listening for this ask → stop (the words stay);
  // else listen for its answer — a dialog's words, or a question's
  // dictation into the field.
  function askMicTap() {
    voicePrime();   // FIRST, synchronously: the gesture that lets the page speak
    var e = ask.entry;
    if (!e) return;
    if (voice.state !== "off" && voice.session === e.title && voiceIsListening()) { voiceListenOver(); return; }
    if (voice.state === "sending") return;
    voiceHearHide();
    voiceBargeIn(e.title);   // reading it (or anything): cut first, then the mic
    if (!askVoiceOn(e.title)) return;
    askListenFor(e, false);
  }
  // `read`: say it first (name first), as the push's button does; a tap
  // on the mic just listens — the question is on the screen.
  function askListenFor(e, read) {
    ask.spoken[e.key] = true;
    var d = ask.dialog;
    // A select question's free option is open: the mic fills its field.
    if (e.kind === "dialog" && ask.sel.input && askIsSelect(d) && !read) {
      voice.box = askTextEl;
      voiceListen_.reprompted = true;
      voiceListen("prompt", VOICE_ANSWER_MS);
      return;
    }
    if (e.kind === "dialog") {
      if (!d || !d.parsed || !(d.options || []).length) {
        if (read) voiceSayThenIdle(nameOf(e.title) + " is waiting on you: open the screen.");
        return;
      }
      voice.dialog = d;
      voice.dialogKey = String(d.id);   // the poll never reads it again
      voice.dialogTries = 0;
      if (read) { voiceReadDialog(d); return; }   // reads, then listens (or "Answer it on the screen.")
      if (!voice.cfg.loaded || !voice.cfg.approve_dialogs) {
        ask.note = "Answering prompts by voice is off (Settings → Your phone) — tap an answer.";
        askRender();
        return;
      }
      voiceListen("dialog", VOICE_DIALOG_MS);
      return;
    }
    voice.box = askTextEl;
    voiceListen_.reprompted = false;
    if (read) {
      speakLine(askVoiceLine()).then(function (ok) {
        if (ok && voice.box === askTextEl) voiceListenAfter("prompt", VOICE_ANSWER_MS);
      });
      return;
    }
    voiceListen("prompt", VOICE_ANSWER_MS);
  }
  askMicBtn.addEventListener("click", askMicTap);
  askHearBtn.addEventListener("click", function () {
    voicePrime();   // in the gesture, first
    var e = ask.entry;
    if (!e) return;
    voiceHearHide();   // the strip's "Tap to hear" is this
    if (!askVoiceOn(e.title)) return;
    askRender();
    askListenFor(e, true);
  });
  // Spoken replies on (and voice on, or Everything): a new ask from
  // ANOTHER session is read, name first, once, in the one speech queue —
  // listening for it would change what "yes" means mid-conversation, so
  // its answer is the sheet's mic. Voice's own session reads its own (the
  // dialog read-out, the Say line): never twice; with Everything a
  // question in prose is its turn's Say line, read already.
  function askMaybeSpeak(e) {
    var mode = voice.prefs.replies;
    if (!e || ask.spoken[e.key] || mode === "off" || (voice.state === "off" && mode !== "all")) return;
    if (voice.state !== "off" && e.title === voice.session) return;
    if (mode === "all" && e.kind === "question") return;
    ask.spoken[e.key] = true;
    if (!askMayNarrate()) return;   // the sheet moved under a hand: it is on the screen
    speakLine(nameOf(e.title) + " asks: " + askLineText(e), true, e.title);
  }

  // --- a select question by voice ---------------------------------------------------
  // Voice reading a select question shows it in the sheet (whatever the
  // pop-up setting: voice is reading it), so a pick, Next and the free
  // field have one place to live; the dialog voice read is what it shows.
  function askShowSelect(title, d) {
    if (ask.open && ask.entry && ask.entry.title === title && ask.entry.kind === "dialog") {
      if (!ask.busy) { ask.dialogSeq++; ask.dialog = d; askSelSync(); askRender(); }
      return;
    }
    var e = ask.all.filter(function (x) { return x.title === title && x.kind === "dialog" && !ask.answered[x.key]; })[0];
    if (!e) {
      e = { title: title, kind: "dialog", text: String(d.question || ""), ts: Date.now() / 1000, dialogId: String(d.id),
            parsed: true, source: "", key: "d:" + title + ":" + d.id };
      ask.all.push(e);
      ask.all.sort(askOrder);
    }
    delete ask.later[e.key];
    ask.spoken[e.key] = true;
    askShow(e, "voice");
    ask.dialogSeq++;   // the read askSetEntry started is older than this one
    ask.dialog = d;
    askSelSync();
    askRender();
  }
  function askSelVoiceOn() {
    return ask.open && !!ask.entry && ask.entry.title === voice.session && askIsSelect(ask.dialog);
  }
  // A phrase while voice listens on the select question shown: "next",
  // "back", "submit" are its buttons ("submit" on the review is Submit
  // answers); "type something" / "chat about this" open the field and
  // dictate into it; "what is it asking" reads it again; anything else
  // picks an option — the router first (it knows the dialog), then the
  // number or yes / no, then an option's own words.
  var ASK_SEL_NEXT = /^(?:next|next question|go on|continue|done|that's it|thats it)$/;
  var ASK_SEL_BACK = /^(?:back|go back|previous|previous question)$/;
  var ASK_SEL_TEXT = /^(?:type something|type it|something else|other)$/;
  var ASK_SEL_CHAT = /^(?:chat about (?:this|it|that)|chat)$/;
  function askSelVoice(text) {
    var e = ask.entry, d = ask.dialog;
    var p = voiceDictParse(text);
    var said = p.kind === "submit" && !p.text ? "submit" : p.kind === "disregard" ? "" : p.text;
    var t = voiceNorm(said);
    if (!t) { voiceListenContinue(); return; }
    clearTimeout(voiceListen_.timer);
    voice.stt.stop();
    var review = askSelReview(d);
    var c = voiceCommand(said);
    if (c && (c.kind === "question" || c.kind === "repeat")) { voiceReadDialog(d); return; }
    if (ASK_SEL_BACK.test(t)) { ask.sel.voice = true; voiceSetState("sending", "Back"); askSelPrev(); return; }
    if (ASK_SEL_NEXT.test(t) || (t === "submit" && !review)) {
      ask.sel.voice = true;
      voiceSetState("sending", "Next");
      askSelNext(t === "submit" || t === "done");
      return;
    }
    var free = ASK_SEL_TEXT.test(t) ? "text" : ASK_SEL_CHAT.test(t) ? "chat" : "";
    if (free) {
      var fo = d.options.filter(function (o) { return o.kind === free; })[0];
      if (fo) { askSelVoicePick(fo); return; }
    }
    if (t === "submit" && review) {
      var yes = d.options.filter(function (o) { return o.kind === "yes"; })[0];
      if (yes) { askSelVoicePick(yes); return; }
    }
    voiceSetState("sending", said);
    voiceRoute(said, e.title).then(function (res) {
      if (voice.state !== "sending" || ask.entry !== e || ask.dialog !== d) return;
      // A free-text answer the router made of it: its keys and words, as said.
      if (res && res.kind === "answer" && res.text && (res.keys || []).length) {
        ask.sel.voice = true;
        askSelPost(res.keys.map(String), res.text);
        return;
      }
      var opt = askSelRouted(res, d.options) || voiceParseAnswer(said, d.options) || askSelByWords(said, d.options);
      if (!opt) { voice.dialog = d; voiceDialogRetry(); return; }
      askSelVoicePick(opt);
    });
  }
  // The router's answer as one of the step's options (by its keys, or its
  // number), or null.
  function askSelRouted(res, options) {
    if (!res || res.kind !== "answer") return null;
    var keys = (res.keys || []).map(String).join(" ");
    var hit = options.filter(function (o) { return keys && askOptKeys(o).join(" ") === keys; })[0];
    if (hit) return hit;
    var n = Number(res.number) || 0;
    return n ? options.filter(function (o) { return String(o.key) === String(n); })[0] || null : null;
  }
  // An option named by its own words ("postgres", "the redis one"): the
  // one whose label is said whole, when exactly one is.
  function askSelByWords(text, options) {
    var t = " " + voiceNorm(text) + " ";
    var hits = options.filter(function (o) {
      var l = voiceNorm(stripMarkdown(o.label || ""));
      return l.length >= 3 && t.indexOf(" " + l + " ") >= 0;
    });
    return hits.length === 1 ? hits[0] : null;
  }
  // What a spoken pick does: a free option opens the field and listens
  // into it ("submit" sends it); a multi-select box toggles here and says
  // so; anything else is posted, and the next step read.
  function askSelVoicePick(opt) {
    var d = ask.dialog;
    if (askSelFree(opt)) {
      ask.sel.input = opt;
      askRender();
      voice.box = askTextEl;
      voiceListen_.reprompted = true;
      voiceListen("prompt", VOICE_ANSWER_MS);
      return;
    }
    if (d.multi && !askSelReview(d)) {
      ask.sel.checked[opt.key] = !ask.sel.checked[opt.key];
      askRender();
      var word = stripMarkdown(opt.label || opt.key);
      speakLine((ask.sel.checked[opt.key] ? "Ticked " : "Unticked ") + word + ".").then(function (ok) {
        if (ok) voiceListenAfter("dialog", VOICE_DIALOG_MS);
      });
      return;
    }
    ask.sel.voice = true;
    voiceSetState("sending", stripMarkdown(opt.label || opt.key));
    askSelPost(askOptKeys(opt));
  }
  // After a move voice made: the next step is read (and listened for);
  // the question over is "Answered." and the agent works on.
  function askSelVoiceNext(e, d) {
    if (!ask.sel.voice) return;
    ask.sel.voice = false;
    if (voice.state === "off" || voice.session !== e.title || !askMayNarrate()) return;
    if (!d) {
      voice.dialog = null;
      voice.answeredAt = Date.now();
      speakLine("Answered.").then(function (ok) { if (ok) voiceSetState("working", ""); });
      return;
    }
    voice.dialog = d;
    voice.dialogKey = String(d.id);
    voice.dialogTries = 0;
    voiceReadDialog(d);
  }
  function askSelVoiceFailed(e) {
    if (!ask.sel.voice) return;
    ask.sel.voice = false;
    if (voice.state !== "off" && voice.session === e.title) voiceSayThenIdle("Couldn't answer that.");
  }
  // A tap moved the question on: voice listening for the old step stops.
  function askSelVoiceStop(title) {
    if (voice.session !== title || voice.state !== "dialog") return;
    voiceStopForCommand();
    voiceIdle("");
  }

  // --- the pop-up setting -------------------------------------------------------------
  // notifications.ask_popup on GET /api/notify/ntfy (on when absent: an
  // older server); re-read when the page comes back.
  function askLoadSettings() {
    fetch("/api/notify/ntfy")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || typeof j !== "object") return;
        if (typeof j.ask_popup === "boolean") ask.popup = j.ask_popup;
        ask.ntfyActive = !!j.active;
      })
      .catch(function () {});
  }
  askLoadSettings();
  document.addEventListener("visibilitychange", function () { if (!document.hidden) askLoadSettings(); });

  // --- notifications ------------------------------------------------------------------
  // The Web Notification API while this page is open but hidden. Android
  // Chrome shows one only through a service worker (`new Notification`
  // throws there), so the page registers ASK_SW when allowed and uses its
  // registration, else the constructor (desktop browsers, Firefox). An
  // iPhone lets only a Home Screen web app notify, and a closed page
  // notifies nobody — the server's ntfy pushes cover both.
  function askNotifySupported() { return typeof window.Notification === "function"; }
  function askNotifyAllowed() {
    return askNotifySupported() && Notification.permission === "granted" && ask.notifyOn;
  }
  function askSwReg() {
    if (!("serviceWorker" in navigator)) return Promise.resolve(null);
    return navigator.serviceWorker.getRegistration().catch(function () { return null; });
  }
  function askSwRegister() {
    if (!("serviceWorker" in navigator) || !askNotifyAllowed()) return;
    navigator.serviceWorker.register(ASK_SW, { scope: "/m" }).catch(function (err) {
      ask.swError = String((err && err.message) || err);
    });
  }
  function askNotify(e) {
    if (!askNotifyAllowed()) return;
    var title = nameOf(e.title) + (e.kind === "dialog" ? " is waiting on you" : " asked you a question");
    var opts = { body: askLineText(e).slice(0, 240), tag: "mf-ask-" + e.title, renotify: true,
                 icon: "/apple-touch-icon.png", data: { title: e.title, key: e.key } };
    ask.notifications.push({ title: title, body: opts.body, session: e.title, key: e.key });
    if (ask.notifications.length > 20) ask.notifications.shift();
    askSwReg().then(function (reg) {
      if (reg && reg.showNotification) return reg.showNotification(title, opts);
      var n = new Notification(title, opts);
      n.onclick = function () {
        try { window.focus(); } catch (x) {}
        try { n.close(); } catch (x) {}
        askOpenFor(e.title, "notification");
      };
    }).catch(function (err) {
      ask.notifyErr = String((err && err.message) || err);
      askNotifyRender();
    });
  }
  // The service worker's notificationclick focuses this page and says which.
  if ("serviceWorker" in navigator && navigator.serviceWorker.addEventListener) {
    navigator.serviceWorker.addEventListener("message", function (ev) {
      var m = ev.data;
      if (m && m.type === "mf-ask-open" && m.title) askOpenFor(String(m.title), "notification");
    });
  }
  // The Sessions sheet's line: a button while there is something to ask
  // for or toggle, and what this browser can and cannot do.
  function askNotifyRender() {
    var btn = askNotifyBtn, note = "", label = "";
    if (!askNotifySupported()) {
      note = IOS && !STANDALONE
        ? "On an iPhone only a Home Screen web app can notify: Share → Add to Home Screen, then open it from there. ntfy pushes reach you either way."
        : IOS ? "This iOS version cannot notify from a web app (iOS 16.4 or newer can). ntfy pushes reach you either way."
        : "This browser cannot notify from a page. ntfy pushes reach you either way.";
    } else if (Notification.permission === "denied") {
      note = "Notifications are blocked for this site — allow them in the browser's site settings.";
    } else if (Notification.permission === "default") {
      label = "Notify me when a session asks";
      note = "While this page is open in the background.";
    } else {
      label = ask.notifyOn ? "Notifications on — tap to turn off" : "Notifications off — tap to turn on";
      note = ask.notifyOn ? "Shown while this page is open in the background; a closed page relies on ntfy pushes." : "";
      if (ask.notifyErr) note = "Couldn't show one: " + ask.notifyErr + (ANDROID ? " — Android needs the page's service worker." : "");
    }
    if (ask.ntfyActive && label) note += (note ? " " : "") + "ntfy already pushes asks; both would arrive.";
    btn.textContent = label;
    btn.classList.toggle("hidden", !label);
    askNotifyNote.textContent = note;
  }
  function askNotifyTap() {
    if (!askNotifySupported()) { askNotifyRender(); return; }
    if (Notification.permission === "granted") {
      ask.notifyOn = !ask.notifyOn;
      try { localStorage.setItem(ASK_NOTIFY_STORE, ask.notifyOn ? "on" : "off"); } catch (e) {}
      ask.notifyErr = "";
      askNotifyRender();
      askSwRegister();
      return;
    }
    var asked = false;
    var done = function () {
      if (asked) return;
      asked = true;
      ask.notifyOn = true;
      try { localStorage.setItem(ASK_NOTIFY_STORE, "on"); } catch (e) {}
      askNotifyRender();
      if (ask.open) askRender();
      askSwRegister();
    };
    try {
      var p = Notification.requestPermission(done);   // old Safari: the callback; the rest: a promise
      if (p && p.then) p.then(done, done);
    } catch (e) { done(); }
  }
  askNotifyBtn.addEventListener("click", askNotifyTap);
  askNotifyInline.addEventListener("click", askNotifyTap);
  askSwRegister();

  // ---- Update banner: a newer release, here or on your other devices ----
  // The same facts Settings shows (/api/update/check, /api/fleet), checked on
  // load and every 30 minutes (the server caches the release lookup). Your
  // other devices behind → "Update all" (the fleet rollout, one at a time,
  // this device last); only this one → "Update". Either answers 403 to a
  // caller who isn't this device's owner, which the banner says.
  var updBanner = document.getElementById("upd-banner");
  var updText = document.getElementById("upd-text");
  var updBtn = document.getElementById("upd-btn");
  var updAction = null;
  function cmpVer(a, b) {
    var pa = String(a || "").replace(/^v/i, "").split(".");
    var pb = String(b || "").replace(/^v/i, "").split(".");
    for (var i = 0; i < Math.max(pa.length, pb.length); i++) {
      var d = (parseInt(pa[i], 10) || 0) - (parseInt(pb[i], 10) || 0);
      if (d) return d;
    }
    return 0;
  }
  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      return r.ok ? r.json() : null;
    });
  }
  function showUpdate(text, label, action) {
    updText.textContent = text;
    updBtn.textContent = label;
    updBtn.disabled = !action;
    updBtn.classList.toggle("hidden", !label);
    updAction = action;
    updBanner.classList.remove("hidden");
  }
  function waitForNewBuild() {
    var t0 = Date.now();
    var t = setInterval(function () {
      if (Date.now() - t0 > 180000) { clearInterval(t); return; }
      getJSON("/api/update/check").then(function (c) {
        if (c && !c.restart_pending && c.state !== "started") { clearInterval(t); location.reload(); }
      }).catch(function () { /* restarting */ });
    }, 2500);
  }
  function startUpdate(url, body, busyText) {
    updBtn.disabled = true;
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 403) throw new Error("only this device's owner can update it — sign in with its token");
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        showUpdate(busyText, "", null);
        if (url === "/api/update/start") waitForNewBuild();
      });
    }).catch(function (err) {
      showUpdate("Couldn't update: " + err.message, "Update", updAction);
    });
  }
  function checkUpdate() {
    getJSON("/api/update/check").then(function (c) {
      if (!c) return;
      if (c.restart_pending) {
        showUpdate("Installed — restarting onto it…", "", null);
        waitForNewBuild();
        return;
      }
      if (!c.latest) return;
      return getJSON("/api/fleet").catch(function () { return null; }).then(function (f) {
        var behind = ((f && f.in_fleet && f.members) || []).filter(function (m) {
          return !m.self && m.reachable && m.version && cmpVer(c.latest, m.version) > 0;
        });
        var here = !!c.available;
        if (f && f.update && f.update.state === "running") {
          showUpdate("Updating your devices to v" + c.latest + "…", "", null);
        } else if (behind.length) {
          var n = behind.length + (here ? 1 : 0);
          showUpdate(
            "MindFlock v" + c.latest + " is out — " + n + " of your devices " + (n === 1 ? "is" : "are") + " behind",
            "Update all",
            function () { startUpdate("/api/fleet/update", { tag: "v" + c.latest }, "Updating your devices one at a time…"); }
          );
        } else if (here && !c.blocked) {
          showUpdate("MindFlock v" + c.latest + " is out", "Update", function () {
            startUpdate("/api/update/start", {}, "Updating — this page reloads when it's back…");
          });
        } else {
          updBanner.classList.add("hidden");
        }
      });
    }).catch(function () { /* offline: try again later */ });
  }
  updBtn.addEventListener("click", function () { if (updAction) updAction(); });
  checkUpdate();
  setInterval(checkUpdate, 30 * 60 * 1000);

  applyViewport();
  poll();
  // Visibility-aware cadence: 4s while visible, ~30s while the tab is hidden
  // (a backgrounded phone browser doesn't need a fresh session list) — 10s
  // while page notifications are on, or an ask would wait half a minute.
  // Self-rescheduling timeout re-reads the cadence every cycle; returning to
  // the tab polls immediately and re-arms at 4s.
  var _pollTimer = null;
  function schedulePoll() {
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(function () { poll(); schedulePoll(); },
      document.hidden ? (askNotifyAllowed() ? ASK_HIDDEN_POLL_MS : 30000) : 4000);
  }
  schedulePoll();
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
    schedulePoll();
  });
})();
