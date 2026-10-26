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
  routeStatus: 0,     // non-zero → /api/voice/route answers that status (an older server: 404)
  routeDelay: 0,      // ms before /api/voice/route answers (past 2 s the page stops waiting)
  transcribes: [],    // POST /api/voice/transcribe: {ctype, head, len, rate}
  transcribeReply: { text: "", backend: "builtin", ms: 90 },
  // The streaming recognizer (POST /api/voice/stream …): ids s1, s2… in open order.
  streamOpens: [],        // the open's JSON bodies
  streamOpenStatus: 0,    // non-zero → the open answers that status (409: no streaming)
  streamChunks: [],       // {id, ctype, head, rate, len}
  streamScript: {},       // id → [{committed, partial}…], one per chunk in order (then empty words)
  streamEndReply: {},     // id → {committed}
  streamEnds: [],
  streamDeletes: [],
  streamInflight: 0,
  streamMaxInflight: 0,
  chunkDelay: 120,        // ms each chunk takes to answer
  streamFailChunk: {},    // id → n: that stream's nth chunk answers 500
  dialogsBy: {},          // title → its dialog (GET /dialog and the router's answers); else `dialog`
};
const NUMS = { one: 1, two: 2, three: 3, four: 4, nine: 9, "1": 1, "2": 2, "3": 3, "4": 4, "9": 9 };

// A small stand-in for the server's router (POST /api/voice/route): the
// commands the scenarios say, a session it creates for "new session …",
// and "prompt" for everything else.
function routeAnswer(text, session) {
  const t = String(text || "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
  let m;
  // The widened vocabulary: numbered / stepped switching, answers (by row
  // number the router names only the index; asked again with that row's
  // title it names the dialog), "what is it asking".
  if ((m = /^session (\w+)$/.exec(t)) && NUMS[m[1]]) return { kind: "switch_index", index: NUMS[m[1]] };
  if (t === "next session") return { kind: "switch_rel", delta: 1 };
  if (t === "previous session") return { kind: "switch_rel", delta: -1 };
  if ((m = /^session (\w+) (yes|no)$/.exec(t)) && NUMS[m[1]]) {
    const d = state.dialogsBy[session];
    if (d) return { kind: "answer", choice: m[2], title: session, dialog_id: d.id, keys: [m[2] === "yes" ? "1" : "2"], say: "Answered " + m[2] + "." };
    return { kind: "answer", choice: m[2], index: NUMS[m[1]] };
  }
  if (t === "yes" && state.dialogsBy[session]) return { kind: "answer", choice: "yes", dialog_id: state.dialogsBy[session].id, keys: ["1"] };
  if (t === "what is it asking") {
    const row = state.rows.find((r) => r.title === session);
    return { kind: "question", say: (row ? row.name : session) + ": Should I deploy it?" };
  }
  if (t === "is it a newer kind") return { kind: "summon_the_flock" };
  if ((m = /^switch to (.+)$/.exec(t))) {
    const row = state.rows.find((r) => r.name.toLowerCase() === m[1]);
    if (!row) return { kind: "no-session", say: "No session called " + m[1] + "." };
    return { kind: "switch", title: row.title, name: row.name, say: "Now on " + row.name + "." + (row.last_say ? " " + row.last_say : "") };
  }
  if ((m = /^new session (?:to )?(.+)$/.exec(t))) {
    const title = m[1].replace(/ /g, "-");
    const name = m[1].charAt(0).toUpperCase() + m[1].slice(1);
    state.rows.push({ title, name, activity: "working", status: "running", last_turn: "", last_say: null, voice: true });
    return { kind: "new", title, name, say: "Started " + name + "." };
  }
  if (t === "what sessions do i have") return { kind: "list", say: "You have " + state.rows.length + " sessions: " + state.rows.map((r) => r.name).join(", ") + "." };
  if (t === "what needs me") return { kind: "needs", say: "" };
  if (t === "never mind") return { kind: "cancel" };
  if (t === "say that again") return { kind: "repeat" };
  if (t === "voice off") return { kind: "off" };
  return { kind: "prompt" };
}

function json(route, body, status) {
  return route.fulfill({ status: status || 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function mock(route) {
  const req = route.request();
  const url = new URL(req.url());
  const p = url.pathname;
  const m = req.method();
  let body = null;
  if (p === "/api/voice/transcribe" && m === "POST") {
    // The clip is binary: read it as bytes, never as JSON.
    const buf = req.postDataBuffer() || Buffer.alloc(0);
    state.transcribes.push({
      ctype: req.headers()["content-type"] || "",
      head: buf.slice(0, 4).toString("latin1"),
      wave: buf.slice(8, 12).toString("latin1"),
      len: buf.length,
      rate: buf.length >= 28 ? buf.readUInt32LE(24) : 0,
      dataSize: buf.length >= 44 ? buf.readUInt32LE(40) : 0,
    });
    return json(route, state.transcribeReply);
  }
  let sm;
  if (p === "/api/voice/stream" && m === "POST") {
    let b = {};
    try { b = JSON.parse(req.postData() || "{}"); } catch (e) {}
    state.streamOpens.push(b);
    if (state.streamOpenStatus) return json(route, { error: "this backend does not stream" }, state.streamOpenStatus);
    return json(route, { id: "s" + state.streamOpens.length });
  }
  if ((sm = /^\/api\/voice\/stream\/([^/]+)\/chunk$/.exec(p)) && m === "POST") {
    const buf = req.postDataBuffer() || Buffer.alloc(0);
    state.streamInflight++;
    state.streamMaxInflight = Math.max(state.streamMaxInflight, state.streamInflight);
    state.streamChunks.push({
      id: sm[1], ctype: req.headers()["content-type"] || "", head: buf.slice(0, 4).toString("latin1"),
      rate: buf.length >= 28 ? buf.readUInt32LE(24) : 0, len: buf.length,
    });
    await sleep(state.chunkDelay);
    state.streamInflight--;
    if (state.streamChunks.filter((c) => c.id === sm[1]).length === state.streamFailChunk[sm[1]]) return json(route, { error: "decoder fell over" }, 500);
    const q = state.streamScript[sm[1]] || [];
    return json(route, Object.assign({ committed: "", partial: "", ms: 42, audio_s: 0.4 }, q.shift() || {}));
  }
  if ((sm = /^\/api\/voice\/stream\/([^/]+)\/end$/.exec(p)) && m === "POST") {
    state.streamEnds.push(sm[1]);
    return json(route, Object.assign({ committed: "", ms: 30 }, state.streamEndReply[sm[1]] || {}));
  }
  if ((sm = /^\/api\/voice\/stream\/([^/]+)$/.exec(p)) && m === "DELETE") {
    state.streamDeletes.push(sm[1]);
    return json(route, { ok: true });
  }
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
    const dt = decodeURIComponent(mm[1]);
    const d = dt in state.dialogsBy ? state.dialogsBy[dt] : state.dialog;
    if (!d) return route.fulfill({ status: 204, body: "" });
    return json(route, d);
  }
  if ((mm = /^\/api\/instances\/([^/]+)\/answer$/.exec(p))) return json(route, state.answerReply || { ok: true }, state.answerStatus || 200);
  if (p === "/api/voice/route" && state.routeDelay) await sleep(state.routeDelay);
  if (p === "/api/voice/route") return state.routeStatus ? json(route, { error: "no route" }, state.routeStatus) : json(route, routeAnswer(body.text, body.session));
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
    stop() {
      this.started = false;
      // __onStopFinal: the phrase a recognizer still hands over after a graceful stop, late.
      const late = window.__onStopFinal; window.__onStopFinal = null;
      if (late) {
        setTimeout(() => { const res = [{ transcript: late }]; res.isFinal = true; if (this.onresult) this.onresult({ resultIndex: 0, results: [res] }); if (this.onend) this.onend(); }, 400);
        return;
      }
      const f = this.onend; if (f) setTimeout(() => f(), 0);
    }
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
  window.__netfail = function () { const r = window.__rec; if (!r || !r.started) return false; r.started = false; r.onerror && r.onerror({ error: "network" }); r.onend && r.onend(); return true; };
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
// Every reply line starts with the session's name: "Login fix: …" (the
// voice says it sentence by sentence, so the name opens the first chunk).
const spokeAs = async (page, name, text) => said(page, name + ": " + text);

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

    // ---- Scenario 1: tap mic → phrases land in the box → "over" keeps them → "submit" sends → ack → reply
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    check("tap mic → listening", (await vstate(page)) === "listening");
    check("prime: an empty utterance was spoken first", (await spoken(page))[0] === "");
    check("wake lock requested", (await page.evaluate(() => window.__wake)) >= 1);
    check("vibrate on Android when listening starts", (await page.evaluate(() => window.__vibes)) >= 1);
    await sleep(100);
    check("POST /voice {on:true} for the session", posts("/voice").some((x) => x.body.on === true && x.path.includes("login-fix")));
    check("strip shows Listening…", (await page.evaluate(() => document.getElementById("voice-state").textContent)) === "Listening…");
    const hint = await page.evaluate(() => document.getElementById("voice-caption").textContent);
    check("caption hints the radio words", /over/.test(hint) && /submit/.test(hint) && /disregard/.test(hint), hint);
    check("…'Say over to keep it, submit to send'", /^Say “over” to keep it, “submit” to send/.test(hint), hint);
    await page.evaluate(() => window.__hear("fix the login", true));
    check("interim caption", (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "fix the login");
    const compose = () => page.evaluate(() => document.getElementById("compose").value);
    await page.evaluate(() => window.__hear("fix the login bug"));
    await waitFor(page, () => document.getElementById("compose").value === "fix the login bug", 3000, "routed phrase in the box");
    check("phrase → in the compose box, still listening", (await vstate(page)) === "listening" && (await compose()) === "fix the login bug", await compose());
    const route1 = posts("/voice/route");
    check("the first phrase into an empty box went to POST /api/voice/route {text, session}", route1.length === 1 && route1[0].body.text === "fix the login bug" && route1[0].body.session === "login-fix", JSON.stringify(route1));
    check("the recognizer stays up for the next phrase", await page.evaluate(() => !!(window.__rec && window.__rec.started)));
    await page.evaluate(() => window.__hear("and add a test"));
    await sleep(150);
    check("second phrase appended", (await compose()) === "fix the login bug and add a test", await compose());
    check("…without asking the router (the box was not empty)", posts("/voice/route").length === 1);
    await page.evaluate(() => window.__hear("Disregard."));
    await sleep(150);
    check("'disregard' empties the box and listens on", (await compose()) === "" && (await vstate(page)) === "listening" && posts("/send").length === 0);
    await page.evaluate(() => window.__hear("submit"));
    await sleep(150);
    check("'submit' with an empty box sends nothing, still listening", posts("/send").length === 0 && (await vstate(page)) === "listening");
    await page.evaluate(() => window.__hear("fix the login bug"));
    await waitFor(page, () => document.getElementById("compose").value === "fix the login bug", 3000, "routed phrase again");
    await page.evaluate(() => window.__hear("Over."));
    await sleep(300);
    check("'over' stops listening, the words left in the box, unsent", (await vstate(page)) === "idle" && !(await page.evaluate(() => window.mfVoice.stt.active())) && (await compose()) === "fix the login bug" && posts("/send").length === 0, (await vstate(page)) + " | " + (await compose()));
    check("…and says how to send: 'In the box — say submit, or tap Send'", (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "In the box — say “submit”, or tap Send", await page.evaluate(() => document.getElementById("voice-caption").textContent));
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening after over");
    await page.evaluate(() => window.__hear("Submit."));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "working after submit");
    const send = posts("/send");
    check("POST /send on 'submit' (the kept words)", send.length === 1 && send[0].path.includes("login-fix"));
    check("send body {text, dialog_safe:true, say:true} — dictated, Spoken replies on", send[0] && send[0].body.text === "fix the login bug" && send[0].body.dialog_safe === true && send[0].body.say === true, JSON.stringify(send[0] && send[0].body));
    check("compose box cleared after send", (await page.evaluate(() => document.getElementById("compose").value)) === "");
    await sleep(200);
    check("spoke 'Sent to Login fix.'", (await spoken(page)).includes("Sent to Login fix."));
    check("state working", (await vstate(page)) === "working");
    // the ack: last_reply changes from the baseline
    state.lastReply = "I'll look at the **login handler** first. Then I'll check `auth.py`.";
    await waitFor(page, () => window.__spoken.some((t) => t.indexOf("login handler") >= 0), 7000, "ack sentence");
    check("spoke the first sentence of the new reply, markdown stripped, name first", (await spoken(page)).includes("Login fix: I'll look at the login handler first."));
    // the reply event with a question
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 5, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting");
    check("idle event: spoke the say line", await said(page, "Fixed the login bug. Should I push it?"));
    check("…as 'Login fix: <say>' (name first)", await spokeAs(page, "Login fix", "Fixed the login bug. Should I push it?"));
    check("has_question → awaiting (listening for the answer)", (await vstate(page)) === "awaiting");
    const nBefore = (await spoken(page)).length;
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 6, event: "session.turn_ended", session: "login-fix", old: null, new: null, ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook", idle_for: 12 } }));
    await sleep(300);
    check("turn_ended with the same say is not spoken twice", (await spoken(page)).length === nBefore);
    await page.evaluate(() => window.__hear("yes push it submit"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer sent");
    const send2 = posts("/send");
    check("the answer went as a prompt via /send", send2.length === 2 && send2[1].body.text === "yes push it" && send2[1].body.dialog_safe === true);

    // ---- A new turn whose Say line repeats the last one is still news
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 60, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting again");
    check("the same Say line after a NEW prompt is spoken again", (await saidAll(page)).split("Fixed the login bug. Should I push it?").length === 3);
    await page.evaluate(() => window.__hear("no thanks, submit"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer 2 sent");

    // ---- Poll fallback: idle row with a new last_say, socket dead
    state.rows[0].activity = "idle";
    state.rows[0].last_say = "Pushed. All done here.";
    state.voiceGet = { on: true, say: "Pushed. All done here.", has_question: false, source: "hook", ts: Date.now() / 1000 };
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Pushed. All done here.") >= 0, 9000, "poll fallback say");
    check("4 s poll fallback spoke the new last_say", await said(page, "Pushed. All done here."));
    check("…name first", await spokeAs(page, "Login fix", "Pushed. All done here."));
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
    check("…the switch came from the router", posts("/voice/route").some((x) => x.body.text === "switch to invoice parser"));
    check("POST /voice {on:true} for the new session", posts("/voice").some((x) => x.path.includes("invoice-parser") && x.body.on === true));
    // An older server (no /api/voice/route): the local table still switches.
    state.routeStatus = 404;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for fuzzy switch");
    await page.evaluate(() => window.__hear("go to the api"));
    await waitFor(page, () => window.mfVoice.session === "api-docs", 4000, "fuzzy switched");
    check("route 404 → the local table: 'go to the api' word-overlap picks API docs", (await page.evaluate(() => window.mfVoice.session)) === "api-docs" && posts("/voice/route").some((x) => x.body.text === "go to the api"));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for bad switch");
    await page.evaluate(() => window.__hear("switch to banana"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("No session called banana") >= 0, 3000, "no match line");
    check("no match → reads the sessions", await said(page, "No session called banana. Sessions: Login fix, Invoice parser, API docs."));
    state.routeStatus = 0;

    // ---- the router: "new session …" (the server creates it), "what sessions…"
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for new session");
    await page.evaluate(() => window.__hear("new session to fix the parser"));
    await waitFor(page, () => window.mfVoice.session === "fix-the-parser", 5000, "new session selected");
    check("'new session to fix the parser' → the new row appears and is selected", (await page.evaluate(() => document.getElementById("picker-title").textContent)) === "Fix the parser" && (await page.evaluate(() => window.mfVoice.session)) === "fix-the-parser");
    await waitFor(page, () => window.__spoken.includes("Started Fix the parser."), 3000, "new session say");
    check("…and speaks the router's line", (await spoken(page)).includes("Started Fix the parser."));
    check("…no prompt was sent", posts("/send").every((x) => x.body.text.indexOf("parser") < 0));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for list");
    await page.evaluate(() => window.__hear("What sessions do I have?"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("You have 4 sessions") >= 0, 3000, "list line");
    check("'what sessions do I have' → the router's line", await said(page, "You have 4 sessions: Login fix, Invoice parser, API docs, Fix the parser."));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for switch back");
    await page.evaluate(() => window.__hear("switch to api docs"));
    await waitFor(page, () => window.mfVoice.session === "api-docs" && window.mfVoice.state === "idle", 4000, "back on API docs");
    check("…and back to API docs", (await page.evaluate(() => window.mfVoice.session)) === "api-docs");

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
    state.routeDelay = 2600;   // a router slower than the page waits: the local table answers
    await page.evaluate(() => window.__hear("never mind"));
    await sleep(3200);
    state.routeDelay = 0;
    check("'never mind' drops the prompt (no /send, idle)", posts("/send").length === sendsBefore && (await vstate(page)) === "idle");
    check("…decided by the local table once the router took over 2 s", /timed out/.test(await page.evaluate(() => window.mfVoice.lastRoute)), await page.evaluate(() => window.mfVoice.lastRoute));
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    const before = (await saidAll(page)).split("Login fix is asking: Should I push?").length;
    await page.evaluate(() => window.__hear("say that again"));
    await sleep(600);
    check("'say that again' re-speaks the last line", (await saidAll(page)).split("Login fix is asking: Should I push?").length === before + 1, (await saidAll(page)).slice(-120));
    // the silence stop (2 minutes without a word): the words stay, unsent
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("add a test for the parser"));
    await waitFor(page, () => document.getElementById("compose").value === "add a test for the parser", 3000, "phrase in the box");
    check("a phrase without 'over' or 'submit' sits in the box, still listening", (await vstate(page)) === "listening" && (await page.evaluate(() => document.getElementById("compose").value)) === "add a test for the parser");
    await page.evaluate(() => { window.mfVoice.listen.until = 0; window.__silence(); });   // the window ran out
    await sleep(300);
    check("silence stops listening and keeps the text, unsent", (await vstate(page)) === "idle" && (await page.evaluate(() => document.getElementById("compose").value)) === "add a test for the parser" && posts("/send").length === sendsBefore);
    check("…and says so", /Kept, not sent/.test(await page.evaluate(() => document.getElementById("voice-caption").textContent)));
    // Send button then sends it through /send
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button");
    check("Send button sends the kept text via /send with dialog_safe (dictated: say true)", posts("/send").length === sendsBefore + 1 && posts("/send").slice(-1)[0].body.text === "add a test for the parser" && posts("/send").slice(-1)[0].body.dialog_safe === true && posts("/send").slice(-1)[0].body.say === true);
    // 409 waking
    state.voiceGet = { on: true, say: null, has_question: false, source: "none", ts: null };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 14, event: "session.activity_changed", session: "api-docs", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: null, has_question: false, source: "none" } }));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after no-say turn");
    check("idle with no say while working → '<name> finished.'", (await spoken(page)).includes("API docs finished."));
    const lastSend = () => posts("/send").slice(-1)[0] || { body: {} };
    const idleEvent = (say, seq) => page.evaluate((a) => window.mfVoice.onEvent({ seq: a[1], event: "session.activity_changed", session: "api-docs", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: a[0], has_question: false, source: "hook" } }), [say, seq]);
    // ---- push to talk, tap mode: the second tap is "over" — it keeps the box, never sends
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("rebuild the docs index"));
    await waitFor(page, () => document.getElementById("compose").value === "rebuild the docs index", 3000, "phrase in the box");
    let nSends = posts("/send").length;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "second tap stopped");
    await sleep(200);
    check("tap mode: the second tap keeps the words, unsent", (await vstate(page)) === "idle" && posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "rebuild the docs index");
    check("…'In the box — say submit, or tap Send'", /^In the box/.test(await page.evaluate(() => document.getElementById("voice-caption").textContent)));
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button after the tap");
    check("…then the Send button sends it (dictated: say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "rebuild the docs index" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await idleEvent("Index rebuilt.", 15);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after index");
    check("…its reply is spoken, name first", (await spoken(page)).includes("API docs: Index rebuilt."));
    // ...a tap with an empty box only stops; the strip is the mic's twin
    nSends = posts("/send").length;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.click("#voice-caption");
    await sleep(300);
    check("tap on the strip with an empty box just stops (no /send)", (await vstate(page)) === "idle" && posts("/send").length === nSends);
    // ---- a typed prompt asks for no Say line, and its reply is not read out
    await page.fill("#compose", "run the linter");
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "typed send");
    check("typed prompt → /send say:false", lastSend().body.text === "run the linter" && lastSend().body.say === false, JSON.stringify(lastSend().body));
    await sleep(200);
    await idleEvent("Lint is clean.", 16);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after typed");
    await sleep(200);
    check("…its reply is NOT spoken", !(await said(page, "Lint is clean.")));
    check("…the strip says 'Your turn'", (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "Your turn");
    // ---- Spoken replies off: dictated prompts ask for no Say line either, and the phone stays quiet
    await page.evaluate(() => { const el = document.getElementById("voice-replies"); el.checked = false; el.dispatchEvent(new Event("change")); });
    const sentLines = (await spoken(page)).filter((t) => t === "Sent to API docs.").length;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("check the build submit"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "replies-off send");
    check("Spoken replies off → /send say:false even when dictated", lastSend().body.text === "check the build" && lastSend().body.say === false, JSON.stringify(lastSend().body));
    await sleep(300);
    check("…'Sent to' is shown, not spoken", (await spoken(page)).filter((t) => t === "Sent to API docs.").length === sentLines && (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "Sent to API docs.");
    await idleEvent("Build is green.", 17);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after replies off");
    await sleep(200);
    check("…and the reply is not spoken", !(await said(page, "Build is green.")));
    await page.evaluate(() => { const el = document.getElementById("voice-replies"); el.checked = true; el.dispatchEvent(new Event("change")); });
    state.sendStatus = 409; state.sendReply = { error: "session is waking up", waking: true };
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("hello there submit"));
    await waitFor(page, () => window.__spoken.includes("API docs is waking up, try again in a moment."), 5000, "waking line");
    check("409 waking → spoken reason", (await spoken(page)).includes("API docs is waking up, try again in a moment."));
    check("…text stays in the box", (await page.evaluate(() => document.getElementById("compose").value)) === "hello there");
    check("'hello there submit' into an empty box was routed (the router said prompt) as 'hello there'", posts("/voice/route").some((x) => x.body.text === "hello there"));
    state.sendStatus = 0; state.sendReply = null;
    await page.evaluate(() => { document.getElementById("compose").value = ""; });
    // ---- a Chromium without Google's speech key (Arc, Brave, Vivaldi…):
    // every start fails with "network". The first is retried like a blip;
    // the second stops the loop and says why — never eight silent restarts.
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    const startsBefore = await page.evaluate(() => window.__recStarts);
    await page.evaluate(() => window.__netfail());
    await sleep(500);
    check("first network error → restarted once, still listening", (await vstate(page)) === "listening" && (await page.evaluate(() => window.__recStarts)) === startsBefore + 1, "starts " + (await page.evaluate(() => window.__recStarts)) + " vs " + startsBefore);
    await page.evaluate(() => window.__netfail());
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after network errors");
    await sleep(400);
    const netErr = await page.evaluate(() => document.getElementById("voice-error").textContent);
    check("second network error → idle, the reason shown", (await vstate(page)) === "idle" && /^Speech recognition failed \(network\)/.test(netErr) && !(await page.evaluate(() => document.getElementById("voice-error").classList.contains("hidden"))), netErr);
    check("…and no further recognizer starts", (await page.evaluate(() => window.__recStarts)) === startsBefore + 1);
    check("…diagnostics keep it as the last error", (await page.evaluate(() => window.mfVoice.lastError)).indexOf("network") > 0);
    // ---- push to talk, hold mode: press to talk, let go to keep ("over"); "submit" sends
    await page.evaluate(() => { const el = document.getElementById("voice-ptt"); el.value = "hold"; el.dispatchEvent(new Event("change")); });
    check("hold mode: the strip says 'Hold the mic to talk'", (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "Hold the mic to talk");
    const mic = await page.locator("#voice-btn").boundingBox();
    await page.mouse.move(mic.x + mic.width / 2, mic.y + mic.height / 2);
    await page.mouse.down();
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening");
    check("pointerdown on the mic → listening", (await vstate(page)) === "listening");
    const holdHint = await page.evaluate(() => document.getElementById("voice-caption").textContent);
    check("…the hint: 'Release to keep it; say submit to send'", holdHint.indexOf("Release to keep it; say “submit” to send") >= 0, holdHint);
    await page.evaluate(() => window.__hear("deploy the docs"));
    await waitFor(page, () => document.getElementById("compose").value === "deploy the docs", 3000, "hold phrase");
    nSends = posts("/send").length;
    await page.evaluate(() => { window.__onStopFinal = "and tag it"; });   // still on its way when the finger lifts
    await page.mouse.up();
    await waitFor(page, () => window.mfVoice.state === "idle" && document.getElementById("compose").value === "deploy the docs and tag it", 4000, "hold kept");
    check("pointerup keeps, after waiting for the last phrase — never sends", posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "deploy the docs and tag it", await page.evaluate(() => document.getElementById("compose").value));
    await sleep(300);
    check("…and the release's click starts nothing", (await vstate(page)) === "idle" && /^In the box/.test(await page.evaluate(() => document.getElementById("voice-caption").textContent)));
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button after the hold");
    check("…the Send button sends it (say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "deploy the docs and tag it" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await idleEvent("Deployed and tagged.", 18);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after hold");
    check("…its reply is spoken, name first", (await spoken(page)).includes("API docs: Deployed and tagged."));
    nSends = posts("/send").length;
    await page.mouse.down();
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 2");
    await page.mouse.up();
    await sleep(400);
    check("a release with nothing heard just stops", (await vstate(page)) === "idle" && posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("voice-caption").textContent)) === "Hold the mic to talk");
    await page.dispatchEvent("#voice-btn", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 3");
    await page.evaluate(() => window.__hear("ship it"));
    await waitFor(page, () => document.getElementById("compose").value === "ship it", 3000, "hold phrase 3");
    nSends = posts("/send").length;
    await page.dispatchEvent("#voice-btn", "pointercancel");
    await waitFor(page, () => window.mfVoice.state === "idle", 4000, "pointercancel kept");
    check("pointercancel keeps like a release", posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "ship it");
    // A press that says "submit": the kept words go, with say.
    await page.dispatchEvent("#voice-btn", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 4");
    await page.evaluate(() => window.__hear("now submit"));
    await waitFor(page, () => window.mfVoice.state === "working", 4000, "hold submit sent");
    check("holding, 'submit' sends the box (say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "ship it now" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await page.dispatchEvent("#voice-btn", "pointercancel");   // the finger lifts after: nothing more
    await sleep(200);
    check("…the release after a submit changes nothing", (await vstate(page)) === "working" && posts("/send").length === nSends + 1);
    await idleEvent("Shipped.", 19);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after cancel");
    check("the ptt choice is remembered", (await page.evaluate(() => JSON.parse(localStorage.getItem("cs_voice")).ptt)) === "hold");
    await page.evaluate(() => { const el = document.getElementById("voice-ptt"); el.value = "toggle"; el.dispatchEvent(new Event("change")); });
    // "voice off"
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("voice off"));
    await waitFor(page, () => window.mfVoice.state === "off", 3000, "off");
    check("'voice off' → off + POST /voice {on:false}", (await vstate(page)) === "off" && posts("/voice").some((x) => x.path.includes("api-docs") && x.body.on === false));
    check("strip hidden again", await page.evaluate(() => document.getElementById("voice-strip").classList.contains("hidden")));
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("cs_voice")));
    check("localStorage holds session/on/rate/voiceName/handsFree/ptt/replies only", stored && Object.keys(stored).sort().join(",") === "handsFree,on,ptt,rate,replies,session,voiceName" && stored.on === false && stored.ptt === "toggle" && stored.replies === true, JSON.stringify(stored));

    // ---- the sheet: diagnostics + voices
    await page.evaluate(() => document.getElementById("voice-btn").click());
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "on again");
    await page.click("#voice-gear");
    await sleep(200);
    const diag = await page.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics name recognition / speech / mode / wake lock / socket", /Recognition: available/.test(diag) && /Speech: available, 3 voices/.test(diag) && /Mode: browser tab, Android/.test(diag) && /Wake lock: (held|supported)/.test(diag) && /Events socket: /.test(diag), diag.replace(/\n/g, " | "));
    check("diagnostics: Replies / Push to talk / Last prompt", /Replies: on/.test(diag) && /Push to talk: tap/.test(diag) && /Last prompt: dictated, say yes/.test(diag), diag.replace(/\n/g, " | "));
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
    check("…name first", await spokeAs(page2, "API docs", "Docs rebuilt. Anything else?"));
    check("question → listening for the answer", (await page2.evaluate(() => window.mfVoice.state)) === "awaiting");
    await page2.close();

    // ---- server recognition: the page records WAV and POSTs it
    // getUserMedia hands over a stream the test controls: a 440 Hz tone
    // whose gain __tone() sets (a hush of noise, never digital silence, so
    // the noise floor has something to follow).
    state.voiceCfg = {
      stt: "server", tts: "phone", approve_dialogs: true,
      server_ready: { stt: true, tts: false, stt_backend: "builtin" },
      backends: { stt: ["builtin"], tts: [] },
      system: { available: true, platform: "Windows", detail: "System.Speech (en-US)", voices: [], recognizers: ["en-US"] },
    };
    state.transcribeReply = { text: "fix the parser submit", backend: "builtin", ms: 90 };
    state.transcribes = [];
    const sends3 = posts("/send").length;
    const page3 = await ctx.newPage();
    page3.on("pageerror", (e) => errors.push(String(e)));
    await page3.addInitScript(() => {
      window.__gum = 0;
      navigator.mediaDevices.getUserMedia = function () {
        window.__gum++;
        const ac = new AudioContext();
        const osc = ac.createOscillator();
        const gain = ac.createGain();
        const dst = ac.createMediaStreamDestination();
        gain.gain.value = 0.002;
        osc.frequency.value = 440;
        osc.connect(gain); gain.connect(dst); osc.start();
        window.__tone = (g) => { gain.gain.value = g; };
        return Promise.resolve(dst.stream);
      };
    });
    await page3.route("**/api/**", mock);
    await page3.evaluate(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} }).catch(() => {});
    await page3.goto(`http://127.0.0.1:${PORT}/mobile.html?s=invoice-parser`, { waitUntil: "load" });
    await waitFor(page3, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Invoice parser", 5000, "server page loaded");
    await page3.click("#voice-btn");
    await waitFor(page3, () => window.mfVoice.state === "listening" && window.mfVoice.stt.name === "server" && window.__tone, 4000, "server listening");
    check("server recognition: the server adapter listens on the tap", (await page3.evaluate(() => window.mfVoice.stt.name)) === "server" && (await page3.evaluate(() => window.mfVoice.state)) === "listening");
    check("…through the page's one AudioContext, running", (await page3.evaluate(() => window.mfVoice.audioCtx && window.mfVoice.audioCtx.state)) === "running");
    await sleep(400);
    await page3.evaluate(() => window.__tone(0.4));
    await sleep(900);
    const level = await page3.evaluate(() => document.getElementById("voice-level-bar").style.width || "");
    check("the level meter moves with the voice", /%$/.test(level) && parseFloat(level) > 0, level);
    await page3.evaluate(() => window.__tone(0.002));
    // VOICE_SILENCE_MS of quiet after speech ends the clip by itself.
    const t0 = Date.now();
    while (!state.transcribes.length && Date.now() - t0 < 6000) await sleep(50);
    const tr = state.transcribes[0] || {};
    check("silence after speech ended the clip: one POST /api/voice/transcribe", state.transcribes.length === 1, JSON.stringify(state.transcribes));
    check("…Content-Type audio/wav", tr.ctype === "audio/wav", tr.ctype);
    check("…the body is a RIFF/WAVE file at 16 kHz", tr.head === "RIFF" && tr.wave === "WAVE" && tr.rate === 16000 && tr.dataSize === tr.len - 44 && tr.len > 44 + 16000, JSON.stringify(tr));
    await waitFor(page3, () => window.mfVoice.state === "working", 5000, "server words sent");
    const sent3 = posts("/send").slice(sends3);
    check("the server's 'fix the parser submit' went through the radio rules: sent", sent3.length === 1 && sent3[0].body.text === "fix the parser" && sent3[0].path.includes("invoice-parser"), JSON.stringify(sent3));
    check("the microphone was asked for once", (await page3.evaluate(() => window.__gum)) === 1);
    await page3.click("#voice-gear");
    await sleep(200);
    const diag3 = await page3.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics: the WAV recorder and the server's built-in engine", /Recorder: WAV 16 kHz \(ScriptProcessor\)/.test(diag3) && /Built-in engine: Windows — System\.Speech \(en-US\)/.test(diag3) && /Last transcribe: 90 ms via builtin/.test(diag3), diag3.replace(/\n/g, " | "));
    check("diagnostics: the clip went through the browser's resampler; the silence stop is 1100 ms", /Resampler: OfflineAudioContext \(the browser's\)$/m.test(diag3) && /Silence stop: 1100 ms after speech/.test(diag3), diag3.replace(/\n/g, " | "));
    await page3.close();

    // ---- streaming recognition: the words appear while the phrase is spoken
    state.voiceCfg = {
      stt: "server", tts: "phone", approve_dialogs: true,
      server_ready: { stt: true, tts: false, stt_backend: "whisper", stt_stream: true },
      backends: { stt: ["whisper"], tts: [] },
    };
    state.voiceGet = { on: false, say: null, has_question: false, source: "none", ts: null };
    state.transcribes = [];
    // Phrase 1 into the empty box (it may be a command: it arrives whole);
    // phrase 2 streams into the box and ends in "submit".
    state.streamScript = {
      s1: [{ committed: "", partial: "fix the" }, { committed: "fix the", partial: "parser" }, { committed: "parser", partial: "" }],
      s2: [{ committed: "and add", partial: "a" }, { committed: "a test", partial: "submit" }],
    };
    state.streamEndReply = { s2: { committed: "submit" } };
    const sends4 = posts("/send").length;
    const page4 = await ctx.newPage();
    page4.on("pageerror", (e) => errors.push(String(e)));
    await page4.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = function () {
        const ac = new AudioContext();
        const osc = ac.createOscillator();
        const gain = ac.createGain();
        const dst = ac.createMediaStreamDestination();
        gain.gain.value = 0.002;
        osc.frequency.value = 440;
        osc.connect(gain); gain.connect(dst); osc.start();
        window.__tone = (g) => { gain.gain.value = g; };
        return Promise.resolve(dst.stream);
      };
      // Every value the box and the caption take, in order.
      window.__boxes = []; window.__caps = [];
      setInterval(() => {
        const b = document.getElementById("compose"), c = document.getElementById("voice-caption");
        if (b && b.value !== window.__lastBox) { window.__lastBox = b.value; window.__boxes.push(b.value); }
        if (c && c.textContent !== window.__lastCap) { window.__lastCap = c.textContent; window.__caps.push(c.textContent); }
      }, 15);
    });
    await page4.route("**/api/**", mock);
    await page4.evaluate(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} }).catch(() => {});
    await page4.goto(`http://127.0.0.1:${PORT}/mobile.html?s=invoice-parser`, { waitUntil: "load" });
    await waitFor(page4, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Invoice parser", 5000, "stream page loaded");
    await page4.click("#voice-btn");
    await waitFor(page4, () => window.mfVoice.state === "listening" && window.mfVoice.stt.name === "server" && window.__tone, 4000, "stream listening");
    let t4 = Date.now();
    while (!state.streamOpens.length && Date.now() - t4 < 3000) await sleep(30);
    check("streaming: the tap opened a stream with the page's language", state.streamOpens.length === 1 && !!state.streamOpens[0].lang, JSON.stringify(state.streamOpens));
    await sleep(400);   // the noise floor first, as a voice would
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(1300);
    await page4.evaluate(() => window.__tone(0.002));
    t4 = Date.now();
    while (state.streamOpens.length < 2 && Date.now() - t4 < 6000) await sleep(30);
    const box1 = await page4.evaluate(() => window.__boxes.filter(Boolean));
    const caps1 = await page4.evaluate(() => window.__caps.slice());
    check("…phrase 1 into an empty box (maybe a command) showed in the caption as it came", caps1.indexOf("fix the") >= 0 && caps1.indexOf("fix the parser") >= 0, JSON.stringify(caps1));
    check("…and reached the box whole, after the silence stop's /end", box1[0] === "fix the parser" && state.streamEnds[0] === "s1", JSON.stringify([box1, state.streamEnds]));
    check("…the router had the whole phrase", state.posts.some((x) => x.path === "/api/voice/route" && x.body.text === "fix the parser"));
    check("…the next phrase opened a new stream", state.streamOpens.length === 2);
    await sleep(300);
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(1300);
    await page4.evaluate(() => window.__tone(0.002));
    await waitFor(page4, () => window.mfVoice.state === "working" || window.mfVoice.state === "sending", 6000, "stream words sent");
    const boxes4 = await page4.evaluate(() => window.__boxes.slice());
    const caps4 = await page4.evaluate(() => window.__caps.slice());
    const i1 = boxes4.indexOf("fix the parser and"), i2 = boxes4.indexOf("fix the parser and add a");
    check("phrase 2's settled words landed in the box progressively, the last word held back", i1 >= 0 && i2 > i1, JSON.stringify(boxes4));
    check("…the held word + the partial showed in the caption", caps4.indexOf("add a") >= 0 && caps4.indexOf("test submit") >= 0, JSON.stringify(caps4));
    check("…'submit' never reached the box", !boxes4.some((b) => /submit/i.test(b)), JSON.stringify(boxes4));
    const sent4 = posts("/send").slice(sends4);
    check("…the flush ending in 'submit' sent the box without the word", sent4.length === 1 && sent4[0].body.text === "fix the parser and add a test", JSON.stringify(sent4));
    check("chunks went one at a time (never two in flight)", state.streamChunks.length >= 4 && state.streamMaxInflight === 1, state.streamChunks.length + " chunks, max in flight " + state.streamMaxInflight);
    check("…each a 16 kHz WAV of at least 300 ms", state.streamChunks.every((c) => c.ctype === "audio/wav" && c.head === "RIFF" && c.rate === 16000 && c.len >= 44 + 9600), JSON.stringify(state.streamChunks.slice(0, 3)));
    check("…and no clip went to /transcribe", state.transcribes.length === 0);
    // Cancel mid-phrase: the stream is DELETEd.
    await sleep(300);
    if (!["listening", "awaiting"].includes(await vstate(page4))) await page4.click("#voice-btn");
    const opens5 = state.streamOpens.length;
    t4 = Date.now();
    while (state.streamOpens.length === opens5 && Date.now() - t4 < 4000) await sleep(30);
    await sleep(500);
    await page4.click("#voice-cancel");
    const sid5 = "s" + state.streamOpens.length;
    t4 = Date.now();
    while (state.streamDeletes.indexOf(sid5) < 0 && Date.now() - t4 < 3000) await sleep(30);
    check("Cancel mid-phrase DELETEs the stream", state.streamDeletes.indexOf(sid5) >= 0 && state.streamEnds.indexOf(sid5) < 0, JSON.stringify([sid5, state.streamDeletes, state.streamEnds]));
    await page4.click("#voice-gear");
    await sleep(200);
    const diag4 = await page4.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics: words as you speak, the last chunk's time", /Recognition stream: words as you speak \(last chunk 42 ms, 0\.4 s of audio\)/.test(diag4), diag4.replace(/\n/g, " | "));
    await page4.click("#voice-done");
    // A 409 on the open: the phrase is a clip, and the page stops asking.
    state.streamOpenStatus = 409;
    state.transcribeReply = { text: "check the logs over", backend: "whisper", ms: 80 };
    await page4.evaluate(() => { document.getElementById("compose").value = ""; });
    await page4.click("#voice-btn");
    await waitFor(page4, () => window.mfVoice.state === "listening", 3000, "listening for the clip");
    await sleep(300);
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(900);
    await page4.evaluate(() => window.__tone(0.002));
    t4 = Date.now();
    while (!state.transcribes.length && Date.now() - t4 < 6000) await sleep(50);
    await waitFor(page4, () => window.mfVoice.state === "idle", 4000, "clip kept");
    check("a 409 on /stream → the clip path: one POST /api/voice/transcribe", state.transcribes.length === 1 && state.transcribes[0].rate === 16000, JSON.stringify(state.transcribes));
    check("…its words went through the radio rules: kept", (await page4.evaluate(() => document.getElementById("compose").value)) === "check the logs" && (await vstate(page4)) === "idle");
    check("…and the page stops asking to stream", (await page4.evaluate(() => window.mfVoice.cfg.server_ready.stt_stream)) === false);
    await page4.click("#voice-gear");
    await sleep(200);
    const diag5 = await page4.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics: one clip per phrase", /Recognition stream: one clip per phrase \(last chunk failed — this backend does not stream\)/.test(diag5), diag5.replace(/\n/g, " | "));
    await page4.click("#voice-done");
    // A chunk failing mid-phrase: the words so far stay, the audio the
    // stream had not answered goes as a clip behind them.
    state.streamOpenStatus = 0;
    const sidF = "s" + (state.streamOpens.length + 1);
    state.streamScript[sidF] = [{ committed: "check the", partial: "build" }];
    state.streamFailChunk[sidF] = 2;
    state.transcribeReply = { text: "logs over", backend: "whisper", ms: 70 };
    const deletesF = state.streamDeletes.length;
    await page4.evaluate(() => { document.getElementById("compose").value = ""; window.mfVoice.cfg.server_ready.stt_stream = true; });
    await page4.click("#voice-btn");
    await waitFor(page4, () => window.mfVoice.state === "listening", 3000, "listening for the failing stream");
    await sleep(400);
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(1200);
    await page4.evaluate(() => window.__tone(0.002));
    t4 = Date.now();
    while (state.transcribes.length < 2 && Date.now() - t4 < 6000) await sleep(50);
    await waitFor(page4, () => window.mfVoice.state === "idle", 4000, "failed stream kept");
    check("a chunk failing mid-phrase → the rest as a clip, behind the words so far", state.transcribes.length === 2 && (await page4.evaluate(() => document.getElementById("compose").value)) === "check the build logs", JSON.stringify([state.transcribes.length, await page4.evaluate(() => document.getElementById("compose").value)]));
    check("…the broken stream was DELETEd, never ended", state.streamDeletes.slice(deletesF).indexOf(sidF) >= 0 && state.streamEnds.indexOf(sidF) < 0, JSON.stringify([sidF, state.streamDeletes, state.streamEnds]));
    await page4.close();
    state.voiceCfg = null;


    // ---- the wider vocabulary: numbered rows, stop phrases anywhere, answers
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    state.routeStatus = 0;
    state.routeDelay = 0;
    state.dialog = null;
    state.dialogsBy = {};
    state.rows = [
      { title: "login-fix", name: "Login fix", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false },
      { title: "invoice-parser", name: "Invoice parser", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false },
      { title: "api-docs", name: "API docs", activity: "idle", status: "running", last_turn: "", last_say: "Docs rebuilt. Anything else?", voice: false },
    ];
    const page5 = await ctx.newPage();
    page5.on("pageerror", (e) => errors.push(String(e)));
    await page5.route("**/api/**", mock);
    await page5.evaluate(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} }).catch(() => {});
    await page5.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page5, () => window.mfVoice && window.mfVoice.cfg && document.getElementById("picker-title").textContent === "Login fix", 5000, "vocab page loaded");
    const rowTitles = () => page5.evaluate(() => Array.from(document.querySelectorAll("#session-list .sess-row")).map((r) => r.getAttribute("data-title")));
    const listen5 = async (label) => {
      if (!["listening", "awaiting"].includes(await vstate(page5))) await page5.click("#voice-btn");
      return waitFor(page5, () => window.mfVoice.state === "listening" || window.mfVoice.state === "awaiting", 3000, label);
    };
    const idle5 = (label) => waitFor(page5, () => window.mfVoice.state === "idle", 4000, label);
    const posts5 = (p) => state.posts.filter((x) => x.path.endsWith(p));
    await page5.click("#voice-btn");
    await waitFor(page5, () => window.mfVoice.state === "listening", 3000, "vocab listening");
    await page5.click("#voice-cancel");
    await idle5("vocab idle");

    // Numbered rows, in the order shown, held while the sheet is open.
    await page5.click("#picker");
    await waitFor(page5, () => !document.getElementById("sessions-sheet").classList.contains("hidden"), 2000, "sessions sheet");
    const order0 = await rowTitles();
    const nums0 = await page5.evaluate(() => Array.from(document.querySelectorAll("#session-list .sess-num")).map((n) => n.textContent));
    check("the Sessions sheet numbers its rows 1, 2, 3 in the order shown", nums0.join(",") === "1,2,3" && order0.length === 3, JSON.stringify([nums0, order0]));
    check("…and says how to use the numbers while voice is on", !(await page5.evaluate(() => document.getElementById("sessions-voice-note").classList.contains("hidden"))));
    // A row that now needs you would sort first; a new session arrives.
    state.rows.find((r) => r.title === order0[2]).activity = "clarify";
    state.rows.push({ title: "new-thing", name: "New thing", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false });
    await waitFor(page5, () => document.querySelectorAll("#session-list .sess-row").length === 4, 6000, "the new row");
    const order1 = await rowTitles();
    check("while the sheet is open the order holds; a new session goes to the end", JSON.stringify(order1) === JSON.stringify(order0.concat(["new-thing"])), JSON.stringify([order0, order1]));
    await page5.evaluate(() => document.getElementById("sessions-sheet").classList.add("hidden"));
    state.rows.find((r) => r.title === order0[2]).activity = "idle";
    state.rows.pop();
    await waitFor(page5, () => document.querySelectorAll("#session-list .sess-row").length === 3, 6000, "back to three rows");
    const order = await rowTitles();

    // "session two" (the router's switch_index) → the second row shown.
    await listen5("listening for session two");
    await page5.evaluate(() => window.__hear("session two"));
    await page5.waitForFunction((t) => window.mfVoice.session === t, order[1], { timeout: 4000 }).catch(() => {});
    check("'session two' selects the second row shown, voice follows", (await page5.evaluate(() => window.mfVoice.session)) === order[1], JSON.stringify([order, await page5.evaluate(() => window.mfVoice.session)]));
    check("…the router said switch_index (diagnostics: last route kind)", /^switch_index 2/.test(await page5.evaluate(() => window.mfVoice.lastRoute)), await page5.evaluate(() => window.mfVoice.lastRoute));
    await idle5("idle after session two");
    // "next session" → the row after it.
    await listen5("listening for next session");
    await page5.evaluate(() => window.__hear("next session"));
    await page5.waitForFunction((t) => window.mfVoice.session === t, order[2], { timeout: 4000 }).catch(() => {});
    check("'next session' moves one row down", (await page5.evaluate(() => window.mfVoice.session)) === order[2]);
    await idle5("idle after next");
    // Out of range.
    await listen5("listening for session nine");
    await page5.evaluate(() => window.__hear("session nine"));
    await waitFor(page5, () => window.__spoken.includes("There is no session 9."), 3000, "no session 9");
    check("'session nine' → 'There is no session 9.'", (await spoken(page5)).includes("There is no session 9.") && (await page5.evaluate(() => window.mfVoice.session)) === order[2]);
    await idle5("idle after nine");
    // A 404 route → the local table numbers the rows the same way.
    state.routeStatus = 404;
    await listen5("listening for local session one");
    await page5.evaluate(() => window.__hear("go to session one"));
    await page5.waitForFunction((t) => window.mfVoice.session === t, order[0], { timeout: 4000 }).catch(() => {});
    check("route 404 → the local table: 'go to session one' → the first row", (await page5.evaluate(() => window.mfVoice.session)) === order[0]);
    await idle5("idle after local");
    state.routeStatus = 0;
    const cur = order[0];
    const curName = state.rows.find((r) => r.title === cur).name;

    // "stop recording" in the middle of dictation: stop, the box kept, nothing sent.
    const sends5 = posts5("/send").length;
    await listen5("listening for dictation");
    await page5.evaluate(() => window.__hear("fix the flaky test"));
    await waitFor(page5, () => document.getElementById("compose").value === "fix the flaky test", 3000, "dictated");
    const routes5 = posts5("/voice/route").length;
    await page5.evaluate(() => window.__hear("and the docs, stop recording"));
    await idle5("stopped by the phrase");
    check("'… stop recording' mid-dictation stops listening, the box kept without the words", (await vstate(page5)) === "idle" && (await page5.evaluate(() => document.getElementById("compose").value)) === "fix the flaky test and the docs" && posts5("/send").length === sends5, await page5.evaluate(() => document.getElementById("compose").value));
    check("…never asked the router", posts5("/voice/route").length === routes5);
    check("…and the strip says how to send", /^In the box/.test(await page5.evaluate(() => document.getElementById("voice-caption").textContent)));
    await listen5("listening for that's all");
    await page5.evaluate(() => window.__hear("That's all."));
    await idle5("that's all");
    check("'that's all' alone stops too, the box untouched", (await page5.evaluate(() => document.getElementById("compose").value)) === "fix the flaky test and the docs");
    await page5.evaluate(() => { const c = document.getElementById("compose"); c.value = ""; c.dispatchEvent(new Event("input")); });

    // "yes" with a pending dialog (the router names it) → POST /answer.
    state.dialogsBy[cur] = { id: "dv1", parsed: true, question: "Allow npm publish?", command: "npm publish", options: [{ key: "1", label: "Yes", kind: "yes" }, { key: "2", label: "No", kind: "no" }] };
    await listen5("listening for yes");
    await page5.evaluate(() => window.__hear("yes"));
    await waitFor(page5, () => window.mfVoice.state === "working", 4000, "answered by the router");
    let ans5 = posts5("/answer");
    check("'yes' with a pending dialog → POST /answer {keys:['1'], dialog_id, by:'user'}", ans5.length >= 1 && ans5[ans5.length - 1].path.includes(cur) && JSON.stringify(ans5[ans5.length - 1].body) === JSON.stringify({ keys: ["1"], dialog_id: "dv1", by: "user" }), JSON.stringify(ans5));
    check("…'Answered yes.'", (await spoken(page5)).includes("Answered yes."));
    await page5.click("#voice-cancel");
    await idle5("idle after answer");
    // Approve dialogs by voice off: read, never answered.
    await page5.evaluate(() => { window.mfVoice.cfg.approve_dialogs = false; });
    const nAns = posts5("/answer").length;
    await listen5("listening for gated yes");
    await page5.evaluate(() => window.__hear("yes"));
    await waitFor(page5, () => window.__spoken.filter((t) => t === "Answer it on the screen.").length >= 1, 3000, "gated");
    check("approve_dialogs off → 'Answer it on the screen.', no /answer", posts5("/answer").length === nAns);
    await idle5("idle after gated");
    await page5.evaluate(() => { window.mfVoice.cfg.approve_dialogs = true; });
    delete state.dialogsBy[cur];

    // "session two yes": the router names only the row → asked again with
    // that row's title → /answer THERE; voice stays here.
    const other = order[1], otherName = state.rows.find((r) => r.title === other).name;
    state.dialogsBy[other] = { id: "dv2", parsed: true, question: "Allow rm build?", command: "rm -rf build", options: [{ key: "1", label: "Yes", kind: "yes" }, { key: "2", label: "No", kind: "no" }] };
    await listen5("listening for session two yes");
    await page5.evaluate(() => window.__hear("session two yes"));
    await waitFor(page5, () => window.__spoken.some((t) => /: Answered yes\.$/.test(t)), 4000, "targeted answer");
    ans5 = posts5("/answer");
    const r5 = posts5("/voice/route").slice(-2);
    check("'session two yes' answers row two: POST /answer there", ans5.length === nAns + 1 && ans5[ans5.length - 1].path.includes(other) && ans5[ans5.length - 1].body.dialog_id === "dv2" && ans5[ans5.length - 1].body.keys[0] === "1", JSON.stringify(ans5.slice(-1)));
    check("…the router was asked again about that row's session", r5.length === 2 && r5[1].body.session === other, JSON.stringify(r5));
    check("…said name first", (await spoken(page5)).includes(otherName + ": Answered yes."));
    check("…and voice stayed on the current session", (await page5.evaluate(() => window.mfVoice.session)) === cur && (await page5.evaluate(() => document.getElementById("picker-title").textContent)) === curName);
    await idle5("idle after targeted");
    // The same offline: the page reads that session's dialog itself.
    state.routeStatus = 404;
    await listen5("listening for local targeted");
    await page5.evaluate(() => window.__hear("session two, no"));
    await page5.waitForFunction((line) => window.__spoken.includes(line), otherName + ": answered no.", { timeout: 4000, polling: 50 }).catch(() => console.log("  timeout waiting for local targeted"));
    ans5 = posts5("/answer");
    check("route 404 → 'session two, no' answers row two's dialog locally (key 2), voice stays", ans5.length === nAns + 2 && ans5[ans5.length - 1].path.includes(other) && ans5[ans5.length - 1].body.keys[0] === "2" && (await page5.evaluate(() => window.mfVoice.session)) === cur, JSON.stringify(ans5.slice(-1)));
    await idle5("idle after local targeted");
    state.routeStatus = 0;
    delete state.dialogsBy[other];

    // "what is it asking" → the router's say.
    await listen5("listening for what is it asking");
    await page5.evaluate(() => window.__hear("What is it asking?"));
    await waitFor(page5, () => window.__spoken.some((t) => t.indexOf("Should I deploy it?") >= 0), 3000, "question say");
    check("'what is it asking' speaks the router's say, name first", (await spoken(page5)).includes(curName + ": Should I deploy it?"));
    await idle5("idle after question");

    // A question (no dialog): the answer is a prompt to this session.
    await page5.evaluate((t) => window.mfVoice.onEvent({ seq: 500, event: "session.activity_changed", session: t, old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Tests pass. Should I push?", has_question: true, source: "hook" } }), cur);
    await waitFor(page5, () => window.mfVoice.state === "awaiting" && window.__rec && window.__rec.started, 4000, "awaiting the answer");
    const sendsQ = posts5("/send").length;
    await page5.evaluate(() => window.__hear("no thanks"));
    await waitFor(page5, () => window.mfVoice.state === "working", 4000, "question answered");
    const sq = posts5("/send").slice(sendsQ);
    check("a question's answer ('no thanks') goes as a prompt to this session, no 'submit' needed", sq.length === 1 && sq[0].path.includes(cur) && sq[0].body.text === "no thanks", JSON.stringify(sq));
    await page5.click("#voice-cancel");
    await idle5("idle after the question");
    // Another session's question: "tell API docs yes" → /send there, voice stays.
    await listen5("listening for tell");
    await page5.evaluate(() => { document.getElementById("compose").value = "draft for later"; });
    await page5.evaluate(() => window.__hear("tell API docs yes"));
    await waitFor(page5, () => window.__spoken.includes("Sent to API docs."), 4000, "tell");
    const st = posts5("/send").slice(-1)[0];
    check("'tell API docs yes' (it asked a question) → /send 'yes' there", st && st.path.includes("api-docs") && st.body.text === "yes", JSON.stringify(st));
    check("…voice stays here and the box is kept", (await page5.evaluate(() => window.mfVoice.session)) === cur && (await page5.evaluate(() => document.getElementById("compose").value)) === "draft for later");
    await idle5("idle after tell");
    await page5.evaluate(() => { const c = document.getElementById("compose"); c.value = ""; c.dispatchEvent(new Event("input")); });

    // A kind this page does not know is a prompt: the words stay words.
    await listen5("listening for an unknown kind");
    await page5.evaluate(() => window.__hear("is it a newer kind"));
    await waitFor(page5, () => document.getElementById("compose").value === "is it a newer kind", 3000, "unknown kind in the box");
    check("an unknown route kind → a prompt (the words in the box, still listening)", (await page5.evaluate(() => document.getElementById("compose").value)) === "is it a newer kind" && (await vstate(page5)) === "listening");
    await page5.click("#voice-cancel");

    // Diagnostics: the route kind and the numbering.
    await page5.click("#voice-gear");
    await sleep(200);
    const diag6 = await page5.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics: the session numbers, as the sheet shows them", diag6.indexOf("Session numbers: 1 ") >= 0 && /held while it is open/.test(diag6), diag6.replace(/\n/g, " | "));
    check("diagnostics: the last route kind", /Router: summon_the_flock \(unknown here: a prompt\)/.test(diag6), diag6.replace(/\n/g, " | "));
    await page5.click("#voice-done");
    await page5.close();
    state.voiceCfg = null;
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
