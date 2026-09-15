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

  var pickerEl = document.getElementById("picker");
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
  // server is still provisioning it (status "loading"). See select() and
  // attachWhenReady().
  var awaitingReady = "";

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
      setStatus("starting " + title + " — setting up the workspace…");
    } else {
      connect();
    }
    updateActions();
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
  function activityOf(title) {
    for (var i = 0; i < instances.length; i++)
      if (instances[i].title === title) return instances[i].activity || "offline";
    return "offline";
  }

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

  function renderPicker() {
    var prev = current;
    pickerEl.innerHTML = "";
    if (!instances.length) {
      var o = document.createElement("option");
      o.textContent = "no sessions";
      pickerEl.appendChild(o);
      pickerEl.disabled = true;
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
    pickerEl.disabled = false;
    // O1: attention-first picker — sessions that need the human sort to the
    // top with a marker (🔵 answer me · 🔴 broken · 🟡 checks failing ·
    // 🟢 ready for PR), so triage works from a phone without a sidebar.
    instances.slice().sort(function (a, b) {
      return attnRank(a) - attnRank(b);
    }).forEach(function (inst) {
      var o = document.createElement("option");
      o.value = inst.title;
      var label = attnMark(inst) + inst.title;
      if (inst.repo) label += "  ·  " + inst.repo;
      o.textContent = label;
      pickerEl.appendChild(o);
    });
    // Keep the current selection if it still exists; else pick a sensible one.
    var titles = instances.map(function (i) { return i.title; });
    var want = prev && titles.indexOf(prev) >= 0 ? prev : null;
    if (!want) {
      var saved = null;
      try { saved = localStorage.getItem("cs_mobile_last"); } catch (e) {}
      var qs = new URLSearchParams(location.search).get("s");
      want = (qs && titles.indexOf(qs) >= 0) ? qs
           : (saved && titles.indexOf(saved) >= 0) ? saved
           : titles[0];
    }
    pickerEl.value = want;
    if (want !== current) select(want);
  }

  function updateDot() {
    var a = current ? activityOf(current) : "offline";
    dotEl.className = "dot " + a;
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
  // server's {error} message (same contract app.js's api() speaks).
  function postAction(action, body) {
    return fetch("/api/instances/" + encodeURIComponent(current) + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error((j && j.error) || "request failed (" + r.status + ")");
        return j;
      });
    });
  }

  function doPush(force) {
    if (!current) return;
    flashStatus("pushing…");
    postAction("push-branch", force ? { force: true } : {})
      .then(function () { flashStatus("pushed"); setTimeout(poll, 800); })
      .catch(function (err) {
        // O3 soft gate: checks haven't passed — offer an explicit override,
        // exactly like the desktop pushSession flow.
        if (err.message === "checks haven't passed for this commit") {
          if (confirm("Checks haven't passed for this commit (see the 🟡 marker).\nPush anyway?")) {
            doPush(true);
            return;
          }
          setStatus("");
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
  var PR_REMEDY = "add a GitHub token in Intake → Pull requests, or install the GitHub CLI";

  function doMakePr() {
    if (!current) return;
    flashStatus("opening PR…");
    postAction("make-pr", {})
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
        setTimeout(poll, 800);
      })
      .catch(function (err) { flashStatus("make PR failed: " + err.message); });
  }

  function doMerge() {
    if (!current) return;
    if (!confirm("Merge this branch's PR into the base branch?")) return;
    flashStatus("merging…");
    postAction("merge-pr", {})
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
      .catch(function (err) { flashStatus("merge failed: " + err.message); });
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
    postAction("commit", { message: msg })
      .then(function () { flashStatus("commit started — watch the shell"); setTimeout(poll, 800); })
      .catch(function (err) { flashStatus("commit failed: " + err.message); });
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

  // O1: attention rank (lower = more urgent) + its picker marker. Mirrors the
  // desktop intake priorities (app.js attentionItems).
  function attnRank(inst) {
    if (inst.status === "paused") return 9;
    if (inst.activity === "clarify") return 0;
    if (inst.stage === "interrupt") return 1;
    if (inst.setup && inst.setup.state === "failed") return 1;
    if (inst.check && inst.check.state === "failed" && !inst.check.stale) return 2;
    if (inst.stage === "pushed") return 3;
    return 9;
  }
  function attnMark(inst) {
    var r = attnRank(inst);
    return r === 0 ? "🔵 " : r === 1 ? "🔴 " : r === 2 ? "🟡 " : r === 3 ? "🟢 " : "";
  }

  function poll() {
    fetch("/api/instances")
      .then(function (r) { return r.json(); })
      .then(function (list) {
        instances = Array.isArray(list) ? list : [];
        // Re-render when the title set OR any attention state changes, so
        // markers move without a session being added/removed.
        var sig = instances.map(function (i) { return attnRank(i) + i.title; }).join("|");
        if (sig !== poll._sig) { poll._sig = sig; renderPicker(); }
        // After renderPicker, before the dot: a session created from the "+"
        // sheet is selected the tick it shows up, and both of those read the
        // selection.
        claimPending();
        // ...and after claimPending, because the session it just selected is
        // exactly the one that is still provisioning.
        attachWhenReady();
        updateDot();
        updateActions();
      })
      .catch(function () { /* transient; next tick retries */ });
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

  function openNewSheet() {
    // A fresh sheet every time. A half-reviewed plan left over from the last
    // opening is indistinguishable from this one's, and the folder it names is
    // almost certainly not the folder this sentence is about.
    newPlan = null;
    newTextEl.value = "";
    setPlanBusy(false, "Continue");
    // The Start button too. Nothing the create's answer does resets it when
    // that answer is stale, so a sheet dismissed mid-create used to reopen with
    // "Starting…" disabled forever (startSession returns at
    // `if (startBusy) return;`) and no way out but reloading the page.
    resetStartBtn();
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
    setPlanBusy(false, "Continue");
    resetStartBtn();
    newSheet.classList.add("hidden");
    newPlan = null;
    newError("");
  }

  function setPlanBusy(on, label) {
    planBusy = on;
    newGoBtn.disabled = on;
    newGoBtn.textContent = label;
  }

  function resetStartBtn() {
    startBusy = false;
    newStartBtn.disabled = false;
    newStartBtn.textContent = "Start session";
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
  // Start session, under a heading that already asks the question). Everything
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
    var seq = ++planSeq;
    newError("");
    setPlanBusy(true, "Reading…");
    planSlowTimer = setTimeout(function () {
      if (seq === planSeq) newGoBtn.textContent = "Still reading…";
    }, NEW_SLOW_MS);
    fetch("/api/session-plan", {
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
    setPlanBusy(false, "Continue");
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
    fetch("/api/repos/suggest")
      .then(function (r) { return r.json(); })
      .then(function (j) {
        suggestHome = (j && j.home) || "";
        var rows = (j && j.suggestions) || [];
        newListEl.innerHTML = "";
        if (!rows.length) {
          newListEl.appendChild(folderNote(
            "No folders found on this machine — make one on the desktop first."));
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
    fetch("/api/instances", {
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
        // tracked and selected when it lands.
        pendingNew = title;
        pendingNewUntil = Date.now() + PENDING_NEW_MS;
        flashStatus("starting " + title + "…");
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
        // the signature), so the option exists to be selected.
        pickerEl.value = title;
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
    awaitingReady = "";
    connect();                 // onopen clears the provisioning line
  }

  document.getElementById("new-btn").addEventListener("click", openNewSheet);
  emptyEl.addEventListener("click", openNewSheet);
  newGoBtn.addEventListener("click", describeIt);
  document.getElementById("new-cancel").addEventListener("click", closeNewSheet);
  newStartBtn.addEventListener("click", startSession);
  document.getElementById("new-back").addEventListener("click", function () {
    harvestPlan();
    newStep(1);
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

  // --- wiring --------------------------------------------------------------
  pickerEl.addEventListener("change", function () { select(pickerEl.value); });

  document.querySelectorAll(".tab").forEach(function (b) {
    b.addEventListener("click", function () { switchTab(b.dataset.tab); });
  });

  // Git workflow bar. Commit opens the message sheet; the rest POST straight
  // to their endpoints (with the desktop's confirm/override prompts).
  actionsEl.querySelectorAll(".act").forEach(function (b) {
    b.addEventListener("click", function () {
      if (b.disabled || !current) return;
      switch (b.dataset.act) {
        case "commit": openCommitSheet(); break;
        case "push": doPush(false); break;
        case "pr": doMakePr(); break;
        case "merge": doMerge(); break;
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

  applyViewport();
  poll();
  // Visibility-aware cadence: 4s while visible, ~30s while the tab is hidden
  // (a backgrounded phone browser doesn't need a fresh session list).
  // Self-rescheduling timeout re-reads the cadence every cycle; returning to
  // the tab polls immediately and re-arms at 4s.
  var _pollTimer = null;
  function schedulePoll() {
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(function () { poll(); schedulePoll(); },
      document.hidden ? 30000 : 4000);
  }
  schedulePoll();
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
    schedulePoll();
  });
})();
