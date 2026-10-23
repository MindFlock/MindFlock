// Headless drive of /m's voice loop: static-serve backend/web/static, mock
// every /api route, stub webkitSpeechRecognition + speechSynthesis, and walk
// the scenarios. Run: node voice_browser.js  (tests/unit/test_mobile_voice_browser.py
// runs it and reads the PASS/FAIL lines; MF_ROOT, MF_PORT and MF_CHROME override
// the repo root, the static server's port and the browser binary).
const path = require("path");
const { spawn } = require("child_process");

const ROOT = process.env.MF_ROOT || path.resolve(__dirname, "..", "..", "..", "..");
const { chromium } = require(path.join(ROOT, "frontend", "node_modules", "playwright-core"));
const STATIC = path.join(ROOT, "backend", "web", "static");
const PORT = Number(process.env.MF_PORT) || 18779;
const CHROME = process.env.MF_CHROME || "/usr/bin/google-chrome";

const state = {
  rows: [
    { title: "login-fix", name: "Login fix", activity: "working", status: "running", last_turn: "Reading auth.py", last_say: null, voice: false, activity_since: Date.now() / 1000 - 90 },
    { title: "invoice-parser", name: "Invoice parser", activity: "idle", status: "running", last_turn: "", last_say: "Parser done. Want tests?", voice: false },
    { title: "api-docs", name: "API docs", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false },
  ],
  say: { login: null },
  voiceGet: { on: false, say: null, has_question: false, source: "none", ts: null },
  lastReply: "",
  dialog: null,       // null → 204
  voiceCfg: null,     // null → 404
  posts: [],          // {url, body}
};

function json(route, body, status) {
  return route.fulfill({ status: status || 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mock(route) {
  const req = route.request();
  const url = new URL(req.url());
  const p = url.pathname;
  const m = req.method();
  let body = null;
  if (m === "POST") { try { body = JSON.parse(req.postData() || "{}"); } catch (e) { body = {}; } state.posts.push({ path: p, body }); }
  if (p === "/api/instances" && m === "GET") return json(route, state.rows);
  let mm;
  if ((mm = /^\/api\/instances\/([^/]+)\/voice\/condense$/.exec(p))) return json(route, { text: "It fixed the login bug and wants to push.", source: "model" });
  if ((mm = /^\/api\/instances\/([^/]+)\/voice$/.exec(p))) {
    if (m === "POST") { const r = state.rows.find((x) => x.title === decodeURIComponent(mm[1])); if (r) r.voice = !!body.on; return json(route, Object.assign({}, state.voiceGet, { on: !!body.on })); }
    return json(route, state.voiceGet);
  }
  if ((mm = /^\/api\/instances\/([^/]+)\/send$/.exec(p))) return json(route, state.sendReply || { sent: true, submitted: true }, state.sendStatus || 200);
  if ((mm = /^\/api\/instances\/([^/]+)\/output$/.exec(p))) {
    const view = url.searchParams.get("view");
    return json(route, { view, text: view === "last_reply" ? state.lastReply : "", source: "x", has_question: false, truncated: false, activity: "working" });
  }
  if ((mm = /^\/api\/instances\/([^/]+)\/dialog$/.exec(p))) {
    if (!state.dialog) return route.fulfill({ status: 204, body: "" });
    return json(route, state.dialog);
  }
  if ((mm = /^\/api\/instances\/([^/]+)\/answer$/.exec(p))) return json(route, state.answerReply || { ok: true }, state.answerStatus || 200);
  if (p === "/api/voice") return state.voiceCfg ? json(route, state.voiceCfg) : json(route, { error: "nope" }, 404);
  if (p === "/api/fleet") return json(route, { in_fleet: false, members: [], requests: [] });
  if (p === "/api/update/check") return json(route, {});
  if (p === "/api/settings") return json(route, { ui: {} });
  if (p === "/api/remote/hello") return json(route, { device: "here" });
  if (p === "/api/devices") return json(route, []);
  if (p.startsWith("/api/instances/") && p.endsWith("/code-map/live")) return json(route, {});
  return json(route, {});
}

const INIT = `
  window.__recs = []; window.__spoken = []; window.__cancels = 0; window.__wake = 0; window.__vibes = 0;
  class FakeRec {
    constructor() { this.started = false; window.__recs.push(this); window.__rec = this; }
    start() { this.started = true; window.__recStarts = (window.__recStarts || 0) + 1; }
    stop() { this.started = false; const f = this.onend; if (f) setTimeout(() => f(), 0); }
    abort() { this.started = false; }
  }
  window.webkitSpeechRecognition = FakeRec;
  window.SpeechRecognition = FakeRec;
  window.__hear = function (text, interimOnly) {
    const r = window.__rec; if (!r || !r.started) return false;
    const res = [{ transcript: text }]; res.isFinal = !interimOnly;
    r.onresult({ resultIndex: 0, results: [res] });
    return true;
  };
  window.__silence = function () { const r = window.__rec; if (!r || !r.started) return false; r.started = false; r.onerror && r.onerror({ error: "no-speech" }); r.onend && r.onend(); return true; };
  const synth = {
    speaking: false, pending: false,
    _voices: [{ name: "Samantha", lang: "en-US" }, { name: "Daniel", lang: "en-GB" }, { name: "Anna", lang: "de-DE" }],
    speak(u) { window.__spoken.push(u.text); setTimeout(() => { if (u.onend) u.onend({}); }, 15); },
    cancel() { window.__cancels++; },
    getVoices() { return this._voices; },
    addEventListener() {},
  };
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  window.SpeechSynthesisUtterance = class { constructor(t) { this.text = t; } };
  Object.defineProperty(navigator, "wakeLock", { value: { request() { window.__wake++; return Promise.resolve({ release() { return Promise.resolve(); }, addEventListener() {} }); } }, configurable: true });
  Object.defineProperty(navigator, "vibrate", { value: function () { window.__vibes++; return true; }, configurable: true });
`;

const results = [];
function check(name, ok, detail) { results.push({ name, ok: !!ok, detail }); console.log((ok ? "PASS " : "FAIL ") + name + (detail ? "  — " + detail : "")); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(page, fn, ms, label) {
  try { await page.waitForFunction(fn, null, { timeout: ms, polling: 50 }); return true; }
  catch (e) { console.log("  timeout waiting for " + label); return false; }
}
const spoken = (page) => page.evaluate(() => window.__spoken.slice());
const saidAll = (page) => page.evaluate(() => window.__spoken.join(" "));
const said = async (page, text) => (await saidAll(page)).indexOf(text) >= 0;
const vstate = (page) => page.evaluate(() => window.mfVoice.state);
const posts = (p) => state.posts.filter((x) => x.path.endsWith(p));

(async () => {
  const srv = spawn("python3", ["-m", "http.server", String(PORT), "--directory", STATIC, "--bind", "127.0.0.1"], { stdio: "ignore" });
  await sleep(800);
  const browser = await chromium.launch({ executablePath: CHROME, args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 780 }, isMobile: true, hasTouch: true, userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36" });
  await ctx.addInitScript(INIT);
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("response", (r) => { if (r.status() === 404) console.log("  404: " + r.url()); });
  page.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
  await page.route("**/api/**", mock);
  try {
    await page.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page, () => window.mfVoice && document.getElementById("picker-title").textContent === "Login fix", 5000, "first poll");
    check("page loaded, session selected", (await page.evaluate(() => document.getElementById("picker-title").textContent)) === "Login fix");
    check("strip hidden while off", await page.evaluate(() => document.getElementById("voice-strip").classList.contains("hidden")));

    // ---- Scenario 1: tap mic → dictate → countdown → /send → ack → reply
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    check("tap mic → listening", (await vstate(page)) === "listening");
    check("prime: an empty utterance was spoken first", (await spoken(page))[0] === "");
    check("wake lock requested", (await page.evaluate(() => window.__wake)) >= 1);
    check("vibrate on Android when listening starts", (await page.evaluate(() => window.__vibes)) >= 1);
    await sleep(100);
    check("POST /voice {on:true} for the session", posts("/voice").some((x) => x.body.on === true && x.path.includes("login-fix")));
    check("strip shows Listening…", (await page.evaluate(() => document.getElementById("voice-state").textContent)) === "Listening…");
    await page.evaluate(() => window.__hear("fix the login", true));
    check("interim caption", (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "fix the login");
    await page.evaluate(() => window.__hear("fix the login bug"));
    await waitFor(page, () => window.mfVoice.state === "confirming", 2000, "confirming");
    check("final → confirming", (await vstate(page)) === "confirming");
    check("text in the compose box", (await page.evaluate(() => document.getElementById("compose").value)) === "fix the login bug");
    check("countdown bar visible", !(await page.evaluate(() => document.getElementById("voice-countdown").classList.contains("hidden"))));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "working after countdown");
    const send = posts("/send");
    check("POST /send after the countdown", send.length === 1 && send[0].path.includes("login-fix"));
    check("send body {text, dialog_safe:true}", send[0] && send[0].body.text === "fix the login bug" && send[0].body.dialog_safe === true);
    check("compose box cleared after send", (await page.evaluate(() => document.getElementById("compose").value)) === "");
    await sleep(200);
    check("spoke 'Sent to Login fix.'", (await spoken(page)).includes("Sent to Login fix."));
    check("state working", (await vstate(page)) === "working");
    // the ack: last_reply changes from the baseline
    state.lastReply = "I'll look at the **login handler** first. Then I'll check `auth.py`.";
    await waitFor(page, () => window.__spoken.some((t) => t.indexOf("login handler") >= 0), 7000, "ack sentence");
    check("spoke the first sentence of the new reply, markdown stripped", (await spoken(page)).includes("I'll look at the login handler first."));
    // the reply event with a question
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 5, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting");
    check("idle event: spoke the say line", await said(page, "Fixed the login bug. Should I push it?"));
    check("has_question → awaiting (listening for the answer)", (await vstate(page)) === "awaiting");
    const nBefore = (await spoken(page)).length;
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 6, event: "session.turn_ended", session: "login-fix", old: null, new: null, ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook", idle_for: 12 } }));
    await sleep(300);
    check("turn_ended with the same say is not spoken twice", (await spoken(page)).length === nBefore);
    await page.evaluate(() => window.__hear("yes push it"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer sent");
    const send2 = posts("/send");
    check("the answer went as a prompt via /send", send2.length === 2 && send2[1].body.text === "yes push it" && send2[1].body.dialog_safe === true);

    // ---- A new turn whose Say line repeats the last one is still news
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 60, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting again");
    check("the same Say line after a NEW prompt is spoken again", (await saidAll(page)).split("Fixed the login bug. Should I push it?").length === 3);
    await page.evaluate(() => window.__hear("no thanks"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer 2 sent");

    // ---- Poll fallback: idle row with a new last_say, socket dead
    state.rows[0].activity = "idle";
    state.rows[0].last_say = "Pushed. All done here.";
    state.voiceGet = { on: true, say: "Pushed. All done here.", has_question: false, source: "hook", ts: Date.now() / 1000 };
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Pushed. All done here.") >= 0, 9000, "poll fallback say");
    check("4 s poll fallback spoke the new last_say", await said(page, "Pushed. All done here."));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after plain reply");
    check("no question + no hands-free → idle", (await vstate(page)) === "idle");
    check("strip says Your turn", (await page.evaluate(() => document.getElementById("voice-state").textContent)) === "Your turn");
    await sleep(4500);
    check("poll does not re-speak the same say", (await saidAll(page)).split("Pushed. All done here.").length === 2);

    // ---- Scenario 2: clarify + dialog → spoke the question → "yes" → /answer
    state.dialog = { id: "d1", parsed: true, question: "Allow git push?", command: "git push origin main", options: [{ key: "1", label: "Yes", kind: "yes" }, { key: "2", label: "Yes, and don't ask again", kind: "always" }, { key: "3", label: "No", kind: "no" }] };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 7, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog listening");
    const dline = await saidAll(page);
    check("spoke the dialog question", dline.indexOf("Login fix: Allow git push?") >= 0, dline.slice(-200));
    check("…then the command", dline.indexOf("It wants to run: git push origin main.") > 0);
    check("…then 'Say yes, no, or always.'", dline.indexOf("Say yes, no, or always.") > 0);
    check("dialog state, listening", (await vstate(page)) === "dialog");
    await page.evaluate(() => window.__hear("yes"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered");
    let ans = posts("/answer");
    check("POST /answer {keys:['1'], dialog_id:'d1', by:'user'}", ans.length === 1 && JSON.stringify(ans[0].body) === JSON.stringify({ keys: ["1"], dialog_id: "d1", by: "user" }), JSON.stringify(ans[0] && ans[0].body));
    check("spoke 'Answered yes.'", (await spoken(page)).includes("Answered yes."));

    // ---- Scenario 3: "always allow" → always; "yeah sure" → yes, never always
    state.dialog = Object.assign({}, state.dialog, { id: "d2" });
    state.rows[0].activity = "clarify";
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 8, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 2");
    await page.evaluate(() => window.__hear("always allow"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered 2");
    ans = posts("/answer");
    check("'always allow' → the always option (key 2)", ans.length === 2 && ans[1].body.keys[0] === "2" && ans[1].body.dialog_id === "d2", JSON.stringify(ans[1] && ans[1].body));
    state.dialog = Object.assign({}, state.dialog, { id: "d3" });
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 9, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 3");
    await page.evaluate(() => window.__hear("yeah sure"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered 3");
    ans = posts("/answer");
    check("'yeah sure' → yes (key 1), never always", ans.length === 3 && ans[2].body.keys[0] === "1" && ans[2].body.dialog_id === "d3", JSON.stringify(ans[2] && ans[2].body));
    // unknown answer → retry line, then "no"
    state.dialog = Object.assign({}, state.dialog, { id: "d4" });
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 10, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 4");
    await page.evaluate(() => window.__hear("banana"));
    await waitFor(page, () => window.__spoken.includes("I didn't catch that: say yes, no, or always."), 3000, "retry line");
    check("unknown word → 'I didn't catch that: say yes, no, or always.'", (await spoken(page)).includes("I didn't catch that: say yes, no, or always."));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 4 again");
    await page.evaluate(() => window.__hear("no don't"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered 4");
    ans = posts("/answer");
    check("'no don't' → no (key 3)", ans.length === 4 && ans[3].body.keys[0] === "3", JSON.stringify(ans[3] && ans[3].body));
    // a refusal that contains "always" is a NO; a number never picks a permission
    state.dialog = Object.assign({}, state.dialog, { id: "d4b" });
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 110, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 4b");
    await page.evaluate(() => window.__hear("two"));
    await waitFor(page, () => window.__spoken.filter((t) => t === "I didn't catch that: say yes, no, or always.").length >= 2, 3000, "retry line 2");
    check("'two' on a permission dialog → retry, never the always option", posts("/answer").length === 4);
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 4b again");
    await page.evaluate(() => window.__hear("no, not always"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered 4b");
    ans = posts("/answer");
    check("'no, not always' → no (key 3), not always", ans.length === 5 && ans[4].body.keys[0] === "3" && ans[4].body.dialog_id === "d4b", JSON.stringify(ans[4] && ans[4].body));
    // other-only options: numbered
    state.dialog = { id: "d5", parsed: true, question: "Which branch?", command: "", options: [{ key: "1", label: "main", kind: "other" }, { key: "2", label: "develop", kind: "other" }, { key: "3", label: "release", kind: "other" }] };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 11, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog 5");
    const d5 = await saidAll(page);
    check("other-only options are numbered: 'Say one, two, or three.'", d5.indexOf("Login fix: Which branch? one: main. two: develop. three: release. Say one, two, or three.") > 0, d5.slice(-200));
    await page.evaluate(() => window.__hear("option two"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "answered 5");
    ans = posts("/answer");
    check("'option two' → key 2", ans.length === 6 && ans[5].body.keys[0] === "2", JSON.stringify(ans[5] && ans[5].body));
    // a stale clarify right after that answer (the row lags a poll or two): silent
    state.dialog = null;
    const nSpoken = (await spoken(page)).length;
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 111, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await sleep(400);
    check("a 204 right after a voice answer is not 'waiting on you'", (await spoken(page)).length === nSpoken && (await vstate(page)) === "working");
    // 204 → "waiting on you: open the screen" — outside the grace after an
    // answer (a stale clarify reading right after a voice answer is silent).
    state.dialog = null;
    await page.evaluate(() => { window.mfVoice.answeredAt = 0; });
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 12, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.__spoken.includes("Login fix is waiting on you: open the screen."), 3000, "204 line");
    check("dialog 204 → 'is waiting on you: open the screen.'", (await spoken(page)).includes("Login fix is waiting on you: open the screen."));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after 204");
    // approve_dialogs false → read, "Answer it on the screen.", no listening
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: false };
    await page.evaluate(() => window.mfVoice.cfg.approve_dialogs = false);
    state.dialog = { id: "d6", parsed: true, question: "Allow rm -rf build?", command: "rm -rf build", options: [{ key: "1", label: "Yes", kind: "yes" }, { key: "2", label: "No", kind: "no" }] };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 13, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.__spoken.includes("Answer it on the screen."), 4000, "answer on screen");
    check("approve_dialogs off → 'Answer it on the screen.'", (await spoken(page)).includes("Answer it on the screen."));
    await sleep(300);
    check("…and no listening for an answer", (await vstate(page)) === "idle" && !(await page.evaluate(() => window.__rec && window.__rec.started)));
    check("yes/no-only dialog says 'Say yes or no.'", await said(page, "Allow rm -rf build? It wants to run: rm -rf build. Say yes or no."));
    await page.evaluate(() => window.mfVoice.cfg.approve_dialogs = true);
    state.dialog = null;
    state.rows[0].activity = "idle";

    // ---- Scenario 4: "switch to <name>"
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for switch");
    await page.evaluate(() => window.__hear("switch to invoice parser"));
    await waitFor(page, () => window.mfVoice.session === "invoice-parser" && window.mfVoice.state === "idle", 4000, "switched");
    check("'switch to invoice parser' selected it", (await page.evaluate(() => window.mfVoice.session)) === "invoice-parser" && (await page.evaluate(() => document.getElementById("picker-title").textContent)) === "Invoice parser");
    check("spoke 'Now on Invoice parser. <last_say>'", await said(page, "Now on Invoice parser. Parser done. Want tests?"));
    check("POST /voice {on:true} for the new session", posts("/voice").some((x) => x.path.includes("invoice-parser") && x.body.on === true));
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for fuzzy switch");
    await page.evaluate(() => window.__hear("go to the api"));
    await waitFor(page, () => window.mfVoice.session === "api-docs", 4000, "fuzzy switched");
    check("'go to the api' word-overlap picks API docs", (await page.evaluate(() => window.mfVoice.session)) === "api-docs");
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for bad switch");
    await page.evaluate(() => window.__hear("switch to banana"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("No session called banana") >= 0, 3000, "no match line");
    check("no match → reads the sessions", await said(page, "No session called banana. Sessions: Login fix, Invoice parser, API docs."));

    // ---- commands: what needs me / cancel / status / repeat / stop
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    state.rows[0].activity = "clarify"; state.rows[0].last_say = "Should I push?";
    await sleep(4600);   // the page's row list refreshes on its 4 s poll
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("What needs me?"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Login fix is asking") >= 0, 3000, "needs me");
    check("'what needs me' reads the attention list", await said(page, "Login fix is asking: Should I push?"));
    check("...and ends the sentence once", !(await said(page, "push?.")));
    state.rows[0].activity = "idle";
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    const sendsBefore = posts("/send").length;
    await page.evaluate(() => window.__hear("never mind"));
    await sleep(3200);
    check("'never mind' drops the prompt (no /send, idle)", posts("/send").length === sendsBefore && (await vstate(page)) === "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    const before = (await saidAll(page)).split("Login fix is asking: Should I push?").length;
    await page.evaluate(() => window.__hear("say that again"));
    await sleep(600);
    check("'say that again' re-speaks the last line", (await saidAll(page)).split("Login fix is asking: Should I push?").length === before + 1, (await saidAll(page)).slice(-120));
    // a tap on the strip during confirming keeps the text
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("add a test for the parser"));
    await waitFor(page, () => window.mfVoice.state === "confirming", 2000, "confirming");
    await page.click("#voice-caption");
    await sleep(3000);
    check("tap on the strip stops the countdown and keeps the text", (await vstate(page)) === "idle" && (await page.evaluate(() => document.getElementById("compose").value)) === "add a test for the parser" && posts("/send").length === sendsBefore);
    // Send button then sends it through /send
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button");
    check("Send button sends the kept text via /send with dialog_safe", posts("/send").length === sendsBefore + 1 && posts("/send").slice(-1)[0].body.text === "add a test for the parser" && posts("/send").slice(-1)[0].body.dialog_safe === true);
    // 409 waking
    state.voiceGet = { on: true, say: null, has_question: false, source: "none", ts: null };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 14, event: "session.activity_changed", session: "api-docs", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: null, has_question: false, source: "none" } }));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after no-say turn");
    check("idle with no say while working → '<name> finished.'", (await spoken(page)).includes("API docs finished."));
    state.sendStatus = 409; state.sendReply = { error: "session is waking up", waking: true };
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("hello there"));
    await waitFor(page, () => window.__spoken.includes("API docs is waking up, try again in a moment."), 5000, "waking line");
    check("409 waking → spoken reason", (await spoken(page)).includes("API docs is waking up, try again in a moment."));
    check("…text stays in the box", (await page.evaluate(() => document.getElementById("compose").value)) === "hello there");
    state.sendStatus = 0; state.sendReply = null;
    await page.evaluate(() => { document.getElementById("compose").value = ""; });
    // "voice off"
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("voice off"));
    await waitFor(page, () => window.mfVoice.state === "off", 3000, "off");
    check("'voice off' → off + POST /voice {on:false}", (await vstate(page)) === "off" && posts("/voice").some((x) => x.path.includes("api-docs") && x.body.on === false));
    check("strip hidden again", await page.evaluate(() => document.getElementById("voice-strip").classList.contains("hidden")));
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("cs_voice")));
    check("localStorage holds session/on/rate/voiceName/handsFree only", stored && Object.keys(stored).sort().join(",") === "handsFree,on,rate,session,voiceName" && stored.on === false, JSON.stringify(stored));

    // ---- the sheet: diagnostics + voices
    await page.evaluate(() => document.getElementById("voice-btn").click());
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "on again");
    await page.click("#voice-gear");
    await sleep(200);
    const diag = await page.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics name recognition / speech / mode / wake lock / socket", /Recognition: available/.test(diag) && /Speech: available, 3 voices/.test(diag) && /Mode: browser tab, Android/.test(diag) && /Wake lock: (held|supported)/.test(diag) && /Events socket: /.test(diag), diag.replace(/\n/g, " | "));
    const opts = await page.evaluate(() => Array.from(document.querySelectorAll("#voice-voice option")).map((o) => o.textContent));
    check("voice picker lists voices, this language first", opts[0] === "Default voice" && opts[1] === "Samantha (en-US)" && opts.length === 4, JSON.stringify(opts));
    check("approve line read-only text", /Approve dialogs by voice: (on|off) \(Settings → Your phone\)/.test(await page.evaluate(() => document.getElementById("voice-approve-line").textContent)));
    await page.click("#voice-done");

    // ---- deep link ?voice=1 → "Tap to hear <name>"
    state.voiceGet = { on: true, say: "Docs rebuilt. Anything else?", has_question: true, source: "hook", ts: Date.now() / 1000 };
    state.rows[2].last_say = "Docs rebuilt. Anything else?";
    const page2 = await ctx.newPage();
    await page2.route("**/api/**", mock);
    await page2.evaluate(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} }).catch(() => {});
    await page2.goto(`http://127.0.0.1:${PORT}/mobile.html?s=api-docs&voice=1`, { waitUntil: "load" });
    await waitFor(page2, () => !document.getElementById("voice-hear").classList.contains("hidden"), 5000, "hear button");
    check("deep link shows 'Tap to hear API docs'", (await page2.evaluate(() => document.getElementById("voice-hear").textContent)) === "Tap to hear API docs");
    check("…and nothing was spoken before the tap", (await page2.evaluate(() => window.__spoken.length)) === 0);
    await page2.click("#voice-hear");
    await waitFor(page2, () => window.mfVoice.state === "awaiting", 5000, "deep link spoke + awaiting");
    check("tap primes (empty utterance first) then speaks the say", (await page2.evaluate(() => window.__spoken[0] === "" && window.__spoken.join(" ").indexOf("Docs rebuilt. Anything else?") >= 0)));
    check("question → listening for the answer", (await page2.evaluate(() => window.mfVoice.state)) === "awaiting");
    await page2.close();
  } catch (e) {
    console.log("HARNESS ERROR", e && e.stack || e);
    results.push({ name: "harness", ok: false, detail: String(e) });
  }
  if (errors.length) console.log("PAGE ERRORS:\n  " + errors.join("\n  "));
  check("no page errors", errors.length === 0);
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed}/${results.length} checks passed`);
  await browser.close();
  srv.kill();
  process.exit(failed ? 1 : 0);
})();
