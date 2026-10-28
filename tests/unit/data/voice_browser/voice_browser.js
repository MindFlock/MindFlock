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
  voiceCfgDelay: 0,   // ms before GET /api/voice answers (the words scenario: ours until then)
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
  streamEndDelay: {},     // id → ms before /end answers
  streamEnds: [],
  streamDeletes: [],
  streamInflight: 0,
  streamMaxInflight: 0,
  chunkDelay: 120,        // ms each chunk takes to answer
  streamFailChunk: {},    // id → n: that stream's nth chunk answers 500
  dialogsBy: {},          // title → its dialog (GET /dialog and the router's answers); else `dialog`
  // GET /api/notify/ntfy: the voice scenarios run with the ask pop-up off
  // (their clarify rows would open it over the mic); the ask scenarios turn it on.
  ntfy: { ask_popup: false, active: false },
  // The say-all marker: GET /api/voice carries say_all unless null (an older
  // server); POST /api/voice/say-all answers sayAllStatus when non-zero (403).
  sayAll: null,
  sayAllStatus: 0,
  // The wake word: POST /api/voice/wake's clips, and what it answers
  // (wakeStatus non-zero → that status: 409 = no recognizer ready).
  wakes: [],          // {ctype, head, rate, ms, lang}
  wakeReply: { wake: false, text: "the" },
  wakeStatus: 0,
  wakeDelay: 0,       // ms /api/voice/wake takes (the server's fast model: ~300)
  wakeAnsweredAt: 0,  // when it last answered (the immediate start is timed from it)
  // The server's voice (POST /api/voice/speak): each request, and how slow
  // it is — speakMsPerChar to make, a clip of speakAudioMsPerChar to play.
  speaks: [],         // {text, at, done, aborted}
  speakMsPerChar: 8,
  speakAudioMsPerChar: 20,
};
// A 16 kHz mono 16-bit WAV of ms of near-silence (the server's voice, mocked).
function wavOf(ms) {
  const n = Math.max(1, Math.round((16000 * ms) / 1000)), buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0, "latin1"); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVE", 8, "latin1");
  buf.write("fmt ", 12, "latin1"); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(16000, 24); buf.writeUInt32LE(32000, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
  buf.write("data", 36, "latin1"); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) buf.writeInt16LE(Math.round(Math.sin(i / 8) * 40), 44 + i * 2);
  return buf;
}
const NUMS = { one: 1, two: 2, three: 3, four: 4, nine: 9, "1": 1, "2": 2, "3": 3, "4": 4, "9": 9 };

// A small stand-in for the server's router (POST /api/voice/route): the
// commands the scenarios say, a session it creates for "new session …",
// and "prompt" for everything else.
function routeAnswer(text, session, pending) {
  const t = String(text || "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
  let m;
  // What the router asked back, answered with the next phrase (pending).
  const switchTo = (row) => ({ kind: "switch", title: row.title, name: row.name, say: "Now on " + row.name + ", idle." });
  if (pending && pending.kind === "pick") {
    const c = (pending.candidates || []).map((x) => state.rows.find((r) => r.title === x.title)).filter(Boolean);
    const hit = t === "copy two" ? c.find((r) => /copy-2$/.test(r.title)) : t === "the first one" ? c[0] : null;
    if (hit) return switchTo(hit);
  }
  if (pending && pending.kind === "new") {
    if (t === "never mind") return { kind: "new-failed", say: "Okay, no new session.", cancelled: true };
    if (pending.await === "repo" && t === "parser") {
      return { kind: "ask", say: "What should it do in parser?", pending: { kind: "new", await: "task", name: "", task: "", repo_name: "parser", repo_path: "/r/parser" } };
    }
    if (pending.await === "task") {
      const title = t.replace(/ /g, "-");
      state.rows.push({ title, name: title, activity: "working", status: "running", last_turn: "", last_say: null, voice: true });
      return { kind: "new", title, name: title, say: "Started " + title + " in " + pending.repo_name + "." };
    }
  }
  if (t === "switch to the sitecheck session") {
    const candidates = state.rows.filter((r) => /^sitecheck/.test(r.title)).map((r) => ({ title: r.title, name: r.name }));
    return { kind: "ambiguous", say: "Which one: " + candidates.map((c) => c.name).join(" or ") + "?", candidates, pending: { kind: "pick", candidates } };
  }
  if (t === "start a new session") {
    return { kind: "ask", say: "In which project? MindFlock or parser?", pending: { kind: "new", await: "repo", name: "", task: "", choices: ["MindFlock", "parser"] } };
  }
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
  // The router answering a select question's step ("the second one").
  if (t === "the second one" && state.dialogsBy[session] && state.dialogsBy[session].kind === "select") {
    const d = state.dialogsBy[session];
    return { kind: "answer", choice: "number", number: 2, title: session, dialog_id: d.id, keys: ["2"] };
  }
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
  // A "stop talking" only the router knows.
  if (t === "could you pipe down") return { kind: "hush", say: null };
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
  if (p === "/api/voice/wake" && m === "POST") {
    const buf = req.postDataBuffer() || Buffer.alloc(0);
    const rate = buf.length >= 28 ? buf.readUInt32LE(24) : 0;
    state.wakes.push({
      ctype: req.headers()["content-type"] || "", head: buf.slice(0, 4).toString("latin1"), rate,
      ms: rate ? Math.round(((buf.length - 44) / 2 / rate) * 1000) : 0, lang: url.searchParams.get("lang"),
    });
    if (state.wakeStatus) return json(route, { error: "no recognizer is ready" }, state.wakeStatus);
    if (state.wakeDelay) await new Promise((r) => setTimeout(r, state.wakeDelay));
    state.wakeAnsweredAt = Date.now();
    return json(route, Object.assign({ word: "mindflock", backend: "local", ms: 80 }, state.wakeReply));
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
    if (state.streamEndDelay[sm[1]]) await sleep(state.streamEndDelay[sm[1]]);
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
  if ((mm = /^\/api\/instances\/([^/]+)\/answer$/.exec(p))) {
    // A select question moves on with the answer (state.onAnswer), as the screen would.
    if (!state.answerStatus && state.onAnswer) state.onAnswer(decodeURIComponent(mm[1]), body);
    return json(route, state.answerReply || { ok: true }, state.answerStatus || 200);
  }
  if (p === "/api/voice/route" && state.routeDelay) await sleep(state.routeDelay);
  if (p === "/api/voice/route") return state.routeStatus ? json(route, { error: "no route" }, state.routeStatus) : json(route, routeAnswer(body.text, body.session, body.pending));
  if (p === "/api/voice/say-all" && m === "POST") {
    if (state.sayAllStatus) return json(route, { error: "owner only" }, state.sayAllStatus);
    state.sayAll = !!body.on;
    return json(route, { say_all: state.sayAll });
  }
  if (p === "/api/voice/speak" && m === "POST") {
    let b = {};
    try { b = JSON.parse(req.postData() || "{}"); } catch (e) {}
    const text = String(b.text || ""), rec = { text, at: Date.now(), done: 0, aborted: false };
    state.speaks.push(rec);
    await new Promise((r) => setTimeout(r, state.speakMsPerChar * text.length));
    rec.done = Date.now();
    try {
      await route.fulfill({ status: 200, contentType: "audio/wav", headers: { "X-Voice-Backend": "voicelab" }, body: wavOf(text.length * state.speakAudioMsPerChar) });
    } catch (e) { rec.aborted = true; }   // the page aborted it (cancel)
    return;
  }
  if (p === "/api/voice" && state.voiceCfgDelay) await sleep(state.voiceCfgDelay);
  if (p === "/api/voice") return state.voiceCfg ? json(route, Object.assign({}, state.voiceCfg, state.sayAll === null ? {} : { say_all: state.sayAll })) : json(route, { error: "nope" }, 404);
  if (p === "/api/notify/ntfy") return json(route, state.ntfy);
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
    start() { this.started = true; window.__recStarts = (window.__recStarts || 0) + 1; window.__recStartAt = Date.now(); }
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
    speak(u) {
      window.__spoken.push(u.text);
      // __speakMs: a slower voice, to see lines queue; __speakLog: when each started and ended.
      const ms = window.__speakMs || 15, rec = { t: u.text, start: Date.now(), end: 0 };
      (window.__speakLog = window.__speakLog || []).push(rec);
      setTimeout(() => { rec.end = Date.now(); if (u.onend) u.onend({}); }, ms);
    },
    cancel() { window.__cancels++; window.__cancelAt = Date.now(); },
    getVoices() { return this._voices; },
    addEventListener() {},
  };
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
  // __oscAt: when each chime tone was made (a chime is two) — the order of chimes and lines.
  if (window.AudioContext) {
    const osc = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function () { (window.__oscAt = window.__oscAt || []).push(Date.now()); return osc.call(this); };
  }
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
// The strip's one word (Listening… / Speaking… / Quiet. / nothing).
const strip = (page) => page.evaluate(() => document.getElementById("voice-caption").textContent);
const posts = (p) => state.posts.filter((x) => x.path.endsWith(p));
// A line about ANOTHER session than the one the phone is on starts with its
// name: "Login fix: …" (the voice says it sentence by sentence, so the name
// opens the first chunk); a line about the phone's own session is bare.
const spokeAs = async (page, name, text) => said(page, name + ": " + text);
const spokeBare = async (page, name, text) => (await said(page, text)) && !(await said(page, name + ": " + text));

(async () => {
  const srv = spawn("python3", ["-m", "http.server", String(PORT), "--directory", STATIC, "--bind", "127.0.0.1"], { stdio: "ignore" });
  await sleep(800);
  const browser = await chromium.launch({ executablePath: CHROME, args: ["--no-sandbox"] });
  const ctx = await browser.newContext({ viewport: { width: 390, height: 780 }, isMobile: true, hasTouch: true, userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36" });
  // How to talk opens the first time voice turns on in a browser: every
  // scenario but its own (a context of its own, below) has seen it.
  await ctx.addInitScript(INIT + `try { localStorage.setItem("cs_voice_tutorial", "1"); } catch (e) {}`);
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

    // ---- Scenario 1: tap mic → phrases land in the box → "over" sends and listens on → "over and out" sends and stops → ack → reply
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    check("tap mic → listening", (await vstate(page)) === "listening");
    check("prime: an empty utterance was spoken first", (await spoken(page))[0] === "");
    check("wake lock requested", (await page.evaluate(() => window.__wake)) >= 1);
    check("vibrate on Android when listening starts", (await page.evaluate(() => window.__vibes)) >= 1);
    await sleep(100);
    check("POST /voice {on:true} for the session", posts("/voice").some((x) => x.body.on === true && x.path.includes("login-fix")));
    check("strip shows Listening…", (await strip(page)) === "Listening…");
    check("…one word, no state word beside it", !(await page.evaluate(() => document.getElementById("voice-state"))));
    // The loop's caption (the hints, the words so far) is the ask sheet's
    // and Diagnostics' (mfVoice.caption) — never drawn on the strip.
    const hint = await page.evaluate(() => window.mfVoice.caption);
    check("caption hints the radio words, no 'submit'", /over/.test(hint) && /over and out/.test(hint) && /disregard/.test(hint) && !/submit/i.test(hint), hint);
    check("…'Say over to send, over and out to stop, disregard to start over'", hint === "Say “over” to send, “over and out” to stop, “disregard” to start over", hint);
    await page.evaluate(() => window.__hear("fix the login", true));
    check("interim words reach the loop's caption, never the strip (it says Listening…)", (await page.evaluate(() => window.mfVoice.caption)) === "fix the login" && (await strip(page)) === "Listening…");
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
    await sleep(150);
    check("…and says 'Cleared.' (a spoken confirmation, the mic still listening)", (await said(page, "Cleared.")) && (await vstate(page)) === "listening");
    await waitFor(page, () => !window.mfVoice.speech.playing && !window.mfVoice.speech.q.length, 3000, "nod said");
    await sleep(250);
    // "submit" is retired: a word like any other — typed, nothing sent.
    await page.evaluate(() => window.__hear("submit"));
    await waitFor(page, () => document.getElementById("compose").value === "submit", 3000, "submit typed");
    check("'submit' is a plain word: typed into the box, nothing sent, still listening", (await compose()) === "submit" && posts("/send").length === 0 && (await vstate(page)) === "listening", await compose());
    await page.evaluate(() => window.__hear("fix the login bug submit"));
    await sleep(150);
    check("…'… submit' too: the words and the word, unsent", (await compose()) === "submit fix the login bug submit" && posts("/send").length === 0, await compose());
    await page.evaluate(() => window.__hear("disregard that"));
    // Words said while a confirmation plays over the listen are its echo
    // (the phone's recognizer cannot be deafened): wait it out.
    await waitFor(page, () => !window.mfVoice.speech.playing && !window.mfVoice.speech.q.length, 3000, "nod said");
    await sleep(250);
    // Continuous: "over" sends the box and the mic listens on.
    await page.evaluate(() => window.__hear("fix the login bug"));
    await waitFor(page, () => document.getElementById("compose").value === "fix the login bug", 3000, "routed phrase again");
    await page.evaluate(() => window.__hear("and add a test, over."));
    for (let t = Date.now(); posts("/send").length < 1 && Date.now() - t < 3000; ) await sleep(30);
    const send0 = posts("/send");
    check("'… over' → POST /send {text, dialog_safe:true, say:true} (dictated, Spoken replies on)", send0.length === 1 && send0[0].path.includes("login-fix") && send0[0].body.text === "fix the login bug and add a test" && send0[0].body.dialog_safe === true && send0[0].body.say === true, JSON.stringify(send0.map((x) => x.body)));
    await sleep(200);
    check("…and listening goes on: the box empty, the recognizer up, no stop", (await vstate(page)) === "listening" && (await compose()) === "" && (await page.evaluate(() => window.mfVoice.stt.active())), await vstate(page));
    check("…'Sent.' said, 'Sent to Login fix.' only shown", (await said(page, "Sent.")) && !(await said(page, "Sent to Login fix.")) && (await page.evaluate(() => window.mfVoice.caption)) === "Sent to Login fix.", await page.evaluate(() => window.mfVoice.caption));
    // A second message in the same listen, each sent on its own "over".
    await page.evaluate(() => window.__hear("then run the linter over"));
    for (let t = Date.now(); posts("/send").length < 2 && Date.now() - t < 3000; ) await sleep(30);
    check("a second '… over' in the same listen sends just its words, still listening", posts("/send")[1].body.text === "then run the linter" && (await vstate(page)) === "listening", JSON.stringify(posts("/send")[1].body));
    // No idle stop while continuous: the window is never due.
    await page.evaluate(() => { window.mfVoice.listen.until = Date.now() - 1000; window.__silence(); });
    await sleep(600);
    check("continuous: silence with the window long past never stops it (no idle stop), the recognizer up again", (await vstate(page)) === "listening" && (await page.evaluate(() => !!(window.__rec && window.__rec.started))), await vstate(page));
    // "over and out": sends what is in the box and stops.
    await page.evaluate(() => window.__hear("fix the login bug"));
    await sleep(150);
    await page.evaluate(() => window.__hear("Over and out."));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "working after over and out");
    const send = posts("/send");
    check("'over and out' → POST /send of the box, then listening stops", send.length === 3 && send[2].body.text === "fix the login bug" && !(await page.evaluate(() => window.mfVoice.stt.active())), JSON.stringify(send.map((x) => x.body.text)));
    check("send body {text, dialog_safe:true, say:true} — dictated, Spoken replies on", send[2] && send[2].body.dialog_safe === true && send[2].body.say === true, JSON.stringify(send[2] && send[2].body));
    check("compose box cleared after send", (await page.evaluate(() => document.getElementById("compose").value)) === "");
    await sleep(200);
    check("spoke 'Sent. Over and out.' (the confirmation, not 'Sent to …')", (await said(page, "Sent. Over and out.")) && !(await said(page, "Sent to Login fix.")));
    check("state working", (await vstate(page)) === "working");
    // the ack: last_reply changes from the baseline
    state.lastReply = "I'll look at the **login handler** first. Then I'll check `auth.py`.";
    await waitFor(page, () => window.__spoken.some((t) => t.indexOf("login handler") >= 0), 7000, "ack sentence");
    check("spoke the first sentence of the new reply, markdown stripped, bare (the phone's own session)", (await spoken(page)).includes("I'll look at the login handler first.") && !(await said(page, "Login fix: I'll")));
    // the reply event with a question
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 5, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting");
    check("idle event: spoke the say line", await said(page, "Fixed the login bug. Should I push it?"));
    check("…bare: the phone is on Login fix", await spokeBare(page, "Login fix", "Fixed the login bug. Should I push it?"));
    check("has_question → awaiting (listening for the answer)", (await vstate(page)) === "awaiting");
    const nBefore = (await spoken(page)).length;
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 6, event: "session.turn_ended", session: "login-fix", old: null, new: null, ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook", idle_for: 12 } }));
    await sleep(300);
    check("turn_ended with the same say is not spoken twice", (await spoken(page)).length === nBefore);
    await page.evaluate(() => window.__hear("yes push it over"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer sent");
    const send2 = posts("/send");
    check("the answer went as a prompt via /send", send2.length === 4 && send2[3].body.text === "yes push it" && send2[3].body.dialog_safe === true);

    // ---- A new turn whose Say line repeats the last one is still news
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 60, event: "session.activity_changed", session: "login-fix", old: "working", new: "idle", ts: Date.now() / 1000, data: { say: "Fixed the login bug. Should I push it?", has_question: true, source: "hook" } }));
    await waitFor(page, () => window.mfVoice.state === "awaiting", 3000, "awaiting again");
    check("the same Say line after a NEW prompt is spoken again", (await saidAll(page)).split("Fixed the login bug. Should I push it?").length === 3);
    await page.evaluate(() => window.__hear("no thanks, over"));
    await waitFor(page, () => window.mfVoice.state === "working", 4500, "answer 2 sent");

    // ---- Poll fallback: idle row with a new last_say, socket dead
    state.rows[0].activity = "idle";
    state.rows[0].last_say = "Pushed. All done here.";
    state.voiceGet = { on: true, say: "Pushed. All done here.", has_question: false, source: "hook", ts: Date.now() / 1000 };
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Pushed. All done here.") >= 0, 9000, "poll fallback say");
    check("4 s poll fallback spoke the new last_say", await said(page, "Pushed. All done here."));
    check("…bare (its own session)", await spokeBare(page, "Login fix", "Pushed. All done here."));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after plain reply");
    check("no question + no hands-free → idle", (await vstate(page)) === "idle");
    check("idle: the strip says nothing (one word only while listening, speaking, or after a hush)", (await strip(page)) === "" && (await vstate(page)) === "idle", JSON.stringify([await strip(page), await page.evaluate(() => window.mfVoice.caption)]));
    await sleep(4500);
    check("poll does not re-speak the same say", (await saidAll(page)).split("Pushed. All done here.").length === 2);

    // ---- Scenario 2: clarify + dialog → spoke the question → "yes" → /answer
    state.dialog = { id: "d1", parsed: true, question: "Allow git push?", command: "git push origin main", options: [{ key: "1", label: "Yes", kind: "yes" }, { key: "2", label: "Yes, and don't ask again", kind: "always" }, { key: "3", label: "No", kind: "no" }] };
    await page.evaluate(() => window.mfVoice.onEvent({ seq: 7, event: "session.activity_changed", session: "login-fix", old: "working", new: "clarify", ts: Date.now() / 1000, data: {} }));
    await waitFor(page, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog listening");
    const dline = await saidAll(page);
    check("spoke the dialog question, bare", dline.indexOf("Allow git push?") >= 0 && dline.indexOf("Login fix: Allow") < 0, dline.slice(-200));
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
    check("other-only options are numbered: 'Say one, two, or three.'", d5.indexOf("Which branch? one: main. two: develop. three: release. Say one, two, or three.") > 0, d5.slice(-200));
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

    // ---- "Which one?": the router asks back, the next phrase is the pick
    state.rows.push({ title: "sitecheck-bot7", name: "sitecheck-bot7", activity: "working", status: "running", last_turn: "", last_say: null, voice: false });
    state.rows.push({ title: "sitecheck-bot7-copy-2", name: "sitecheck-bot7-copy-2", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false });
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for the sitecheck switch");
    await page.evaluate(() => window.__hear("switch to the sitecheck session"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?") >= 0, 3000, "which one");
    check("pick: 'switch to the sitecheck session' → the router's 'Which one: a or b?'", await said(page, "Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?"));
    await waitFor(page, () => window.mfVoice.state === "listening", 4000, "listening for the pick");
    check("pick: …and the mic opens for the answer", (await vstate(page)) === "listening");
    await page.evaluate(() => window.__hear("copy two"));
    await waitFor(page, () => window.mfVoice.session === "sitecheck-bot7-copy-2", 5000, "picked");
    const pickPost = posts("/voice/route").filter((x) => x.body.text === "copy two")[0];
    check("pick: 'copy two' went back with the pending candidates", !!pickPost && pickPost.body.pending && pickPost.body.pending.kind === "pick" && pickPost.body.pending.candidates.length === 2, JSON.stringify(pickPost && pickPost.body));
    check("pick: …and switched to the copy", (await page.evaluate(() => window.mfVoice.session)) === "sitecheck-bot7-copy-2");
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after pick");
    // Any answer without a pending drops it; and a pending older than 30 s goes unsent.
    check("pick: the switch answer dropped the pending choice", (await page.evaluate(() => window.mfVoice.routePending)) === null);
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for the second ask");
    await page.evaluate(() => window.__hear("switch to the sitecheck session"));
    await waitFor(page, () => window.mfVoice.state === "listening" && !!window.mfVoice.routePending, 5000, "asked again");
    await page.evaluate(() => { window.mfVoice.routePending.at -= 31000; });
    await page.evaluate(() => window.__hear("the first one"));
    for (let t = Date.now(); !posts("/voice/route").some((x) => x.body.text === "the first one") && Date.now() - t < 3000; ) await sleep(30);
    const latePost = posts("/voice/route").filter((x) => x.body.text === "the first one")[0];
    check("pick: a choice older than 30 s is not sent (the phrase routes as usual)", !!latePost && !latePost.body.pending, JSON.stringify(latePost && latePost.body));
    // The late phrase was words (the mock's prompt): wait for them to land, then empty the box.
    await waitFor(page, () => document.getElementById("compose").value.indexOf("the first one") >= 0, 3000, "late phrase typed");
    await page.click("#voice-cancel");
    await page.evaluate(() => { document.getElementById("compose").value = ""; });
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after the late phrase");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening to leave the copy");
    await page.evaluate(() => window.__hear("switch to api docs"));
    await waitFor(page, () => window.mfVoice.session === "api-docs" && window.mfVoice.state === "idle", 5000, "back on API docs before the rows go");
    state.rows = state.rows.filter((r) => !/^sitecheck/.test(r.title));

    // ---- "start a new session": the project, then the task, then it starts
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for the new session");
    await page.evaluate(() => window.__hear("start a new session"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("In which project? MindFlock or parser?") >= 0, 5000, "which project");
    await waitFor(page, () => window.mfVoice.state === "listening", 4000, "listening for the project");
    check("new: 'start a new session' asks which project, and listens for it", (await said(page, "In which project? MindFlock or parser?")) && (await vstate(page)) === "listening");
    await page.evaluate(() => window.__hear("parser"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("What should it do in parser?") >= 0, 3000, "what should it do");
    await waitFor(page, () => window.mfVoice.state === "listening", 4000, "listening for the task");
    check("new: …then what it should do", (await vstate(page)) === "listening");
    await page.evaluate(() => window.__hear("add tests for the queue"));
    await waitFor(page, () => window.mfVoice.session === "add-tests-for-the-queue", 5000, "new session selected");
    const newPosts = posts("/voice/route").filter((x) => x.body.text === "parser" || x.body.text === "add tests for the queue");
    check("new: each answer carried the gathered fields back", newPosts.length === 2 && newPosts[0].body.pending.await === "repo" && newPosts[1].body.pending.await === "task" && newPosts[1].body.pending.repo_name === "parser", JSON.stringify(newPosts.map((x) => x.body.pending)));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Started add-tests-for-the-queue in parser.") >= 0, 3000, "started");
    check("new: …and it started and is selected", (await page.evaluate(() => window.mfVoice.session)) === "add-tests-for-the-queue" && (await spoken(page)).includes("Started add-tests-for-the-queue in parser."));
    check("new: the task was never typed as a prompt", posts("/send").every((x) => x.body.text.indexOf("queue") < 0));
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after new");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening for the cancelled new session");
    await page.evaluate(() => window.__hear("start a new session"));
    await waitFor(page, () => window.mfVoice.state === "listening" && !!window.mfVoice.routePending, 5000, "asked for the project again");
    const rowsBefore = state.rows.length;
    await page.evaluate(() => window.__hear("never mind"));
    await waitFor(page, () => window.__spoken.join(" ").indexOf("Okay, no new session.") >= 0, 3000, "cancelled");
    check("new: 'never mind' drops it — said, nothing created, no pending kept", state.rows.length === rowsBefore && (await page.evaluate(() => window.mfVoice.routePending)) === null);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after cancel");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening to go back");
    await page.evaluate(() => window.__hear("switch to api docs"));
    await waitFor(page, () => window.mfVoice.session === "api-docs" && window.mfVoice.state === "idle", 5000, "back on API docs again");
    state.rows = state.rows.filter((r) => r.title !== "add-tests-for-the-queue");
    await sleep(4600);   // the page's row list refreshes on its 4 s poll: the scenarios below see the rows they expect

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
    // the silence stop (2 minutes without a word) of a listen that is not
    // continuous (a reply's question, hands-free): the words stay, unsent
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle");
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    check("a tap's listen is continuous", await page.evaluate(() => window.mfVoice.listen.cont === true));
    await page.evaluate(() => { window.mfVoice.listen.cont = false; });   // as an auto-listen is
    await page.evaluate(() => window.__hear("add a test for the parser"));
    await waitFor(page, () => document.getElementById("compose").value === "add a test for the parser", 3000, "phrase in the box");
    check("a phrase without 'over' sits in the box, still listening", (await vstate(page)) === "listening" && (await page.evaluate(() => document.getElementById("compose").value)) === "add a test for the parser");
    await page.evaluate(() => { window.mfVoice.listen.until = 0; window.__silence(); });   // the window ran out
    await sleep(300);
    check("silence stops listening and keeps the text, unsent", (await vstate(page)) === "idle" && (await page.evaluate(() => document.getElementById("compose").value)) === "add a test for the parser" && posts("/send").length === sendsBefore);
    check("…and says so", /Kept, not sent/.test(await page.evaluate(() => window.mfVoice.caption)));
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
    // ---- push to talk, tap mode: the second tap stops — it keeps the box, never sends
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("rebuild the docs index"));
    await waitFor(page, () => document.getElementById("compose").value === "rebuild the docs index", 3000, "phrase in the box");
    let nSends = posts("/send").length;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "second tap stopped");
    await sleep(200);
    check("tap mode: the second tap keeps the words, unsent", (await vstate(page)) === "idle" && posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "rebuild the docs index");
    check("…'In the box — say over, or tap Send'", (await page.evaluate(() => window.mfVoice.caption)) === "In the box — say “over”, or tap Send", await page.evaluate(() => window.mfVoice.caption));
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button after the tap");
    check("…then the Send button sends it (dictated: say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "rebuild the docs index" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await idleEvent("Index rebuilt.", 15);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after index");
    check("…its reply is spoken, bare (the phone is on API docs)", (await spoken(page)).includes("Index rebuilt.") && !(await said(page, "API docs: Index")));
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
    check("…the strip says 'Your turn'", (await page.evaluate(() => window.mfVoice.caption)) === "Your turn");
    // ---- Spoken replies off: dictated prompts ask for no Say line either, and the phone stays quiet
    await page.evaluate(() => { const el = document.getElementById("voice-replies"); el.value = "off"; el.dispatchEvent(new Event("change")); });
    const sentLines = (await spoken(page)).filter((t) => t === "Sent to API docs.").length;
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("check the build over and out"));
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "replies-off send");
    check("Spoken replies off → /send say:false even when dictated", lastSend().body.text === "check the build" && lastSend().body.say === false, JSON.stringify(lastSend().body));
    await sleep(300);
    check("…'Sent to' is shown, not spoken", (await spoken(page)).filter((t) => t === "Sent to API docs.").length === sentLines && (await page.evaluate(() => window.mfVoice.caption)) === "Sent to API docs.");
    check("…but the confirmation still is (it is about what you did, not the reply)", (await saidAll(page)).slice(-40).indexOf("Sent. Over and out.") >= 0, JSON.stringify((await spoken(page)).slice(-3)));
    await idleEvent("Build is green.", 17);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after replies off");
    await sleep(200);
    check("…and the reply is not spoken", !(await said(page, "Build is green.")));
    await page.evaluate(() => { const el = document.getElementById("voice-replies"); el.value = "mine"; el.dispatchEvent(new Event("change")); });
    state.sendStatus = 409; state.sendReply = { error: "session is waking up", waking: true };
    await page.click("#voice-btn");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "listening");
    await page.evaluate(() => window.__hear("hello there over and out"));
    await waitFor(page, () => window.__spoken.includes("API docs is waking up, try again in a moment."), 5000, "waking line");
    check("409 waking → spoken reason", (await spoken(page)).includes("API docs is waking up, try again in a moment."));
    check("…text stays in the box", (await page.evaluate(() => document.getElementById("compose").value)) === "hello there");
    check("'hello there over and out' into an empty box was routed (the router said prompt) as 'hello there'", posts("/voice/route").some((x) => x.body.text === "hello there"));
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
    // ---- push to talk, hold mode: press to talk, let go to keep; "over" sends (and ends the hold's listen)
    await page.evaluate(() => { const el = document.getElementById("voice-ptt"); el.value = "hold"; el.dispatchEvent(new Event("change")); });
    check("hold mode: the strip says 'Hold the mic to talk'", (await page.evaluate(() => window.mfVoice.caption)) === "Hold the mic to talk");
    const mic = await page.locator("#voice-btn").boundingBox();
    await page.mouse.move(mic.x + mic.width / 2, mic.y + mic.height / 2);
    await page.mouse.down();
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening");
    check("pointerdown on the mic → listening", (await vstate(page)) === "listening");
    const holdHint = await page.evaluate(() => window.mfVoice.caption);
    check("…the hint: 'Release to keep it; say over to send'", holdHint.indexOf("Release to keep it; say “over” to send") >= 0, holdHint);
    check("…a hold is never continuous", await page.evaluate(() => window.mfVoice.listen.cont === false));
    await page.evaluate(() => window.__hear("deploy the docs"));
    await waitFor(page, () => document.getElementById("compose").value === "deploy the docs", 3000, "hold phrase");
    nSends = posts("/send").length;
    await page.evaluate(() => { window.__onStopFinal = "and tag it"; });   // still on its way when the finger lifts
    await page.mouse.up();
    await waitFor(page, () => window.mfVoice.state === "idle" && document.getElementById("compose").value === "deploy the docs and tag it", 4000, "hold kept");
    check("pointerup keeps, after waiting for the last phrase — never sends", posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "deploy the docs and tag it", await page.evaluate(() => document.getElementById("compose").value));
    await sleep(300);
    check("…and the release's click starts nothing", (await vstate(page)) === "idle" && /^In the box/.test(await page.evaluate(() => window.mfVoice.caption)));
    await page.dispatchEvent("#send", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "working", 3000, "send button after the hold");
    check("…the Send button sends it (say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "deploy the docs and tag it" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await idleEvent("Deployed and tagged.", 18);
    await waitFor(page, () => window.mfVoice.state === "idle", 3000, "idle after hold");
    check("…its reply is spoken, bare", (await spoken(page)).includes("Deployed and tagged.") && !(await said(page, "API docs: Deployed")));
    nSends = posts("/send").length;
    await page.mouse.down();
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 2");
    await page.mouse.up();
    await sleep(400);
    check("a release with nothing heard just stops", (await vstate(page)) === "idle" && posts("/send").length === nSends && (await page.evaluate(() => window.mfVoice.caption)) === "Hold the mic to talk");
    await page.dispatchEvent("#voice-btn", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 3");
    await page.evaluate(() => window.__hear("ship it"));
    await waitFor(page, () => document.getElementById("compose").value === "ship it", 3000, "hold phrase 3");
    nSends = posts("/send").length;
    await page.dispatchEvent("#voice-btn", "pointercancel");
    await waitFor(page, () => window.mfVoice.state === "idle", 4000, "pointercancel kept");
    check("pointercancel keeps like a release", posts("/send").length === nSends && (await page.evaluate(() => document.getElementById("compose").value)) === "ship it");
    // A press that says "over": the kept words go, with say, and the hold's listen ends.
    await page.dispatchEvent("#voice-btn", "pointerdown");
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "hold listening 4");
    await page.evaluate(() => window.__hear("now over"));
    await waitFor(page, () => window.mfVoice.state === "working", 4000, "hold over sent");
    check("holding, 'over' sends the box (say:true)", posts("/send").length === nSends + 1 && lastSend().body.text === "ship it now" && lastSend().body.say === true, JSON.stringify(lastSend().body));
    await page.dispatchEvent("#voice-btn", "pointercancel");   // the finger lifts after: nothing more
    await sleep(200);
    check("…the release after an over changes nothing", (await vstate(page)) === "working" && posts("/send").length === nSends + 1);
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
    check("localStorage holds session/on/rate/voiceName/handsFree/ptt/replies/wake/confirm/sounds only", stored && Object.keys(stored).sort().join(",") === "confirm,handsFree,on,ptt,rate,replies,session,sounds,voiceName,wake" && stored.on === false && stored.ptt === "toggle" && stored.replies === "mine" && stored.wake === false && stored.confirm === true && stored.sounds === true, JSON.stringify(stored));

    // ---- the sheet: diagnostics + voices
    await page.evaluate(() => document.getElementById("voice-btn").click());
    await waitFor(page, () => window.mfVoice.state === "listening", 3000, "on again");
    await page.click("#voice-gear");
    await sleep(200);
    const diag = await page.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics name recognition / speech / mode / wake lock / socket", /Recognition: available/.test(diag) && /Speech: available, 3 voices/.test(diag) && /Mode: browser tab, Android/.test(diag) && /Wake lock: (held|supported)/.test(diag) && /Events socket: /.test(diag), diag.replace(/\n/g, " | "));
    check("diagnostics: Replies / Push to talk / Last prompt", /Replies: to what you said by voice/.test(diag) && /Push to talk: tap/.test(diag) && /Last prompt: dictated, say yes/.test(diag), diag.replace(/\n/g, " | "));
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
    check("…bare (the session the link opened)", await spokeBare(page2, "API docs", "Docs rebuilt. Anything else?"));
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
    state.transcribeReply = { text: "fix the parser over and out", backend: "builtin", ms: 90 };
    state.transcribes = [];
    const sends3 = posts("/send").length;
    const page3 = await ctx.newPage();
    page3.on("pageerror", (e) => errors.push(String(e)));
    await page3.addInitScript(() => {
      window.__gum = 0; window.__stops = 0;
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
        // The page giving the microphone back = its tracks stopped.
        dst.stream.getTracks().forEach((t) => { const stop = t.stop.bind(t); t.stop = () => { window.__stops++; stop(); }; });
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
    const lvl = () => page3.evaluate(() => ["voice-level", "voice-btn", "asking-mic"].map((id) => document.getElementById(id).style.getPropertyValue("--pick")));
    const level = await lvl();
    check("the mic's ring steps up with the voice (--pick: 0.5 hearing / 1 loud — one of three steps)", ["0.5", "1"].includes(level[1]), JSON.stringify(level));
    check("…the ask sheet's mic and the meter take the same step", level[0] === level[1] && level[2] === level[1], JSON.stringify(level));
    check("…the mic is marked as hearing", await page3.evaluate(() => document.getElementById("voice-btn").hasAttribute("data-hearing")));
    const still = await page3.evaluate(() => {
      const b = document.getElementById("voice-btn"), cs = getComputedStyle(b), ring = getComputedStyle(b, "::after");
      return { anim: cs.animationName, tf: cs.transform, ringTf: ring.transform, ringOp: ring.opacity, ringTr: ring.transitionProperty + " " + ring.transitionDuration, meter: document.getElementById("voice-level").classList.contains("hidden") };
    });
    check("the mic is still: no pulse, no scale — a thin ring whose opacity alone eases (400 ms)", still.anim === "none" && still.tf === "none" && still.ringTf === "none" && /opacity 0\.4s/.test(still.ringTr) && parseFloat(still.ringOp) > 0.5, JSON.stringify(still));
    check("…and the level meter stays hidden while the ⚙ sheet is closed", still.meter, JSON.stringify(still));
    const capH = await page3.evaluate(() => window.mfVoice.caption);
    check("the loop's caption (the ask sheet's line) says 'Hearing you…' while the voice comes in — the strip only 'Listening…'", /^Hearing you…/.test(capH) && (await page3.evaluate(() => document.getElementById("voice-caption").textContent)) === "Listening…", capH);
    await page3.evaluate(() => window.__tone(0.002));
    await sleep(650);   // past VOICE_HEARING_MS, short of the silence stop
    const capL = await page3.evaluate(() => window.mfVoice.caption);
    const level2 = await lvl();
    check("…and 'Listening…' once it is quiet", /^Listening… \d+ s/.test(capL), capL);
    check("…the ring steps down as it goes quiet (smoothed: no flicker)", parseFloat(level2[1]) <= 0.5, JSON.stringify(level2));
    // VOICE_SILENCE_MS of quiet after speech ends the clip by itself.
    const t0 = Date.now();
    while (!state.transcribes.length && Date.now() - t0 < 6000) await sleep(50);
    const tr = state.transcribes[0] || {};
    check("silence after speech ended the clip: one POST /api/voice/transcribe", state.transcribes.length === 1, JSON.stringify(state.transcribes));
    check("…Content-Type audio/wav", tr.ctype === "audio/wav", tr.ctype);
    check("…the body is a RIFF/WAVE file at 16 kHz", tr.head === "RIFF" && tr.wave === "WAVE" && tr.rate === 16000 && tr.dataSize === tr.len - 44 && tr.len > 44 + 16000, JSON.stringify(tr));
    await waitFor(page3, () => window.mfVoice.state === "working", 5000, "server words sent");
    const sent3 = posts("/send").slice(sends3);
    check("the server's 'fix the parser over and out' went through the radio rules: sent", sent3.length === 1 && sent3[0].body.text === "fix the parser" && sent3[0].path.includes("invoice-parser"), JSON.stringify(sent3));
    check("the microphone was asked for once", (await page3.evaluate(() => window.__gum)) === 1);
    await page3.click("#voice-gear");
    await sleep(200);
    const diag3 = await page3.evaluate(() => document.getElementById("voice-diag").textContent);
    check("diagnostics: the WAV recorder and the server's built-in engine", /Recorder: WAV 16 kHz \(ScriptProcessor\)/.test(diag3) && /Built-in engine: Windows — System\.Speech \(en-US\)/.test(diag3) && /Last transcribe: 90 ms via builtin/.test(diag3), diag3.replace(/\n/g, " | "));
    check("diagnostics: the clip went through the browser's resampler; the silence stop is 1100 ms", /Resampler: OfflineAudioContext \(the browser's\)$/m.test(diag3) && /Silence stop: 1100 ms after speech/.test(diag3), diag3.replace(/\n/g, " | "));
    check("diagnostics: the warm microphone and the last start-up trace", /^Microphone: warm \(kept 90 s after the last phrase\)$/m.test(diag3) && /^Last start: mic \d+ ms, speech \d+ ms after the start$/m.test(diag3), diag3.replace(/\n/g, " | "));
    await page3.click("#voice-done");
    // ---- the warm microphone: kept after the phrase, reused by the next
    // tap (no getUserMedia), given back once the grace runs out.
    await page3.click("#voice-cancel");
    await waitFor(page3, () => window.mfVoice.state === "idle", 3000, "server idle");
    check("after the phrase the microphone stays warm (its tracks running)", (await page3.evaluate(() => window.mfVoice.stt.warm() && window.__stops === 0)));
    const vib3 = await page3.evaluate(() => window.__vibes);
    await page3.click("#voice-btn");
    await waitFor(page3, () => window.mfVoice.state === "listening" && window.mfVoice.rec.trace && "mic" in window.mfVoice.rec.trace, 3000, "warm listening");
    const tr3 = await page3.evaluate(() => window.mfVoice.rec.trace);
    check("…the next tap reused it: no second getUserMedia, the mic ready at once", (await page3.evaluate(() => window.__gum)) === 1 && tr3.mic <= 50, JSON.stringify(tr3));
    await sleep(900);   // past the listening buzz's gap
    await page3.evaluate(() => window.__tone(0.4));
    await waitFor(page3, () => "speech" in window.mfVoice.rec.trace, 2000, "the first word");
    await sleep(600);   // more loud blocks: still one tick
    check("vibrate: the listening buzz, then ONE tick at the phrase's first word", (await page3.evaluate(() => window.__vibes)) - vib3 === 2, String((await page3.evaluate(() => window.__vibes)) - vib3) + " " + JSON.stringify(await page3.evaluate(() => [window.mfVoice.rec.trace, window.mfVoice.buzzAt])));
    await page3.evaluate(() => window.__tone(0.002));
    await page3.click("#voice-cancel");
    await waitFor(page3, () => window.mfVoice.state === "idle", 3000, "idle again");
    const vib3b = await page3.evaluate(() => window.__vibes);
    await page3.click("#voice-btn");
    await waitFor(page3, () => window.mfVoice.state === "listening", 3000, "listening, speaking at once");
    await sleep(120);
    await page3.evaluate(() => window.__tone(0.4));
    await sleep(500);
    check("…no tick on top of the listening buzz (speech right after the tap)", (await page3.evaluate(() => window.__vibes)) - vib3b === 1 && (await page3.evaluate(() => "speech" in window.mfVoice.rec.trace)), String((await page3.evaluate(() => window.__vibes)) - vib3b));
    await page3.evaluate(() => window.__tone(0.002));
    // The grace, shortened: past it the tracks stop and the next tap asks again.
    await page3.evaluate(() => { window.mfVoice.keepMs = 300; });
    await page3.click("#voice-cancel");
    await waitFor(page3, () => window.mfVoice.state === "idle", 3000, "idle before the grace");
    await waitFor(page3, () => !window.mfVoice.stt.warm(), 3000, "the mic released");
    check("past the grace the microphone is given back (tracks stopped)", (await page3.evaluate(() => !window.mfVoice.stt.warm() && window.__stops >= 1)));
    await page3.click("#voice-btn");
    await waitFor(page3, () => window.mfVoice.state === "listening" && window.__gum === 2, 3000, "cold listening");
    check("…and the next tap asks for it again", (await page3.evaluate(() => window.__gum)) === 2);
    await page3.click("#voice-cancel");
    await page3.close();

    // ---- streaming recognition: the words appear while the phrase is spoken
    state.voiceCfg = {
      stt: "server", tts: "phone", approve_dialogs: true,
      server_ready: { stt: true, tts: false, stt_backend: "whisper", stt_stream: true },
      backends: { stt: ["whisper"], tts: [] },
    };
    state.voiceGet = { on: false, say: null, has_question: false, source: "none", ts: null };
    state.transcribes = [];
    // Phrase 1 into the empty box (it can't be a command: typed as it
    // comes, the router reads its final words); phrase 2 streams into the
    // box, corrected as it goes, and ends in "over and out".
    state.streamScript = {
      s1: [{ committed: "", partial: "fix the" }, { committed: "fix the", partial: "parser" }, { committed: "parser", partial: "" }],
      s2: [{ committed: "and add", partial: "a" }, { committed: "a test", partial: "over and out" }],
    };
    state.streamEndReply = { s2: { committed: "over and out" } };
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
      // Every value the box and the caption take, in order (and the
      // caption's greyed, still-settling words).
      window.__boxes = []; window.__caps = []; window.__parts = [];
      setInterval(() => {
        const b = document.getElementById("compose"), c = document.getElementById("voice-caption");
        if (b && b.value !== window.__lastBox) { window.__lastBox = b.value; window.__boxes.push(b.value); }
        if (c && c.textContent !== window.__lastCap) {
          window.__lastCap = c.textContent; window.__caps.push(c.textContent);
          const sp = c.querySelector(".voice-partial");
          window.__parts.push(sp ? sp.textContent : null);
        }
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
    const tr4q = await page4.evaluate(() => window.mfVoice.rec.trace);
    check("…opened at the start, before any word (the trace: open, no speech yet)", "open" in tr4q && !("speech" in tr4q), JSON.stringify(tr4q));
    check("…and no quiet uploaded: no chunk before the first word", state.streamChunks.length === 0, JSON.stringify(state.streamChunks));
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(1300);
    await page4.evaluate(() => window.__tone(0.002));
    const tr4 = await page4.evaluate(() => window.mfVoice.rec.trace);
    const c4 = state.streamChunks.filter((c) => c.id === "s1");
    // The first chunk is the lead-in (VOICE_LEAD_MS) + the first loud block(s): it goes as the word starts.
    check("the first chunk went as the word started: words shown within 400 ms of the first loud block", tr4.words - tr4.speech < 400 && c4.length >= 1 && c4[0].len < 44 + 32000 * 0.7, JSON.stringify(tr4) + " first chunk " + (c4[0] && c4[0].len) + " B");
    check("…and typed into the box in the same moment (the trace's 'typed' mark, each pass's ms listed)", tr4.typed - tr4.words < 50 && Array.isArray(tr4.passes) && tr4.passes[0] === 42, JSON.stringify(tr4));
    console.log("  trace (phrase 1): " + JSON.stringify(tr4));
    t4 = Date.now();
    while (state.streamOpens.length < 2 && Date.now() - t4 < 6000) await sleep(30);
    // The next phrase's stream opens as listening restarts — before the
    // router's answer has put phrase 1 in the box.
    await waitFor(page4, () => window.__boxes.some(Boolean), 3000, "phrase 1 in the box");
    const box1 = await page4.evaluate(() => window.__boxes.filter(Boolean));
    const caps1 = await page4.evaluate(() => window.__caps.slice());
    const STRIP_WORDS = ["", "Listening…", "Speaking…", "Quiet."];
    const parts1 = await page4.evaluate(() => window.__parts.slice());
    check("…the strip never showed the words while they streamed — only its one word (no transcript, no 'Hearing you…', no greyed partial)", caps1.length >= 1 && caps1.every((c) => STRIP_WORDS.includes(c)) && caps1.includes("Listening…") && parts1.every((x) => x === null), JSON.stringify([caps1, parts1]));
    // The session's last Say: line asked a question, so "the" waits a word
    // (it may start "the first").
    check("…typed into the box as it came — the first pass's guess (its 'the' held back: the session is asking), then corrected by the next", box1[0] === "fix" && box1[1] === "fix the parser" && state.streamEnds[0] === "s1", JSON.stringify([box1, state.streamEnds]));
    check("…the router had the whole phrase", state.posts.some((x) => x.path === "/api/voice/route" && x.body.text === "fix the parser"));
    const all1 = await page4.evaluate(() => window.__boxes.slice());
    check("…and the words never blinked out while it read them (the guess stayed until the answer)", all1.indexOf("fix") >= 0 && all1.slice(all1.indexOf("fix")).every(Boolean), JSON.stringify(all1));
    check("…the next phrase opened a new stream", state.streamOpens.length === 2);
    await sleep(300);
    const vib4 = await page4.evaluate(() => window.__vibes);
    await page4.evaluate(() => window.__tone(0.4));
    await sleep(1300);
    check("vibrate: phrase 2 (no listening buzz of its own) ticked once at its first word", (await page4.evaluate(() => window.__vibes)) - vib4 === 1, String((await page4.evaluate(() => window.__vibes)) - vib4));
    await page4.evaluate(() => window.__tone(0.002));
    await waitFor(page4, () => window.mfVoice.state === "working" || window.mfVoice.state === "sending", 6000, "stream words sent");
    const boxes4 = await page4.evaluate(() => window.__boxes.slice());
    const caps4 = await page4.evaluate(() => window.__caps.slice());
    const i1 = boxes4.indexOf("fix the parser and add a"), i2 = boxes4.indexOf("fix the parser and add a test");
    check("phrase 2 typed into the box as it came — the unstable tail too — after the words already there", i1 >= 0 && i2 > i1, JSON.stringify(boxes4));
    check("…and the strip still said only its word (the DOM: one text node, no partial span)", caps4.every((c) => STRIP_WORDS.includes(c)) && (await page4.evaluate(() => { const c = document.getElementById("voice-caption"); return c.children.length === 0 && c.offsetHeight < 30; })), JSON.stringify(caps4));
    check("…'over and out' never reached the box (held back, then acted on)", !boxes4.some((b) => /\bover\b|\bout\b/i.test(b)), JSON.stringify(boxes4));
    const sent4 = posts("/send").slice(sends4);
    check("…the phrase ending in 'over and out' sent the box without the words", sent4.length === 1 && sent4[0].body.text === "fix the parser and add a test", JSON.stringify(sent4));
    check("chunks went one at a time (never two in flight)", state.streamChunks.length >= 4 && state.streamMaxInflight === 1, state.streamChunks.length + " chunks, max in flight " + state.streamMaxInflight);
    check("…each a 16 kHz WAV", state.streamChunks.every((c) => c.ctype === "audio/wav" && c.head === "RIFF" && c.rate === 16000), JSON.stringify(state.streamChunks.slice(0, 3)));
    // Pacing: a stream's first chunk is at least 300 ms (padded before its
    // audio); with a 42 ms pass the later ones need only 150 ms of new
    // audio — never the old 300 ms floor — and none is padded.
    const ids4 = ["s1", "s2"].map((id) => state.streamChunks.filter((c) => c.id === id));
    const later4 = [].concat.apply([], ids4.map((cs) => cs.slice(1)));
    check("…a stream's first chunk ≥ 300 ms; the later ones ≥ 150 ms, some under 300 ms (paced by the 42 ms pass)", ids4.every((cs) => cs.length && cs[0].len >= 44 + 9600) && later4.length >= 2 && later4.every((c) => c.len >= 44 + 4800) && later4.some((c) => c.len < 44 + 9600), JSON.stringify(state.streamChunks.map((c) => c.id + ":" + c.len)));
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
    check("diagnostics: words as you speak, the pass time, the last chunk's time", /Recognition stream: words as you speak \(~0\.04 s\/pass, last chunk 42 ms, 0\.4 s of audio\)/.test(diag4), diag4.replace(/\n/g, " | "));
    await page4.click("#voice-done");
    // A 409 on the open: the phrase is a clip, and the page stops asking.
    state.streamOpenStatus = 409;
    state.transcribeReply = { text: "check the logs, that's all", backend: "whisper", ms: 80 };
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
    state.transcribeReply = { text: "logs that's all", backend: "whisper", ms: 70 };
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
    // ---- the accurate final: /end's `final` replaces the phrase typed so far
    // (only it — the words before stay) BEFORE the radio rules read it;
    // `same` skips the retype; no `final` keeps the fast words.
    const finalRun = async (reply, done) => {
      const sid = "s" + (state.streamOpens.length + 1);
      state.streamScript[sid] = [{ committed: "", partial: "fix the bag" }, { committed: "fix the bag", partial: "" }];
      state.streamEndReply[sid] = reply;
      await page4.evaluate(() => { document.getElementById("compose").value = "keep this"; window.__boxes = []; window.__lastBox = null; window.mfVoice.cfg.server_ready.stt_stream = true; });
      await page4.click("#voice-btn");
      await waitFor(page4, () => window.mfVoice.state === "listening", 3000, "listening for the final");
      await sleep(400);
      await page4.evaluate(() => window.__tone(0.4));
      await sleep(900);
      await page4.evaluate(() => window.__tone(0.002));
      const tF = Date.now();
      while (state.streamEnds.indexOf(sid) < 0 && Date.now() - tF < 6000) await sleep(30);
      await waitFor(page4, done || (() => window.mfVoice.state === "idle"), 4000, "final kept");
      return { box: await page4.evaluate(() => document.getElementById("compose").value), boxes: await page4.evaluate(() => window.__boxes.slice()) };
    };
    const fx = await finalRun({ committed: "that's all", final: "fix the bug that's all", final_ms: 1200, same: false });
    check("final: a different final replaces the whole typed phrase (the words before it stay), then the stop phrase keeps it", fx.box === "keep this fix the bug" && fx.boxes.includes("keep this fix the bag") && fx.boxes.every((b) => b.indexOf("keep this") === 0 && !/that's/.test(b)), JSON.stringify(fx));
    await page4.click("#voice-gear");
    await sleep(200);
    const diagF = await page4.evaluate(() => document.getElementById("voice-diag").textContent);
    check("final: …Diagnostics says 'final 1.2 s (corrected)' once", (diagF.match(/final 1\.2 s \(corrected\)/g) || []).length === 1, diagF.replace(/\n/g, " | "));
    await page4.click("#voice-done");
    const fs = await finalRun({ committed: "that's all", final: "fix the bug that's all", final_ms: 900, same: true });
    check("final: `same` → no retype, the fast words stand", fs.box === "keep this fix the bag", JSON.stringify(fs));
    const fa = await finalRun({ committed: "that's all" });
    check("final: no `final` (an older server) → the fast words stand", fa.box === "keep this fix the bag", JSON.stringify(fa));
    const fd = await finalRun({ committed: "", final: "fix the bug over and out", final_ms: 800, same: false }, () => ["sending", "working"].includes(window.mfVoice.state));
    const sentF = posts("/send").slice(-1)[0];
    check("final: …the rules read the final words ('over and out' in the final alone sends the corrected box)", sentF && sentF.body.text === "keep this fix the bug", JSON.stringify([fd, sentF && sentF.body]));
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
      // (the recognizer too: a tap that cut a line — "Stopped." — opens the mic a moment later)
      return waitFor(page5, () => (window.mfVoice.state === "listening" || window.mfVoice.state === "awaiting") && window.__rec && window.__rec.started, 3000, label);
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
    check("…and the strip says how to send", /^In the box/.test(await page5.evaluate(() => window.mfVoice.caption)));
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

    // ---- a session is asking you something: the ask sheet ------------------
    // Rows carry `asking` (the newer server); the pop-up is on; Notification
    // is stubbed "granted" and visibility is switchable, so a notification
    // can be told apart from a buzz.
    state.ntfy = { ask_popup: true, active: false };
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    state.dialog = null;
    state.dialogsBy = {};
    state.sendStatus = 0; state.sendReply = null;
    state.answerStatus = 0; state.answerReply = null;
    const now6 = () => Date.now() / 1000;
    state.rows = [
      { title: "login-fix", name: "Login fix", activity: "working", status: "running", last_turn: "", last_say: null, voice: false, asking: null },
      { title: "invoice-parser", name: "Invoice parser", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false, asking: null },
      { title: "api-docs", name: "API docs", activity: "idle", status: "running", last_turn: "", last_say: null, voice: false, asking: null },
    ];
    const PERM = (id) => ({ id, parsed: true, kind: "dialog", heading: "Bash command", question: "Allow git push?", command: "git push origin main",
      options: [{ key: "1", label: "Yes", kind: "yes", keys: ["1"] }, { key: "2", label: "Yes, and don't ask again", kind: "always", keys: ["2"] }, { key: "3", label: "No", kind: "no", keys: ["Escape"] }] });
    const page6 = await ctx.newPage();
    page6.on("pageerror", (e) => errors.push(String(e)));
    page6.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409|no worker/.test(msg.text())) errors.push(msg.text()); });
    await page6.route("**/api/**", mock);
    await page6.addInitScript(() => {
      window.__notes = [];
      class FakeNote { constructor(t, o) { this.title = t; this.opts = o || {}; window.__notes.push(this); } close() { this.closed = true; } }
      FakeNote.permission = "granted";
      FakeNote.requestPermission = () => Promise.resolve("granted");
      window.Notification = FakeNote;
      if (navigator.serviceWorker) {
        navigator.serviceWorker.register = () => Promise.reject(new Error("no worker in the harness"));
        navigator.serviceWorker.getRegistration = () => Promise.resolve(undefined);
      }
      window.__hidden = false;
      Object.defineProperty(document, "visibilityState", { get() { return window.__hidden ? "hidden" : "visible"; }, configurable: true });
    });
    await page6.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page6, () => window.mfAsk && window.mfAsk.polls >= 1 && document.getElementById("picker-title").textContent === "Login fix", 6000, "ask page loaded");
    const open6 = () => page6.evaluate(() => !document.getElementById("asking-sheet").classList.contains("hidden"));
    const txt6 = (id) => page6.evaluate((i) => document.getElementById(i).textContent, id);
    const shown6 = (id) => page6.evaluate((i) => !document.getElementById(i).classList.contains("hidden"), id);
    const posts6 = (p) => state.posts.filter((x) => x.path.endsWith(p));
    // An ask event fetches the rows now (not at the next 4 s tick).
    const kick6 = (t, text, ts) => page6.evaluate((a) => window.mfVoice.onEvent({ seq: 0, event: "session.question", session: a[0], ts: Date.now() / 1000, data: { title: a[0], text: a[1], ts: a[2], kind: "question" } }), [t, text, ts]);
    const waitOpen6 = (label, ms) => waitFor(page6, () => !document.getElementById("asking-sheet").classList.contains("hidden"), ms || 6000, label);
    const waitShut6 = (label) => waitFor(page6, () => document.getElementById("asking-sheet").classList.contains("hidden"), 4000, label);
    // The explicit open: the Sessions sheet, then the session's row.
    const openRow6 = async (t) => {
      await page6.click("#picker");
      await waitFor(page6, () => !document.getElementById("sessions-sheet").classList.contains("hidden"), 2000, "sessions sheet");
      await page6.click('#session-list .sess-row[data-title="' + t + '"] .sess-main');
      await waitOpen6("sheet from the " + t + " row", 3000);
    };
    check("ask: nobody asking → no sheet, plain title", !(await open6()) && (await page6.title()) === "MindFlock — Mobile", await page6.title());

    // A prose question on a row: never the pop-up (a reply saying "question"
    // is not a prompt) — its row and its notification open it.
    const vib0 = await page6.evaluate(() => window.__vibes);
    const q1 = { kind: "question", text: "Should I add tests for the parser?", ts: now6(), dialog_id: null, parsed: false, source: "" };
    state.rows[1].asking = q1;
    await waitFor(page6, () => document.title.indexOf("(1)") === 0, 6000, "the poll saw the question");
    await sleep(300);
    check("ask: a row's prose question does NOT open the sheet by itself", !(await open6()));
    const row0 = await page6.evaluate(() => { const r = document.querySelector('#session-list .sess-row[data-title="invoice-parser"]'); return r ? r.querySelector(".sess-state").textContent : null; });
    check("ask: …it keeps its Question chip", row0 === "Question", String(row0));
    await openRow6("invoice-parser");
    check("ask: …a tap on its row opens it (explicit)", await open6());
    check("ask: …centred (the sheet-center overlay)", await page6.evaluate(() => getComputedStyle(document.getElementById("asking-sheet")).alignItems === "center"));
    check("ask: name, the Sessions number, kind Question, the question", (await txt6("asking-name")) === "Invoice parser" && (await txt6("asking-num")) === "1" && (await txt6("asking-kind")) === "Question" && (await txt6("asking-question")) === q1.text,
      [await txt6("asking-num"), await txt6("asking-name"), await txt6("asking-kind"), await txt6("asking-question")].join(" | "));
    check("ask: prose → a field + Send + Open the session, no options, no stepper", (await shown6("asking-reply")) && (await shown6("asking-open")) && (await page6.evaluate(() => document.querySelectorAll(".asking-opt").length)) === 0 && !(await shown6("asking-nav")));
    check("ask: the tab title counts the asking sessions: (1)", (await page6.title()) === "(1) MindFlock — Mobile", await page6.title());
    check("ask: a new ask on a visible page buzzes", (await page6.evaluate(() => window.__vibes)) > vib0);
    check("ask: …and raises no notification while visible", (await page6.evaluate(() => window.__notes.length)) === 0);
    const row1 = await page6.evaluate(() => { const r = document.querySelector('#session-list .sess-row[data-title="invoice-parser"]'); return r ? [r.querySelector(".sess-state").textContent, (r.querySelector(".sess-ask") || {}).textContent || ""] : null; });
    check("ask: the list row says Question (not Idle) with the question under it", row1 && row1[0] === "Question" && row1[1] === q1.text, JSON.stringify(row1));
    // The answer: typed, Send → /send {text, dialog_safe}.
    await page6.fill("#asking-text", "yes, add them");
    await page6.click("#asking-send");
    await waitShut6("sheet closed after Send");
    const s6 = posts6("/send").slice(-1)[0];
    check("ask: Send → POST /send {text, dialog_safe:true} to that session", s6 && s6.path === "/api/instances/invoice-parser/send" && s6.body.text === "yes, add them" && s6.body.dialog_safe === true && !s6.body.say, JSON.stringify(s6));
    check("ask: …and the sheet closes (nothing else asking)", !(await open6()));
    await sleep(4500);
    check("ask: an answered ask does not reopen while the row lags", !(await open6()));
    state.rows[1].asking = null;

    // /send 409 in_dialog → the dialog it shows now; a tap → /answer with keys.
    const q2 = { kind: "question", text: "Run the docs build?", ts: now6(), dialog_id: null, parsed: false, source: "" };
    state.rows[2].asking = q2;
    await kick6("api-docs", q2.text, q2.ts);
    await sleep(1500);
    check("ask: a session.question event does not pop the sheet either", !(await open6()));
    await openRow6("api-docs");
    check("ask: …its row opens it", (await open6()) && (await txt6("asking-name")) === "API docs");
    state.sendStatus = 409; state.sendReply = { in_dialog: true, error: "a dialog is up" };
    state.dialogsBy["api-docs"] = PERM("dx");
    await page6.fill("#asking-text", "go ahead");
    await page6.click("#asking-send");
    await waitFor(page6, () => document.querySelectorAll(".asking-opt").length === 3, 4000, "the dialog after in_dialog");
    state.sendStatus = 0; state.sendReply = null;
    check("ask: /send 409 in_dialog → the dialog's options instead", (await page6.evaluate(() => document.querySelectorAll(".asking-opt").length)) === 3 && (await txt6("asking-kind")) === "Permission");
    check("ask: …the command in monospace, the words kept, a note why", (await txt6("asking-command")) === "git push origin main" && (await page6.evaluate(() => getComputedStyle(document.getElementById("asking-command")).fontFamily)).indexOf("mono") >= 0 && (await page6.evaluate(() => document.getElementById("asking-text").value)) === "go ahead" && /showing a prompt/.test(await txt6("asking-note")), await txt6("asking-note"));
    await page6.click('.asking-opt[data-kind="no"]');
    await waitShut6("sheet closed after the option");
    const a6 = posts6("/answer").slice(-1)[0];
    check("ask: an option tap → POST /answer {keys: opt.keys, dialog_id, by:'user'}", a6 && a6.path === "/api/instances/api-docs/answer" && JSON.stringify(a6.body) === JSON.stringify({ keys: ["Escape"], dialog_id: "dx", by: "user" }), JSON.stringify(a6));
    state.rows[2].asking = null;
    delete state.dialogsBy["api-docs"];
    await sleep(300);

    // Two dialogs (and a prose question): one sheet, "1 of 2", the oldest
    // first, the question not in it; next/prev; Later snoozes a dialog.
    const t3 = now6();
    state.rows[1].asking = { kind: "question", text: "Which docs theme?", ts: t3 - 60, dialog_id: null, parsed: false, source: "" };
    state.rows[0].activity = "clarify";
    state.rows[0].asking = { kind: "dialog", text: "Allow git push?", ts: t3 - 30, dialog_id: "d9", parsed: true, source: "" };
    state.dialogsBy["login-fix"] = PERM("d9");
    state.rows[2].asking = { kind: "dialog", text: "Allow npm publish?", ts: t3, dialog_id: "da", parsed: true, source: "" };
    state.dialogsBy["api-docs"] = Object.assign(PERM("da"), { question: "Allow npm publish?", command: "npm publish" });
    await waitFor(page6, () => !document.getElementById("asking-sheet").classList.contains("hidden") && document.getElementById("asking-step").textContent === "1 of 2", 6000, "two asks");
    check("ask: two dialogs → one sheet, '1 of 2', the oldest first, the prose question not in it", (await txt6("asking-step")) === "1 of 2" && (await txt6("asking-name")) === "Login fix" && (await shown6("asking-nav")), (await txt6("asking-step")) + " " + (await txt6("asking-name")));
    check("ask: title counts every asking session (3)", (await page6.title()) === "(3) MindFlock — Mobile", await page6.title());
    await waitFor(page6, () => document.querySelectorAll(".asking-opt").length === 3, 3000, "d9 options");
    check("ask: a parsed dialog shows its options and the mic", (await page6.evaluate(() => document.querySelectorAll(".asking-opt").length)) === 3 && (await shown6("asking-mic")) && !(await shown6("asking-reply")));
    await page6.click("#asking-next");
    check("ask: › steps to the other dialog, '2 of 2'", (await txt6("asking-name")) === "API docs" && (await txt6("asking-step")) === "2 of 2");
    await page6.click("#asking-prev");
    check("ask: ‹ steps back", (await txt6("asking-name")) === "Login fix");
    await page6.click("#asking-next");
    const dis0 = posts6("/asking/dismiss").length;
    await page6.click("#asking-later");
    await sleep(200);
    check("ask: Later on a dialog → snoozed here, no /asking/dismiss", posts6("/asking/dismiss").length === dis0);
    check("ask: …and the sheet moves to the one left, no stepper", (await open6()) && (await txt6("asking-name")) === "Login fix" && !(await shown6("asking-nav")));
    state.rows[2].asking = null;
    delete state.dialogsBy["api-docs"];
    await page6.click("#asking-later");
    await waitShut6("closed after Later on the dialog");
    check("ask: Later on a dialog snoozes it here (no server call), the sheet closes", !(await open6()) && posts6("/asking/dismiss").length === dis0);
    await sleep(4500);
    check("ask: a snoozed dialog does not pop up again", !(await open6()));
    // A tap on its row opens it anyway.
    await page6.click("#picker");
    await waitFor(page6, () => !document.getElementById("sessions-sheet").classList.contains("hidden"), 2000, "sessions sheet");
    check("ask: the Sessions sheet offers 'Notify me…' state (granted → a toggle)", /Notifications on/.test(await txt6("ask-notify-btn")), await txt6("ask-notify-btn"));
    await page6.click('#session-list .sess-row[data-title="login-fix"] .sess-main');
    await waitOpen6("sheet from the row", 3000);
    check("ask: tapping an asking row opens its sheet (even snoozed)", (await open6()) && (await txt6("asking-name")) === "Login fix");
    // 409 dialog_changed → the new prompt; Back = Later.
    await waitFor(page6, () => document.querySelectorAll(".asking-opt").length === 3, 3000, "d9 options again");
    state.answerStatus = 409; state.answerReply = { dialog_changed: true };
    state.dialogsBy["login-fix"] = Object.assign(PERM("d10"), { question: "Allow git push --force?", command: "git push --force" });
    await page6.click('.asking-opt[data-kind="yes"]');
    await waitFor(page6, () => document.getElementById("asking-command").textContent === "git push --force", 3000, "the changed prompt");
    check("ask: 409 dialog_changed → re-read, the new prompt shown with a note", (await txt6("asking-question")) === "Allow git push --force?" && /changed/.test(await txt6("asking-note")), await txt6("asking-note"));
    state.answerStatus = 0; state.answerReply = null;
    await page6.evaluate(() => history.back());
    await waitShut6("Back closed it");
    check("ask: Back = Later (closes it)", !(await open6()));
    // A new dialog id pops again; 409 dialog_answered → "Already answered".
    state.rows[0].asking = { kind: "dialog", text: "Allow git push?", ts: now6(), dialog_id: "d12", parsed: true, source: "" };
    state.dialogsBy["login-fix"] = PERM("d12");
    await waitOpen6("a new dialog id pops up");
    check("ask: a NEW dialog (new id) on a snoozed session pops up", await open6());
    await waitFor(page6, () => document.querySelectorAll(".asking-opt").length === 3, 3000, "d12 options");
    state.answerStatus = 409; state.answerReply = { dialog_answered: true };
    await page6.click('.asking-opt[data-kind="yes"]');
    await waitFor(page6, () => /Already answered/.test(document.getElementById("asking-note").textContent), 3000, "already answered");
    check("ask: 409 dialog_answered → 'Already answered', no options", /Already answered/.test(await txt6("asking-note")) && (await page6.evaluate(() => document.querySelectorAll(".asking-opt").length)) === 0);
    state.answerStatus = 0; state.answerReply = null;
    await page6.mouse.click(8, 8);   // the dimmed backdrop
    await waitShut6("backdrop closed it");
    check("ask: a tap outside closes it", !(await open6()));
    // The prose question opened by hand: Later dismisses it on the server.
    await openRow6("invoice-parser");
    check("ask: the prose question opens alone from its row (the snoozed dialog not with it)", (await txt6("asking-name")) === "Invoice parser" && !(await shown6("asking-nav")));
    const dis1 = posts6("/asking/dismiss").length;
    await page6.click("#asking-later");
    await waitShut6("closed after Later on the question");
    const disq = posts6("/asking/dismiss");
    check("ask: Later on a question → POST /asking/dismiss {} for it", disq.length === dis1 + 1 && disq[disq.length - 1].path === "/api/instances/invoice-parser/asking/dismiss" && JSON.stringify(disq[disq.length - 1].body) === "{}", JSON.stringify(disq.slice(-1)));
    state.rows[1].asking = null;

    // The mic on a dialog: the voice dialog mode → /answer.
    state.rows[0].asking = { kind: "dialog", text: "Allow git push?", ts: now6(), dialog_id: "d13", parsed: true, source: "" };
    state.dialogsBy["login-fix"] = PERM("d13");
    await waitOpen6("d13 pops");
    await waitFor(page6, () => document.querySelectorAll(".asking-opt").length === 3, 3000, "d13 options");
    const ans0 = posts6("/answer").length;
    await page6.click("#asking-mic");
    await waitFor(page6, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "dialog listening from the sheet");
    check("ask: the sheet's mic → voice on, the dialog mode listening", (await page6.evaluate(() => window.mfVoice.state)) === "dialog" && (await page6.evaluate(() => document.getElementById("asking-mic").dataset.state)) === "listening");
    check("ask: …the strip's state mirrored on the sheet", /Waiting on you/.test(await txt6("asking-voice")), await txt6("asking-voice"));
    await page6.evaluate(() => window.__hear("yes"));
    await waitShut6("answered by voice");
    const av = posts6("/answer").slice(ans0);
    check("ask: 'yes' by voice → POST /answer {keys:['1'], dialog_id:'d13', by:'user'}", av.length === 1 && JSON.stringify(av[0].body) === JSON.stringify({ keys: ["1"], dialog_id: "d13", by: "user" }), JSON.stringify(av));
    check("ask: …and the sheet closes", !(await open6()));
    state.rows[0].asking = null; state.rows[0].activity = "working";
    delete state.dialogsBy["login-fix"];
    await waitFor(page6, () => window.mfVoice.state === "working", 3000, "working after the voice answer");

    // Voice on, another session asks: read name first; the mic dictates into the field.
    await page6.evaluate(() => { window.mfVoice.prefs.replies = "mine"; });
    const q5 = { kind: "question", text: "Want me to rename the module?", ts: now6(), dialog_id: null, parsed: false, source: "" };
    state.rows[1].asking = q5;
    await kick6("invoice-parser", q5.text, q5.ts);
    await waitFor(page6, () => window.__spoken.indexOf("Invoice parser asks: Want me to rename the module?") >= 0, 3000, "read aloud");
    check("ask: voice on + spoken replies → another session's question read aloud, name first", await said(page6, "Invoice parser asks: Want me to rename the module?"));
    check("ask: …without popping the sheet", !(await open6()));
    await openRow6("invoice-parser");
    check("ask: …read once (the row's open does not read it again)", (await spoken(page6)).filter((t) => t === "Invoice parser asks: Want me to rename the module?").length === 1);
    const sendsQ5 = posts6("/send").length;
    await page6.click("#asking-mic");
    await waitFor(page6, () => window.mfVoice.state === "listening" && window.__rec && window.__rec.started, 4000, "dictating into the sheet");
    check("ask: the mic on a question → listening, voice moved to that session", (await page6.evaluate(() => window.mfVoice.session)) === "invoice-parser" && (await page6.evaluate(() => window.mfVoice.box === document.getElementById("asking-text"))));
    await page6.evaluate(() => window.__hear("rename it to parse"));
    await sleep(150);
    const fld = () => page6.evaluate(() => document.getElementById("asking-text").value);
    check("ask: dictation lands in the sheet's field, not the compose box", (await fld()) === "rename it to parse" && (await page6.evaluate(() => document.getElementById("compose").value)) === "");
    check("ask: …no router for it (every phrase is the answer)", !posts6("/voice/route").some((x) => x.body.text === "rename it to parse"));
    await page6.evaluate(() => window.__hear("disregard"));
    await sleep(150);
    check("ask: 'disregard' clears the field, still listening", (await fld()) === "" && (await page6.evaluate(() => window.mfVoice.state)) === "listening");
    await waitFor(page6, () => !window.mfVoice.speech.playing && !window.mfVoice.speech.q.length, 3000, "nod said");
    await sleep(250);
    await page6.evaluate(() => window.__hear("call it parser"));
    await page6.evaluate(() => window.__hear("submit"));
    await sleep(150);
    check("ask: 'submit' is a word in the field too: typed, nothing sent", (await fld()) === "call it parser submit" && posts6("/send").length === sendsQ5 && (await open6()), await fld());
    await page6.evaluate(() => window.__hear("disregard"));
    await waitFor(page6, () => !window.mfVoice.speech.playing && !window.mfVoice.speech.q.length, 3000, "nod said");
    await sleep(250);
    await page6.evaluate(() => window.__hear("call it parser two"));
    await page6.evaluate(() => window.__hear("over"));
    await waitShut6("sent by voice");
    const sv = posts6("/send").slice(sendsQ5);
    check("ask: 'over' → POST /send {text, dialog_safe, say:true} to the asking session", sv.length === 1 && sv[0].path === "/api/instances/invoice-parser/send" && sv[0].body.text === "call it parser two" && sv[0].body.say === true && sv[0].body.dialog_safe === true, JSON.stringify(sv));
    check("ask: …the sheet closes, the field emptied, the compose box untouched", !(await open6()) && (await fld()) === "" && (await page6.evaluate(() => document.getElementById("compose").value)) === "");
    await sleep(200);
    check("ask: …and listening goes on, now into the compose box (the sheet's mic is continuous)", (await page6.evaluate(() => window.mfVoice.state)) === "listening" && (await page6.evaluate(() => window.mfVoice.box === null)));
    state.rows[1].asking = null;
    await page6.evaluate(() => { document.getElementById("voice-cancel").click(); });
    await sleep(200);

    // Hidden page: a notification (once), its tap opens the sheet.
    await page6.evaluate(() => { window.__hidden = true; });
    const q6 = { kind: "question", text: "Publish the docs now?", ts: now6(), dialog_id: null, parsed: false, source: "" };
    state.rows[2].asking = q6;
    await waitFor(page6, () => window.__notes.length === 1, 6000, "a notification");
    const n6 = await page6.evaluate(() => window.__notes.map((n) => ({ t: n.title, b: n.opts.body, tag: n.opts.tag })));
    check("ask: hidden → a Notification for the new ask (name, question, per-session tag)", n6.length === 1 && n6[0].t === "API docs asked you a question" && n6[0].b === q6.text && n6[0].tag === "mf-ask-api-docs", JSON.stringify(n6));
    await sleep(4500);
    check("ask: …once (deduped by the ask)", (await page6.evaluate(() => window.__notes.length)) === 1);
    await page6.evaluate(() => { window.__hidden = false; });
    await page6.mouse.click(8, 8);
    await waitShut6("closed before the tap");
    await page6.evaluate(() => window.__notes[0].onclick());
    await waitOpen6("the notification's tap", 2000);
    check("ask: tapping the notification opens that session's sheet", (await open6()) && (await txt6("asking-name")) === "API docs");
    await page6.click("#asking-later");
    state.rows[2].asking = null;
    await waitShut6("closed");

    // Typing into that session's compose box: its ask waits for you.
    await page6.click("#voice-btn").catch(() => {});
    await page6.evaluate(() => { const c = document.getElementById("compose"); c.focus(); c.value = "half a thought"; });
    const cur6 = await page6.evaluate(() => document.getElementById("picker-title").textContent);
    const curT = cur6 === "Invoice parser" ? 1 : cur6 === "API docs" ? 2 : 0;
    state.rows[curT].asking = { kind: "dialog", text: "Allow rm -rf build?", ts: now6(), dialog_id: "dk", parsed: true, source: "" };
    state.dialogsBy[state.rows[curT].title] = PERM("dk");
    await sleep(4600);
    check("ask: not while you type into that session's compose box", !(await open6()));
    await page6.evaluate(() => { const c = document.getElementById("compose"); c.value = ""; c.blur(); });
    await waitOpen6("opens once you stop typing");
    check("ask: …it opens once you stop", await open6());
    await page6.click("#asking-later");
    state.rows[curT].asking = null;
    delete state.dialogsBy[state.rows[curT].title];
    await page6.close();

    // ---- the push's deep link: /m?s=<title>&voice=1 opens THAT session's sheet
    state.ntfy = { ask_popup: false, active: true };   // the link opens it even with the pop-up off
    const q7 = { kind: "question", text: "Should I bump the version?", ts: now6(), dialog_id: null, parsed: false, source: "" };
    state.rows[1].asking = q7;
    state.rows[2].asking = { kind: "dialog", text: "Allow npm publish?", ts: now6(), dialog_id: "dp", parsed: true, source: "" };
    state.dialogsBy["api-docs"] = Object.assign(PERM("dp"), { question: "Allow npm publish?" });
    const page7 = await ctx.newPage();
    page7.on("pageerror", (e) => errors.push(String(e)));
    await page7.route("**/api/**", mock);
    await page7.goto(`http://127.0.0.1:${PORT}/mobile.html?s=invoice-parser&voice=1`, { waitUntil: "load" });
    await waitFor(page7, () => window.mfAsk && !document.getElementById("asking-sheet").classList.contains("hidden"), 6000, "deep link sheet");
    check("ask: the push link opens that session's sheet (pop-up setting off)", await page7.evaluate(() => !document.getElementById("asking-sheet").classList.contains("hidden") && document.getElementById("asking-name").textContent === "Invoice parser"));
    check("ask: …the other session's dialog queued behind it: '1 of 2'", (await page7.evaluate(() => document.getElementById("asking-step").textContent)) === "1 of 2");
    check("ask: …with 'Hear it and answer by voice' (the page cannot speak before a tap)", await page7.evaluate(() => !document.getElementById("asking-hear").classList.contains("hidden")));
    check("ask: …and the strip keeps its 'Tap to hear' (voice=1 as before)", await page7.evaluate(() => /Tap to hear/.test(document.getElementById("voice-hear").textContent)));
    await page7.click("#asking-hear");
    await waitFor(page7, () => window.mfVoice.state === "listening" && window.__rec && window.__rec.started, 5000, "listening after hear");
    check("ask: Hear → read aloud name first, then listening for the answer", (await page7.evaluate(() => window.__spoken.join(" "))).indexOf("Invoice parser asks: Should I bump the version?") >= 0);
    check("ask: …the strip's 'Tap to hear' gone (one tap, not two)", await page7.evaluate(() => document.getElementById("voice-hear").classList.contains("hidden")));
    const sendsQ7 = posts("/send").length;
    await page7.evaluate(() => window.__hear("yes bump it over"));
    await waitFor(page7, () => document.getElementById("asking-name").textContent === "API docs", 4000, "deep link answered");
    const s7 = posts("/send").slice(sendsQ7);
    check("ask: the spoken answer → /send to that session, say:true", s7.length === 1 && s7[0].path === "/api/instances/invoice-parser/send" && s7[0].body.text === "yes bump it" && s7[0].body.say === true, JSON.stringify(s7));
    check("ask: …then the sheet moves on to the next ask in it", await page7.evaluate(() => !document.getElementById("asking-sheet").classList.contains("hidden") && document.getElementById("asking-name").textContent === "API docs" && document.getElementById("asking-nav").classList.contains("hidden")));
    state.rows[1].asking = null;
    await page7.click("#asking-later");
    await waitFor(page7, () => document.getElementById("asking-sheet").classList.contains("hidden"), 3000, "closed");
    state.rows[2].asking = null;
    delete state.dialogsBy["api-docs"];
    state.rows[0].asking = { kind: "dialog", text: "Allow git push?", ts: now6(), dialog_id: "dq", parsed: true, source: "" };
    state.dialogsBy["login-fix"] = PERM("dq");
    await sleep(4600);
    check("ask: pop-up off → a new dialog does not open the sheet by itself", await page7.evaluate(() => document.getElementById("asking-sheet").classList.contains("hidden")));
    check("ask: …but the title still counts it", (await page7.title()) === "(1) MindFlock — Mobile", await page7.title());
    state.rows[0].asking = null;
    delete state.dialogsBy["login-fix"];
    await page7.close();
    state.rows[1].asking = null; state.rows[2].asking = null;
    state.ntfy = { ask_popup: false, active: false };
    state.voiceCfg = null;

    // ---- Claude's select questions in the ask sheet ---------------------------
    // GET /dialog kind "select": a step at a time in the same sheet; the mock
    // moves the question on with each /answer (state.onAnswer), like the screen.
    state.ntfy = { ask_popup: true, active: false };
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    state.dialogsBy = {};
    state.answerStatus = 0; state.answerReply = null;
    state.sendStatus = 0; state.sendReply = null;
    state.rows.forEach((r) => { r.asking = null; r.activity = "idle"; });
    const STEPS = (at) => [{ label: "Database", done: at > 0 }, { label: "Features", done: at > 1 }, { label: "Submit", done: false, submit: true }];
    const NAV = { next: ["Right"], prev: ["Left"], submit: ["Enter"] };
    const Q1 = (id) => ({ id, kind: "select", parsed: true, heading: "Database", question: "Which database?", multi: false, steps: STEPS(0), step: 0, nav: NAV,
      options: [{ key: "1", label: "Postgres", desc: "The server one", kind: "other", checked: null, keys: ["1"] },
                { key: "2", label: "SQLite", desc: "A file", kind: "other", checked: null, keys: ["2"] },
                { key: "3", label: "Type something", desc: null, kind: "text", checked: null, keys: ["3"] }] });
    const Q2 = (id, checked, button) => ({ id, kind: "select", parsed: true, heading: "Features", question: "Which features?", multi: true, button: button || null, steps: STEPS(1), step: 1, nav: NAV,
      options: [{ key: "1", label: "Auth", desc: null, kind: "other", checked: !!(checked && checked[1]), keys: ["1"] },
                { key: "2", label: "Billing", desc: null, kind: "other", checked: !!(checked && checked[2]), keys: ["2"] },
                { key: "3", label: "Search", desc: "Full text", kind: "other", checked: !!(checked && checked[3]), keys: ["3"] },
                { key: "4", label: "Chat about this", desc: null, kind: "chat", checked: null, keys: ["4"] }] });
    const REVIEW = (id, answers) => ({ id, kind: "select", parsed: true, heading: "Submit", question: "Review your answers", multi: false, steps: STEPS(2), step: 2, nav: NAV,
      review: answers, options: [{ key: "1", label: "Submit answers", desc: null, kind: "yes", checked: null, keys: ["1"] }, { key: "2", label: "Cancel", desc: null, kind: "no", checked: null, keys: ["2"] }] });
    const page8 = await ctx.newPage();
    page8.on("pageerror", (e) => errors.push(String(e)));
    page8.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
    await page8.route("**/api/**", mock);
    await page8.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page8, () => window.mfAsk && window.mfAsk.polls >= 1, 6000, "select page loaded");
    const open8 = () => page8.evaluate(() => !document.getElementById("asking-sheet").classList.contains("hidden"));
    const txt8 = (id) => page8.evaluate((i) => document.getElementById(i).textContent, id);
    const shown8 = (id) => page8.evaluate((i) => !document.getElementById(i).classList.contains("hidden"), id);
    const opts8 = () => page8.evaluate(() => Array.from(document.querySelectorAll(".asking-opt")).map((b) => ({ key: b.dataset.key, kind: b.dataset.kind, role: b.getAttribute("role") || "", checked: b.getAttribute("aria-checked"), label: (b.querySelector(".asking-opt-label") || {}).textContent, desc: (b.querySelector(".asking-opt-desc") || {}).textContent || "" })));
    const answers8 = () => posts("/answer").filter((x) => x.path.indexOf("/api/instances/login-fix/") === 0 || x.path.indexOf("/api/instances/api-docs/") === 0);
    const ask8 = (title, id) => { const r = state.rows.find((x) => x.title === title); r.asking = { kind: "dialog", text: "", ts: Date.now() / 1000, dialog_id: id, parsed: true, source: "" }; };

    // Flow A: single pick → multi (ticked here) + Next → review → Submit answers.
    let flowA = [];
    state.onAnswer = (title, body) => {
      flowA.push(body);
      const d = state.dialogsBy[title];
      if (!d) return;
      if (d.id === "s1") { state.dialogsBy[title] = Q2("s2", { 3: true }); ask8(title, "s2"); }
      else if (d.id === "s2") { state.dialogsBy[title] = REVIEW("s3", [{ question: "Which database?", answer: "Postgres" }, { question: "Which features?", answer: "Auth" }]); ask8(title, "s3"); }
      else if (d.id === "s3") { delete state.dialogsBy[title]; state.rows.find((x) => x.title === title).asking = null; }
    };
    state.dialogsBy["login-fix"] = Q1("s1");
    ask8("login-fix", "s1");
    await waitFor(page8, () => document.querySelectorAll(".asking-opt").length === 3, 6000, "select Q1 pops up");
    check("select: a select dialog pops the sheet (kind Question)", (await open8()) && (await txt8("asking-kind")) === "Question", await txt8("asking-kind"));
    const tabs8 = () => page8.evaluate(() => Array.from(document.querySelectorAll(".asking-step-tab")).map((t) => t.textContent + (t.classList.contains("active") ? "*" : "") + (t.classList.contains("done") ? "+" : "")));
    check("select: the step row like Claude's tabs, the first active, Submit last", JSON.stringify(await tabs8()) === JSON.stringify(["Database*", "Features", "Submit"]), JSON.stringify(await tabs8()));
    const o1 = await opts8();
    check("select: single-select → option buttons with their descriptions, no checkboxes", o1[0].label === "Postgres" && o1[0].desc === "The server one" && o1.every((o) => o.role === ""), JSON.stringify(o1));
    check("select: …no Next or Back on the first single-select step", !(await shown8("asking-sel")));
    await page8.click('.asking-opt[data-key="1"]');
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Which features?", 5000, "Q2 in the same sheet");
    check("select: a tap → POST /answer {keys:['1'], dialog_id:'s1', by:'user'}", JSON.stringify(flowA[0]) === JSON.stringify({ keys: ["1"], dialog_id: "s1", by: "user" }), JSON.stringify(flowA));
    check("select: …the next step shows in the SAME sheet (it never closed)", (await open8()) && JSON.stringify(await tabs8()) === JSON.stringify(["Database+", "Features*", "Submit"]), JSON.stringify(await tabs8()));
    const o2 = await opts8();
    check("select: multi-select → checkboxes (the server's checked shown), the chat option a plain button", o2.filter((o) => o.role === "checkbox").length === 3 && o2[2].checked === "true" && o2[0].checked === "false" && o2[3].kind === "chat" && o2[3].role === "", JSON.stringify(o2));
    check("select: …Next and Back shown", (await shown8("asking-sel-next")) && (await shown8("asking-sel-prev")));
    check("select: the multi-select's primary is labelled from `button` (none → Next), Back says Back", (await txt8("asking-sel-next")) === "Next ›" && (await txt8("asking-sel-prev")) === "‹ Back", await txt8("asking-sel-next"));
    const nA = flowA.length;
    await page8.click('.asking-opt[data-key="1"]');
    await page8.click('.asking-opt[data-key="3"]');
    const o2b = await opts8();
    check("select: a tap toggles the box HERE (nothing posted)", flowA.length === nA && o2b[0].checked === "true" && o2b[2].checked === "false", JSON.stringify(o2b));
    await page8.click("#asking-sel-next");
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Review your answers", 5000, "the review step");
    check("select: Next → ONE /answer: the digits that differ from the server's, then nav.next", JSON.stringify(flowA[nA]) === JSON.stringify({ keys: ["1", "3", "Right"], dialog_id: "s2", by: "user" }), JSON.stringify(flowA.slice(nA)));
    const rv = await page8.evaluate(() => Array.from(document.querySelectorAll("#asking-review dt, #asking-review dd")).map((x) => x.textContent));
    check("select: the review lists the answers", JSON.stringify(rv) === JSON.stringify(["Which database?", "Postgres", "Which features?", "Auth"]) && (await shown8("asking-review")), JSON.stringify(rv));
    const acts8 = () => page8.evaluate(() => Array.from(document.querySelectorAll("#asking-sel-acts button")).map((b) => b.textContent + (b.classList.contains("asking-act-primary") ? "*" : "")));
    check("select: …then Cancel and ONE primary Submit answers in the step's row, Submit the active tab", JSON.stringify(await acts8()) === JSON.stringify(["Cancel", "Submit answers*"]) && (await tabs8())[2] === "Submit*", JSON.stringify(await acts8()));
    check("select: the review shows no numbered option rows, no digit badges", (await opts8()).length === 0 && (await page8.evaluate(() => document.querySelectorAll("#asking-sel .asking-opt-key").length)) === 0 && (await shown8("asking-sel")));
    check("select: …Back is its arrow on the left of the row", (await txt8("asking-sel-prev")) === "‹" && (await shown8("asking-sel-prev")) && !(await shown8("asking-sel-next")));
    check("select: the primary button looks primary (the accent, white text)", await page8.evaluate(() => { const b = document.querySelector("#asking-sel-acts .asking-act-primary"), c = document.querySelector("#asking-sel-acts button:not(.asking-act-primary)"); return getComputedStyle(b).color === "rgb(255, 255, 255)" && getComputedStyle(b).backgroundColor !== getComputedStyle(c).backgroundColor; }));
    check("select: the review row fits a 390 px phone", await page8.evaluate(() => { const box = document.querySelector("#asking-sheet .sheet-box").getBoundingClientRect(); return Array.from(document.querySelectorAll("#asking-sel button")).every((b) => b.getBoundingClientRect().right <= box.right - 1); }));
    await page8.click('.asking-act[data-kind="yes"]');
    await waitFor(page8, () => document.getElementById("asking-sheet").classList.contains("hidden"), 5000, "closed after Submit");
    check("select: Submit answers → its digit pinned to the review, then the sheet closes (204: answered)", JSON.stringify(flowA[flowA.length - 1]) === JSON.stringify({ keys: ["1"], dialog_id: "s3", by: "user" }) && !(await open8()), JSON.stringify(flowA.slice(-1)));
    await sleep(4500);
    check("select: …and it does not pop up again while the row lags", !(await open8()));

    // Flow B: "Type something" (digit, then {text, keys:[Enter]}), Back, a
    // dialog_changed that is just the next step, "Chat about this" (digit, then /send).
    let flowB = [];
    state.onAnswer = (title, body) => {
      flowB.push(body);
      const d = state.dialogsBy[title];
      if (!d) return;
      if (d.id === "t1" && body.text === "MariaDB") { state.dialogsBy[title] = Q2("t2"); ask8(title, "t2"); }
      else if (d.id === "t2" && body.keys && body.keys[0] === "Left") { state.dialogsBy[title] = Q1("t1b"); ask8(title, "t1b"); }
      else if (d.id === "t2b" && body.keys && body.keys[0] === "4") { delete state.dialogsBy[title]; state.rows.find((x) => x.title === title).asking = null; }
    };
    state.dialogsBy["api-docs"] = Q1("t1");
    ask8("api-docs", "t1");
    await waitFor(page8, () => document.getElementById("asking-name").textContent === "API docs" && document.querySelectorAll(".asking-opt").length === 3, 6000, "flow B pops");
    await page8.click('.asking-opt[data-kind="text"]');
    check("select: 'Type something' opens the field (nothing posted yet)", (await shown8("asking-reply")) && flowB.length === 0);
    await page8.fill("#asking-text", "MariaDB");
    await page8.click("#asking-send");
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Which features?", 5000, "Q2 after typing");
    check("select: Send → its keys, then {text, keys:['Enter']}, both pinned to the same id", flowB.length === 2 && JSON.stringify(flowB[0]) === JSON.stringify({ keys: ["3"], dialog_id: "t1", by: "user" }) && JSON.stringify(flowB[1]) === JSON.stringify({ text: "MariaDB", keys: ["Enter"], dialog_id: "t1", by: "user" }), JSON.stringify(flowB));
    check("select: …the field closed and emptied on the next step", !(await shown8("asking-reply")) && (await page8.evaluate(() => document.getElementById("asking-text").value)) === "");
    await page8.click("#asking-sel-prev");
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Which database?", 5000, "back to Q1");
    check("select: Back → nav.prev pinned to the step", JSON.stringify(flowB[2]) === JSON.stringify({ keys: ["Left"], dialog_id: "t2", by: "user" }), JSON.stringify(flowB[2]));
    state.answerStatus = 409; state.answerReply = { dialog_changed: true, error: "the prompt changed" };
    state.dialogsBy["api-docs"] = Q2("t2b", null, "Submit"); ask8("api-docs", "t2b");
    await page8.click('.asking-opt[data-key="2"]');
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Which features?", 5000, "dialog_changed → the step shown now");
    state.answerStatus = 0; state.answerReply = null;
    check("select: 409 dialog_changed inside the flow → the step on screen, no error, no note", (await open8()) && !(await shown8("asking-error")) && !(await shown8("asking-note")));
    check("select: a multi-select whose own row says Submit → the primary reads 'Submit ›'", (await txt8("asking-sel-next")) === "Submit ›", await txt8("asking-sel-next"));
    const sendsB = posts("/send").length, postsB = state.posts.length;
    await page8.click('.asking-opt[data-kind="chat"]');
    check("select: 'Chat about this' opens the field", (await shown8("asking-reply")) && (await page8.evaluate(() => document.getElementById("asking-text").placeholder)) === "What to tell it");
    await page8.fill("#asking-text", "let's talk about search first");
    await page8.click("#asking-send");
    await waitFor(page8, () => document.getElementById("asking-sheet").classList.contains("hidden"), 5000, "closed after chat");
    const after = state.posts.slice(postsB).map((x) => x.path.replace("/api/instances/api-docs", "") + " " + JSON.stringify(x.body));
    check("select: Chat → its digit, then /send {text, dialog_safe:true}, then the sheet closes", after.length === 2 && after[0] === '/answer {"keys":["4"],"dialog_id":"t2b","by":"user"}' && after[1] === '/send {"text":"let\'s talk about search first","dialog_safe":true}' && posts("/send").length === sendsB + 1, JSON.stringify(after));

    // Flow C: by voice — the sheet's mic; the router names a step's option,
    // a number ticks a box here, "next" moves on, "submit" on the review submits.
    let flowC = [];
    state.onAnswer = (title, body) => {
      flowC.push(body);
      const d = state.dialogsBy[title];
      if (!d) return;
      if (d.id === "v1") { state.dialogsBy[title] = Q2("v2"); ask8(title, "v2"); }
      else if (d.id === "v2") { state.dialogsBy[title] = REVIEW("v3", [{ question: "Which database?", answer: "SQLite" }, { question: "Which features?", answer: "Billing" }]); ask8(title, "v3"); }
      else if (d.id === "v3") { delete state.dialogsBy[title]; state.rows.find((x) => x.title === title).asking = null; }
    };
    state.dialogsBy["login-fix"] = Q1("v1");
    ask8("login-fix", "v1");
    await waitFor(page8, () => document.getElementById("asking-name").textContent === "Login fix" && document.getElementById("asking-question").textContent === "Which database?" && !document.getElementById("asking-sheet").classList.contains("hidden"), 6000, "flow C pops");
    await page8.click("#asking-mic");
    await waitFor(page8, () => window.mfVoice.state === "dialog" && window.__rec && window.__rec.started, 4000, "listening on the select");
    check("select by voice: the sheet's mic listens in the dialog mode", (await vstate(page8)) === "dialog");
    await page8.evaluate(() => window.__hear("the second one"));
    await waitFor(page8, () => window.mfVoice.state === "dialog" && document.getElementById("asking-question").textContent === "Which features?" && window.__rec && window.__rec.started, 6000, "Q2 read and listened for");
    check("select by voice: the router's pick → its digit pinned to the step", JSON.stringify(flowC[0]) === JSON.stringify({ keys: ["2"], dialog_id: "v1", by: "user" }), JSON.stringify(flowC));
    const q2line = await saidAll(page8);
    check("select by voice: the next step is read (bare: the phone is on it), multi → 'Pick any, then say next.'", q2line.indexOf("Which features? one: Auth. two: Billing. three: Search. four: Chat about this. Say one, two, three, or four. Pick any, then say next.") >= 0 && q2line.indexOf("Login fix: Which features") < 0, q2line.slice(-260));
    await page8.evaluate(() => window.__hear("two"));
    await waitFor(page8, () => window.__spoken.indexOf("Ticked Billing.") >= 0 && window.mfVoice.state === "dialog", 5000, "ticked by voice");
    check("select by voice: a number on a multi-select ticks the box here and says so", (await opts8())[1].checked === "true" && flowC.length === 1);
    await page8.evaluate(() => window.__hear("next"));
    await waitFor(page8, () => document.getElementById("asking-question").textContent === "Review your answers" && window.mfVoice.state === "dialog", 6000, "review by voice");
    check("select by voice: 'next' → the ticked digit + nav.next in one /answer", JSON.stringify(flowC[1]) === JSON.stringify({ keys: ["2", "Right"], dialog_id: "v2", by: "user" }), JSON.stringify(flowC[1]));
    check("select by voice: the review is read: 'Say yes or no.'", (await saidAll(page8)).indexOf("Review your answers Say yes or no.") >= 0);
    await page8.evaluate(() => window.__hear("submit"));
    await waitFor(page8, () => document.getElementById("asking-sheet").classList.contains("hidden") && window.mfVoice.state === "working", 6000, "submitted by voice");
    check("select by voice: 'submit' on the review → Submit answers' digit, 'Answered.', working", JSON.stringify(flowC[2]) === JSON.stringify({ keys: ["1"], dialog_id: "v3", by: "user" }) && (await said(page8, "Answered.")) && (await vstate(page8)) === "working", JSON.stringify(flowC[2]));
    state.onAnswer = null;
    await page8.close();
    state.dialogsBy = {};
    state.rows.forEach((r) => { r.asking = null; });

    // ---- Everything + one voice at a time ---------------------------------------
    // Spoken replies "all": every session's finished turn is read (voice on
    // or off), once, named unless the phone is on it; typed prompts ask for a
    // Say line; one queue says it all, in order, never two at once.
    state.ntfy = { ask_popup: false, active: false };
    const page9 = await ctx.newPage();
    page9.on("pageerror", (e) => errors.push(String(e)));
    page9.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
    await page9.route("**/api/**", mock);
    await page9.addInitScript(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ replies: "all" })); } catch (e) {} });
    await page9.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page9, () => window.mfVoice && document.getElementById("picker-title").textContent === "Login fix", 6000, "everything page loaded");
    check("say-all: an older server (no say_all in GET /api/voice) → nothing posted on load", posts("/voice/say-all").length === 0);
    check("everything: the pref loads, voice off, the events socket wanted anyway", (await page9.evaluate(() => window.mfVoice.prefs.replies)) === "all" && (await vstate(page9)) === "off" && (await page9.evaluate(() => window.mfVoice.eventsState)) !== "off", await page9.evaluate(() => window.mfVoice.eventsState));
    await page9.mouse.click(200, 300);
    check("everything: the first tap anywhere primes speech", await page9.evaluate(() => window.mfVoice.primed === true));
    const turn9 = (t, say, ev, ts) => page9.evaluate((a) => window.mfVoice.onEvent({ seq: 0, event: a[2] || "session.activity_changed", session: a[0], old: "working", new: "idle", ts: a[3] || Date.now() / 1000, data: { title: a[0], say: a[1] } }), [t, say, ev, ts]);
    await turn9("invoice-parser", "Parsed all twelve files.");
    await waitFor(page9, () => window.__spoken.indexOf("Invoice parser: Parsed all twelve files.") >= 0, 3000, "another session's turn");
    check("everything: another session's finished turn is read, its name first (voice off)", await said(page9, "Invoice parser: Parsed all twelve files."));
    await turn9("invoice-parser", "Parsed all twelve files.", "session.turn_ended");
    await sleep(300);
    check("everything: …once (turn_ended with the same line is not read again)", (await spoken(page9)).filter((t) => t === "Invoice parser: Parsed all twelve files.").length === 1);
    await turn9("login-fix", "Login works again.");
    await waitFor(page9, () => window.__spoken.indexOf("Login works again.") >= 0, 3000, "own turn");
    check("everything: the phone's own session is read bare", (await said(page9, "Login works again.")) && !(await said(page9, "Login fix: Login works again.")));
    await page9.evaluate(() => window.mfVoice.onEvent({ seq: 0, event: "session.activity_changed", session: "invoice-parser", old: "idle", new: "working", ts: Date.now() / 1000, data: {} }));
    await turn9("invoice-parser", "Parsed all twelve files.");
    await waitFor(page9, () => window.__spoken.filter((t) => t === "Invoice parser: Parsed all twelve files.").length === 2, 3000, "a new turn, same words");
    check("everything: a new turn with the same words is read again", (await spoken(page9)).filter((t) => t === "Invoice parser: Parsed all twelve files.").length === 2);
    // A turn whose frame is two minutes old (a reconnect's replay): dropped, not said late.
    await page9.evaluate(() => { window.mfVoice.serverSkew = 0; });
    await turn9("api-docs", "Old news from before.", "session.turn_ended", Date.now() / 1000 - 120);
    await sleep(400);
    check("everything: a line over a minute old when its turn comes is dropped", !(await said(page9, "Old news from before.")));
    // One at a time: a slow voice, three sessions at once.
    await page9.evaluate(() => { window.__speakMs = 250; window.__speakLog = []; });
    await turn9("invoice-parser", "Tests pass.");
    await turn9("api-docs", "Docs are built.");
    await turn9("login-fix", "Pushed the fix.");
    await waitFor(page9, () => window.__speakLog.length >= 3 && window.__speakLog.every((x) => x.end), 5000, "three lines said");
    const log9 = await page9.evaluate(() => window.__speakLog.map((x) => [x.t, x.start, x.end]));
    const overlap = log9.some((x, i) => i > 0 && x[1] < log9[i - 1][2]);
    check("queue: three sessions' lines said one at a time, in arrival order, never over each other", JSON.stringify(log9.map((x) => x[0])) === JSON.stringify(["Invoice parser: Tests pass.", "API docs: Docs are built.", "Pushed the fix."]) && !overlap, JSON.stringify(log9));
    // Typed prompts ask for a Say line with Everything — voice off: through /send.
    const sends9 = posts("/send").length;
    await page9.fill("#compose", "list the files");
    await page9.dispatchEvent("#send", "pointerdown");
    await waitFor(page9, () => document.getElementById("compose").value === "", 3000, "typed send");
    await sleep(300);
    const ts9 = posts("/send").slice(sends9);
    check("everything: a typed prompt with voice off → POST /send {text, dialog_safe, say:true}", ts9.length === 1 && ts9[0].path === "/api/instances/login-fix/send" && ts9[0].body.text === "list the files" && ts9[0].body.say === true && ts9[0].body.dialog_safe === true, JSON.stringify(ts9));
    // Voice on, continuous listening (a tap): a line plays in the quiet,
    // over the listen — the recorder deaf for it — never after "over and
    // out"; mid-sentence it waits for the quiet. A listen that is not
    // continuous holds the queue; a listen after a read-out starts only
    // once the queue has drained.
    await page9.evaluate(() => { window.__speakMs = 300; });
    await page9.click("#voice-btn");
    await waitFor(page9, () => window.mfVoice.state === "listening", 3000, "listening (everything)");
    await sleep(300);
    await turn9("api-docs", "Deployed to staging.");
    await sleep(120);
    check("continuous: a line queued in the quiet plays at once, over the listen", (await said(page9, "API docs: Deployed to staging.")) && (await vstate(page9)) === "listening", await vstate(page9));
    check("continuous: …the recorder is muted while it plays, the listen still on", (await page9.evaluate(() => window.mfVoice.micMuted() && window.mfVoice.speech.playing && window.mfVoice.speech.playing.over === true)));
    await waitFor(page9, () => !window.mfVoice.speech.playing, 3000, "line over");
    await sleep(200);
    check("continuous: …and unmuted after it (+150 ms), still listening", !(await page9.evaluate(() => window.mfVoice.micMuted())) && (await vstate(page9)) === "listening");
    // Mid-sentence: words a moment ago and still coming — it waits for the quiet.
    await page9.evaluate(() => window.__hear("fix the", true));
    await turn9("invoice-parser", "Parsed the last file.");
    await sleep(60);
    check("continuous: mid-sentence the line waits", !(await said(page9, "Invoice parser: Parsed the last file.")));
    await waitFor(page9, () => window.__spoken.indexOf("Invoice parser: Parsed the last file.") >= 0, 3000, "said in the quiet");
    check("continuous: …then plays once the voice has been quiet, still listening", (await vstate(page9)) === "listening");
    await waitFor(page9, () => !window.mfVoice.speech.playing, 3000, "line 2 over");
    await page9.evaluate(() => { window.__speakMs = 15; window.mfVoice.listen.cont = false; });   // as an answer-listen is
    await turn9("api-docs", "Deployed again.");
    await sleep(500);
    check("queue: a listen that is not continuous holds it", !(await said(page9, "API docs: Deployed again.")) && (await page9.evaluate(() => window.mfVoice.speech.q.length)) === 1);
    await page9.evaluate(() => window.__hear("hello, that's all"));
    await waitFor(page9, () => window.__spoken.indexOf("API docs: Deployed again.") >= 0, 3000, "said after listening");
    check("queue: …and says it once listening stops", await said(page9, "API docs: Deployed again."));
    await page9.evaluate(() => { document.getElementById("compose").value = ""; });
    await waitFor(page9, () => window.mfVoice.state === "idle", 3000, "idle again");
    await page9.evaluate(() => { window.__speakMs = 300; window.__speakLog = []; });
    await turn9("invoice-parser", "Coverage is ninety percent.");
    await page9.evaluate(() => window.mfVoice.onEvent({ seq: 0, event: "session.turn_ended", session: "login-fix", ts: Date.now() / 1000, data: { title: "login-fix", say: "Should I push it?", has_question: true } }));
    await turn9("api-docs", "Index rebuilt again.");
    await waitFor(page9, () => window.mfVoice.state === "awaiting" && window.__rec && window.__rec.started, 6000, "awaiting after the queue drained");
    const log9b = await page9.evaluate(() => window.__speakLog.map((x) => [x.t, x.end]));
    const recAt = await page9.evaluate(() => window.__recStartAt);
    check("queue: the answer-listen after a read-out starts only once the queue has drained", JSON.stringify(log9b.map((x) => x[0])) === JSON.stringify(["Invoice parser: Coverage is ninety percent.", "Should I push it?", "API docs: Index rebuilt again."]) && recAt >= Math.max.apply(null, log9b.map((x) => x[1])), JSON.stringify(log9b) + " rec " + recAt);
    await page9.evaluate(() => { window.__speakMs = 15; document.getElementById("voice-cancel").click(); });
    await sleep(200);
    // Typed with voice on, Everything → say:true; Mine → other sessions' turns stay quiet.
    const sends9b = posts("/send").length;
    await page9.fill("#compose", "run it");
    await page9.dispatchEvent("#send", "pointerdown");
    await waitFor(page9, () => window.mfVoice.state === "working", 3000, "typed send, voice on");
    check("everything: a typed prompt with voice on asks for a Say line too", posts("/send").slice(sends9b).some((x) => x.body.text === "run it" && x.body.say === true), JSON.stringify(posts("/send").slice(sends9b)));
    await page9.evaluate(() => { const el = document.getElementById("voice-replies"); el.value = "mine"; el.dispatchEvent(new Event("change")); });
    await turn9("api-docs", "Nobody asked for this.");
    await sleep(400);
    check("mine: another session's turn is not read", !(await said(page9, "Nobody asked for this.")));
    check("mine: the choice is stored as 'mine'", (await page9.evaluate(() => JSON.parse(localStorage.getItem("cs_voice")).replies)) === "mine");
    await page9.close();
    state.ntfy = { ask_popup: false, active: false };
    state.voiceCfg = null;

    // ---- Speech stops when you act; barge-in; "stop talking"; say-all -----------
    // A hand on the ask sheet cuts what is read about that ask (other lines
    // stay) and the step it moves to is not read; the mic while a line is
    // said cuts it before listening (~150 ms later); "stop talking" (said,
    // the router's hush, the strip's button) drops the queue and the mic
    // listens on; Everything sets the server's say-all marker.
    state.ntfy = { ask_popup: true, active: false };
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    state.sayAll = false; state.sayAllStatus = 0;
    state.dialogsBy = {};
    state.rows.forEach((r) => { r.asking = null; r.activity = "idle"; });
    const sa0 = posts("/voice/say-all").length;
    const sa = () => posts("/voice/say-all").slice(sa0).map((x) => x.body.on);
    const page10 = await ctx.newPage();
    page10.on("pageerror", (e) => errors.push(String(e)));
    page10.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409|403/.test(msg.text())) errors.push(msg.text()); });
    await page10.route("**/api/**", mock);
    await page10.addInitScript(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ replies: "all" })); } catch (e) {} });
    await page10.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page10, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Login fix", 6000, "hush page loaded");
    await sleep(400);
    check("say-all: Everything on load while the server's say_all is false → ONE POST {on:true}", JSON.stringify(sa()) === "[true]", JSON.stringify(sa()));
    const turn10 = (t, say) => page10.evaluate((a) => window.mfVoice.onEvent({ seq: 0, event: "session.turn_ended", session: a[0], ts: Date.now() / 1000, data: { title: a[0], say: a[1] } }), [t, say]);
    const q10 = () => page10.evaluate(() => window.mfVoice.speech.q.map((x) => x.text));
    const listening10 = () => page10.evaluate(() => window.mfVoice.state === "listening" && !!window.__rec && window.__rec.started);
    const compose10 = () => page10.evaluate(() => document.getElementById("compose").value);

    // "stop talking" by voice, while listening: the queue goes, the mic stays.
    await page10.click("#voice-btn");
    await waitFor(page10, () => window.mfVoice.state === "listening" && window.__rec && window.__rec.started, 3000, "listening (hush)");
    await page10.evaluate(() => { window.mfVoice.listen.cont = false; });   // a listen that holds the queue (an answer-listen); continuous plays them (page9)
    await turn10("invoice-parser", "Parsed the twelve files.");
    await turn10("api-docs", "Docs are built.");
    await sleep(200);
    check("hush: lines queue while the mic listens", (await q10()).length === 2, JSON.stringify(await q10()));
    const r10 = posts("/voice/route").length;
    await page10.evaluate(() => window.__hear("stop talking"));
    await sleep(150);
    check("hush: 'stop talking' → every queued line goes", (await q10()).length === 0, JSON.stringify(await q10()));
    check("hush: …the mic keeps listening", await listening10());
    check("hush: …'Quiet.' on the strip, nothing typed, the router not asked", (await page10.evaluate(() => document.getElementById("voice-caption").textContent)) === "Quiet." && (await compose10()) === "" && posts("/voice/route").length === r10, await page10.evaluate(() => window.mfVoice.caption));
    await sleep(500);
    check("hush: …and those lines are never said", !(await said(page10, "Parsed the twelve files.")) && !(await said(page10, "Docs are built.")));
    await turn10("invoice-parser", "Coverage is up.");
    await sleep(100);
    await page10.evaluate(() => window.__hear("could you pipe down"));
    await waitFor(page10, () => window.mfVoice.speech.q.length === 0, 3000, "router hush");
    await sleep(100);
    check("hush: the router's kind hush → the queue goes, still listening, nothing typed", (await q10()).length === 0 && (await listening10()) && (await compose10()) === "" && posts("/voice/route").length === r10 + 1);
    await turn10("api-docs", "Index rebuilt.");
    await sleep(100);
    await page10.evaluate(() => window.__hear("okay stop"));
    await sleep(200);
    check("hush: 'okay stop' is stop talking, not stop listening", (await q10()).length === 0 && (await listening10()));
    await page10.evaluate(() => window.__hear("shut up"));
    await sleep(100);
    check("hush: 'shut up' → never typed", (await compose10()) === "" && (await listening10()));
    await page10.click("#voice-cancel");
    await waitFor(page10, () => window.mfVoice.state === "idle", 3000, "idle (hush)");

    // The strip's Stop talking, and a tap on the line being said.
    await page10.evaluate(() => { window.__speakMs = 2500; });
    await turn10("invoice-parser", "First long line.");
    await turn10("api-docs", "Second long line.");
    await waitFor(page10, () => !document.getElementById("voice-hush").classList.contains("hidden"), 3000, "Stop talking shown");
    check("hush: 'Stop talking' shows in the strip while a line is said", await page10.evaluate(() => !document.getElementById("voice-hush").classList.contains("hidden") && !!window.mfVoice.speech.playing));
    let c10 = await page10.evaluate(() => window.__cancels);
    await page10.click("#voice-hush");
    await sleep(150);
    check("hush: the strip's Stop talking → the speaker cut, the queue gone, the button hidden", (await page10.evaluate(() => window.__cancels)) > c10 && (await q10()).length === 0 && !(await page10.evaluate(() => window.mfVoice.speech.playing)) && (await page10.evaluate(() => document.getElementById("voice-hush").classList.contains("hidden"))));
    await sleep(400);
    check("hush: …the line after it is never said", !(await said(page10, "Second long line.")));
    await turn10("invoice-parser", "Third long line.");
    await turn10("api-docs", "Fourth long line.");
    await waitFor(page10, () => window.__spoken.some((t) => /Third long line/.test(t)), 3000, "third line being said");
    await page10.click("#voice-caption");
    await sleep(150);
    check("hush: a tap on the line being said (the strip) stops talking too", (await q10()).length === 0 && !(await page10.evaluate(() => window.mfVoice.speech.playing)));

    // Barge-in: the mic while a line is said.
    await page10.evaluate(() => { window.__cancelAt = 0; window.__recStartAt = 0; });
    await turn10("invoice-parser", "Barge line one.");
    await turn10("invoice-parser", "Barge line two.");
    await turn10("api-docs", "Kept for later.");
    await waitFor(page10, () => window.mfVoice.speech.playing && /Barge line one/.test(window.mfVoice.speech.playing.text) && window.__spoken.some((t) => /Barge line one/.test(t)), 3000, "barge line being said");
    await page10.click("#voice-btn");
    await waitFor(page10, () => window.mfVoice.state === "listening" && window.__rec && window.__rec.started, 3000, "barge listening");
    const b10 = await page10.evaluate(() => ({ cancelAt: window.__cancelAt, recAt: window.__recStartAt, q: window.mfVoice.speech.q.map((x) => x.text), playing: !!window.mfVoice.speech.playing }));
    check("barge-in: the mic while a line is said → the speaker is cut BEFORE the mic opens", b10.cancelAt > 0 && b10.recAt > 0 && b10.cancelAt <= b10.recAt, JSON.stringify(b10));
    check("barge-in: …the mic opens ~150 ms after the cut (the phone's own voice unheard)", b10.recAt - b10.cancelAt >= 120 && b10.recAt - b10.cancelAt < 1000, String(b10.recAt - b10.cancelAt));
    const b10s = await spoken(page10);
    check("barge-in: …that session's next line dropped, another session's kept (said over the continuous listen, or queued)", !b10.q.some((t) => /Barge line two/.test(t)) && !b10s.some((t) => /Barge line two/.test(t)) && (b10.q.some((t) => /Kept for later/.test(t)) || (await page10.evaluate(() => window.mfVoice.speech.playing && /Kept for later/.test(window.mfVoice.speech.playing.text))) || b10s.some((t) => /Kept for later/.test(t))), JSON.stringify([b10.q, b10s.slice(-3)]));
    check("barge-in: …and the cut line is not said again", (await spoken(page10)).filter((t) => /Barge line/.test(t)).length === 1, JSON.stringify((await spoken(page10)).slice(-6)));
    await page10.click("#voice-cancel");
    await waitFor(page10, () => window.mfVoice.state === "idle", 3000, "idle (barge)");

    // A hand on the sheet: the read-out of that ask stops, other lines stay,
    // voice stops waiting on it, and the step it moves to is not read.
    let flowH = [];
    state.onAnswer = (title, body) => {
      flowH.push(body);
      const d = state.dialogsBy[title];
      if (d && d.id === "h1") { state.dialogsBy[title] = Q2("h2"); ask8(title, "h2"); }
    };
    state.dialogsBy["login-fix"] = Q1("h1");
    state.rows.find((x) => x.title === "login-fix").activity = "clarify";
    ask8("login-fix", "h1");
    await page10.evaluate(() => { window.__speakMs = 4000; });
    await waitFor(page10, () => window.mfVoice.speech.playing && /Which database/.test(window.mfVoice.speech.playing.text) && window.__spoken.some((t) => /Which database/.test(t)), 9000, "the select read aloud");
    await turn10("api-docs", "Other session line.");
    await waitFor(page10, () => !document.getElementById("asking-sheet").classList.contains("hidden") && document.querySelectorAll(".asking-opt").length === 3, 4000, "the sheet shows it");
    c10 = await page10.evaluate(() => window.__cancels);
    await page10.click('.asking-opt[data-key="1"]');
    await sleep(100);
    const h10 = await page10.evaluate(() => ({ playing: window.mfVoice.speech.playing ? window.mfVoice.speech.playing.text : "", q: window.mfVoice.speech.q.map((x) => x.text), st: window.mfVoice.state }));
    check("tap: a tap on an option cuts the read-out of that ask", (await page10.evaluate(() => window.__cancels)) > c10 && !/Which database/.test(h10.playing), JSON.stringify(h10));
    check("tap: …another session's line stays (said next)", /Other session line/.test(h10.playing) || h10.q.some((t) => /Other session line/.test(t)), JSON.stringify(h10));
    check("tap: …voice stops waiting on its answer (not the dialog listen)", h10.st !== "dialog" && h10.st !== "speaking" && h10.st !== "listening", h10.st);
    check("tap: …and the tap is posted", JSON.stringify(flowH[0]) === JSON.stringify({ keys: ["1"], dialog_id: "h1", by: "user" }), JSON.stringify(flowH));
    await waitFor(page10, () => document.getElementById("asking-question").textContent === "Which features?", 5000, "the next step by hand");
    await page10.evaluate(() => { window.__speakMs = 15; });
    await sleep(4800);   // a poll comes and goes
    check("tap: the step a hand moved to is not read aloud (not even by the next poll)", !(await said(page10, "Which features?")) && (await vstate(page10)) !== "dialog", (await saidAll(page10)).slice(-200));
    check("tap: …the other session's line was said", await said(page10, "Other session line."));
    await page10.click('.asking-opt[data-key="2"]');
    await sleep(150);
    check("tap: a box toggled by hand reads nothing", !(await said(page10, "Ticked")));
    await page10.click("#asking-later");
    state.onAnswer = null;
    state.dialogsBy = {};
    state.rows.forEach((r) => { r.asking = null; r.activity = "idle"; });

    // Say-all follows the Spoken replies choice.
    const sel10 = (v) => page10.evaluate((x) => { const el = document.getElementById("voice-replies"); el.value = x; el.dispatchEvent(new Event("change")); }, v);
    await sel10("mine"); await sleep(200);
    await sel10("off"); await sleep(200);
    await sel10("all"); await sleep(200);
    check("say-all: away from Everything → {on:false}; mine → off posts nothing; back → {on:true}", JSON.stringify(sa()) === "[true,false,true]", JSON.stringify(sa()));
    state.sayAllStatus = 403;
    await sel10("mine"); await sleep(200);
    const st10 = await page10.evaluate(() => document.getElementById("status").textContent);
    await sel10("all"); await sleep(200);
    check("say-all: a 403 → said once, the pref kept", /can't change/.test(st10) && (await page10.evaluate(() => window.mfVoice.sayAll.denied && window.mfVoice.prefs.replies === "all")), st10);
    await page10.close();
    state.sayAllStatus = 0;
    state.sayAll = true;
    const sa1 = posts("/voice/say-all").length;
    const page11 = await ctx.newPage();
    page11.on("pageerror", (e) => errors.push(String(e)));
    await page11.route("**/api/**", mock);
    await page11.addInitScript(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ replies: "mine" })); } catch (e) {} });
    await page11.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(page11, () => window.mfVoice && window.mfVoice.cfg.loaded, 6000, "say-all page loaded");
    await sleep(400);
    check("say-all: not Everything on load while the server's say_all is on → NOTHING posted (another device may be on Everything; off is only an explicit switch)", posts("/voice/say-all").length === sa1, JSON.stringify(posts("/voice/say-all").slice(sa1)));
    await page11.close();
    state.sayAll = null;
    state.ntfy = { ask_popup: false, active: false };
    state.voiceCfg = null;

    // ---- the wake word: a lone "MindFlock" starts listening
    // The mic is page3's controllable tone; the page is hidden at will
    // (__hidden + a visibilitychange); every caption is logged (__caps).
    const until = async (fn, ms) => { const t0 = Date.now(); while (!fn() && Date.now() - t0 < ms) await sleep(50); return fn(); };
    const wakeInit = (deny) => {
      window.__gum = 0; window.__stops = 0; window.__caps = []; window.__hidden = false;
      navigator.mediaDevices.getUserMedia = function () {
        window.__gum++;
        if (window.__deny) return Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
        const ac = new AudioContext();
        const osc = ac.createOscillator();
        const gain = ac.createGain();
        const dst = ac.createMediaStreamDestination();
        gain.gain.value = 0.002;
        osc.frequency.value = 440;
        osc.connect(gain); gain.connect(dst); osc.start();
        window.__tone = (g) => { gain.gain.value = g; };
        dst.stream.getTracks().forEach((t) => { const stop = t.stop.bind(t); t.stop = () => { window.__stops++; stop(); }; });
        return Promise.resolve(dst.stream);
      };
      window.__deny = !!deny;
      Object.defineProperty(document, "hidden", { get() { return window.__hidden; }, configurable: true });
      Object.defineProperty(document, "visibilityState", { get() { return window.__hidden ? "hidden" : "visible"; }, configurable: true });
      document.addEventListener("DOMContentLoaded", () => {
        const el = document.getElementById("voice-caption");
        new MutationObserver(() => window.__caps.push(el.textContent)).observe(el, { childList: true, characterData: true, subtree: true });
      });
    };
    state.voiceCfg = {
      stt: "server", tts: "phone", approve_dialogs: true,
      server_ready: { stt: true, tts: false, stt_backend: "builtin" },
      backends: { stt: ["builtin"], tts: [] },
    };
    state.wakes = []; state.wakeReply = { wake: false, text: "the" }; state.wakeStatus = 0;
    state.transcribes = [];
    state.transcribeReply = { text: "", backend: "builtin", ms: 90 };
    const opensW = state.streamOpens.length;
    const pageW = await ctx.newPage();
    pageW.on("pageerror", (e) => errors.push(String(e)));
    pageW.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
    await pageW.route("**/api/**", mock);
    await pageW.addInitScript(wakeInit, false);
    await pageW.addInitScript(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ wake: true, replies: "all" })); } catch (e) {} });
    await pageW.goto(`http://127.0.0.1:${PORT}/mobile.html?s=invoice-parser`, { waitUntil: "load" });
    await waitFor(pageW, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Invoice parser", 6000, "wake page loaded");
    const W = (fn, arg) => pageW.evaluate(fn, arg);
    const standbyW = () => W(() => window.mfVoice.stt.standbyOn() && document.getElementById("voice-btn").hasAttribute("data-standby"));
    const lone = async (ms) => { await W(() => window.__tone(0.4)); await sleep(ms); await W(() => window.__tone(0.002)); };
    check("wake: the pref loads from storage; voice off → no standby, the mic never asked for", (await W(() => window.mfVoice.prefs.wake === true && !document.getElementById("voice-btn").hasAttribute("data-standby") && window.__gum === 0)));
    await pageW.click("#voice-btn");
    await waitFor(pageW, () => window.mfVoice.state === "listening" && window.__tone, 4000, "wake page listening");
    check("wake: while the tap's listen runs the standby waits", !(await W(() => window.mfVoice.stt.standbyOn() || document.getElementById("voice-btn").hasAttribute("data-standby"))));
    check("wake: …the standby and the recorder asked for the mic ONCE between them", (await W(() => window.__gum)) === 1);
    await pageW.click("#voice-cancel");
    await waitFor(pageW, () => window.mfVoice.state === "idle" && window.mfVoice.stt.standbyOn() && document.getElementById("voice-btn").hasAttribute("data-standby"), 3000, "standby");
    check("wake: idle → the standby runs on the warm mic, the button shows it (data-standby)", await standbyW());
    await sleep(700);   // its quiet before
    await lone(600);
    await until(() => state.wakes.length >= 1, 3000);
    const w1 = state.wakes[0] || {};
    check("wake: a lone word → ONE POST /api/voice/wake, a 16 kHz WAV of just it (lead-in, the word, its quiet tail)", state.wakes.length === 1 && w1.ctype === "audio/wav" && w1.head === "RIFF" && w1.rate === 16000 && w1.ms >= 800 && w1.ms <= 1600 && /^en/.test(w1.lang || ""), JSON.stringify(state.wakes));
    check("wake: …and nothing else reached the server (no transcribe, no stream)", state.transcribes.length === 0 && state.streamOpens.length === opensW);
    await sleep(400);
    await lone(300);
    await sleep(800);
    check("wake: not the word → nothing (idle, no chime, not listening)", (await W(() => window.mfVoice.state === "idle" && window.mfVoice.wakeState.chimes === 0)));
    check("wake: one check per 1.5 s — a second lone word right after is not sent", state.wakes.length === 1, JSON.stringify(state.wakes));
    await sleep(800);
    await lone(2000);
    await sleep(900);
    check("wake: a sentence (2 s of sound) is never sent", state.wakes.length === 1, JSON.stringify(state.wakes));
    await lone(120);
    await sleep(900);
    check("wake: …nor a click (120 ms)", state.wakes.length === 1, JSON.stringify(state.wakes));
    await lone(500);
    await until(() => state.wakes.length >= 2, 3000);
    check("wake: past the rate limit the next lone word is sent", state.wakes.length === 2, JSON.stringify(state.wakes));
    // The word: listening at once, as a tap does — the chime, a tick in the
    // same moment. The server's check takes 300 ms (its fast model).
    state.wakeReply = { wake: true, text: "MindFlock." };
    state.wakeDelay = 300;
    await sleep(1700);
    const vibW = await W(() => window.__vibes);
    await lone(600);
    await waitFor(pageW, () => window.mfVoice.state === "listening", 4000, "woke → listening");
    state.wakeDelay = 0;
    const wk = await W(() => ({ ms: window.mfVoice.wakeState.wakeMs, startedAt: window.mfVoice.listen.startedAt, trace: window.mfVoice.rec.trace }));
    check("wake: listening started the moment the answer came (no wait for the chime)", wk.startedAt - state.wakeAnsweredAt < 100, JSON.stringify([wk.startedAt - state.wakeAnsweredAt, wk]));
    check("wake: the word's end → listening measured: the 200 ms tail + the 300 ms check (+ the round trip), in the trace too", wk.ms >= 450 && wk.ms < 1000 && wk.trace && wk.trace.wake === wk.ms, JSON.stringify(wk));
    console.log("  wake: the word's end → listening " + wk.ms + " ms (200 ms tail, 300 ms mocked check)");
    check("wake: the word → listening, as a tap in toggle mode (prompt, the recorder on)", (await W(() => window.mfVoice.state === "listening" && window.mfVoice.listen.mode === "prompt" && !window.mfVoice.listen.held)) && state.wakes.length === 3);
    check("wake: …the chime played (the page's AudioContext)", (await W(() => window.mfVoice.wakeState.chimes)) === 1);
    check("wake: …the strip says only “Listening…” (no “Yes?” on it)", (await W(() => document.getElementById("voice-caption").textContent === "Listening…" && window.__caps.indexOf("Yes?") < 0)), JSON.stringify((await W(() => window.__caps)).slice(-6)));
    check("wake: …a tick (and the listening buzz)", (await W(() => window.__vibes)) - vibW >= 2, String((await W(() => window.__vibes)) - vibW));
    await waitFor(pageW, () => window.mfVoice.stt.active(), 2000, "woke recorder");
    check("wake: …the standby paused for it", !(await W(() => window.mfVoice.stt.standbyOn())));
    await sleep(400);   // an answer starts a moment after the chime (the recorder's floor is the room's first)
    await lone(500);
    await until(() => state.transcribes.length >= 1, 4000);   // the listen's silence stop: 1.1 s after the word
    check("wake: a word while it listens is the listen's, not a wake check", state.wakes.length === 3 && state.transcribes.length >= 1, JSON.stringify([state.wakes.length, state.transcribes.length]));
    await pageW.click("#voice-cancel");
    await waitFor(pageW, () => window.mfVoice.state === "idle" && window.mfVoice.stt.standbyOn(), 3000, "standby again");
    check("wake: listening over → the standby again", await standbyW());
    await pageW.click("#voice-gear");
    await sleep(200);
    const diagW = await W(() => document.getElementById("voice-diag").textContent);
    check("wake: Diagnostics says 'wake 0.x s' (the word's end → listening)", /^Wake word: .*→ wake, \d+ ms, wake \d\.\d s \(the word's end → listening\)/m.test(diagW), diagW.replace(/\n/g, " | "));
    await pageW.click("#voice-done");
    await waitFor(pageW, () => window.mfVoice.stt.standbyOn(), 3000, "standby after the sheet");
    // The page speaking: no standby, and none for 500 ms after.
    state.wakeReply = { wake: false, text: "the" };
    await W(() => { window.__speakMs = 1500; });
    await W(() => window.mfVoice.onEvent({ seq: 0, event: "session.activity_changed", session: "api-docs", old: "working", new: "idle", ts: Date.now() / 1000, data: { title: "api-docs", say: "Docs are built." } }));
    await waitFor(pageW, () => !!window.mfVoice.speech.playing, 3000, "speaking");
    await sleep(100);
    check("wake: while the page speaks the standby is paused", !(await W(() => window.mfVoice.stt.standbyOn() || document.getElementById("voice-btn").hasAttribute("data-standby"))));
    const wakesSp = state.wakes.length;
    await sleep(500);
    await lone(500);
    await waitFor(pageW, () => !window.mfVoice.speech.playing, 4000, "spoken");
    await sleep(150);
    check("wake: …and stays paused just after it", !(await W(() => window.mfVoice.stt.standbyOn())));
    await waitFor(pageW, () => window.mfVoice.stt.standbyOn(), 2000, "standby after speech");
    check("wake: …back ~500 ms after; nothing it said was checked", (await W(() => window.mfVoice.stt.standbyOn())) && state.wakes.length === wakesSp, JSON.stringify(state.wakes.length - wakesSp));
    await W(() => { window.__speakMs = 15; });
    // Hidden: the standby stops; visible: it comes back.
    await W(() => { window.__hidden = true; document.dispatchEvent(new Event("visibilitychange")); });
    await sleep(100);
    check("wake: hidden → the standby stops", !(await W(() => window.mfVoice.stt.standbyOn() || document.getElementById("voice-btn").hasAttribute("data-standby"))));
    await W(() => { window.__hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
    await waitFor(pageW, () => window.mfVoice.stt.standbyOn(), 3000, "standby after visible");
    check("wake: visible → the standby again", await standbyW());
    // The grace never gives the mic back while the standby runs.
    const stopsW = await W(() => window.__stops);
    await W(() => { window.mfVoice.keepMs = 300; });
    await sleep(900);
    check("wake: the warm-mic grace never releases the stream under the standby", (await W(() => window.mfVoice.stt.warm())) && (await W(() => window.__stops)) === stopsW);
    // The sheet: the switch, its hint, and the pref saved.
    await pageW.click("#voice-gear");
    await sleep(200);
    const swW = await W(() => {
      const el = document.getElementById("voice-wake");
      const label = el.closest("label").textContent.replace(/\s+/g, " ");
      return { checked: el.checked, label, note: document.getElementById("voice-wake-note").textContent };
    });
    check("wake: the sheet's switch “Listen for “MindFlock””, on, with the mic-indicator and iPhone hints", swW.checked && /Listen for “MindFlock”/.test(swW.label) && /mic indicator/.test(swW.label) && /iPhone/.test(swW.label) && /mic is open/.test(swW.note), JSON.stringify(swW));
    await pageW.click("#voice-wake");
    await sleep(200);
    check("wake: switched off → saved, the standby stops", (await W(() => !window.mfVoice.prefs.wake && JSON.parse(localStorage.getItem("cs_voice")).wake === false && !window.mfVoice.stt.standbyOn())));
    await pageW.click("#voice-wake");
    await waitFor(pageW, () => window.mfVoice.stt.standbyOn(), 3000, "switched on");
    check("wake: switched on → saved, the standby runs", (await W(() => window.mfVoice.prefs.wake && JSON.parse(localStorage.getItem("cs_voice")).wake === true)));
    await pageW.click("#voice-done");
    // 409: no recognizer ready → the standby goes off and says why.
    state.wakeStatus = 409;
    await sleep(1700);
    await lone(600);
    await waitFor(pageW, () => !window.mfVoice.prefs.wake, 4000, "409 → off");
    const off409 = await W(() => ({ on: window.mfVoice.prefs.wake, standby: window.mfVoice.stt.standbyOn(), attr: document.getElementById("voice-btn").hasAttribute("data-standby"), err: document.getElementById("voice-error").textContent, saved: JSON.parse(localStorage.getItem("cs_voice")).wake }));
    check("wake: 409 → the standby off (saved), the strip says why", !off409.on && !off409.standby && !off409.attr && off409.saved === false && /no speech recognition ready/.test(off409.err), JSON.stringify(off409));
    await pageW.click("#voice-gear");
    await sleep(200);
    check("wake: …the sheet's switch is off, the reason under it", (await W(() => !document.getElementById("voice-wake").checked && /no speech recognition ready/.test(document.getElementById("voice-wake-note").textContent))));
    await pageW.click("#voice-done");
    state.wakeStatus = 0;
    await pageW.close();

    // The phone's own recognizer: the stream goes back while it listens;
    // and a refused microphone turns the standby off.
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true, server_ready: { stt: true, tts: false, stt_backend: "builtin" }, backends: { stt: ["builtin"], tts: [] } };
    state.wakes = []; state.wakeReply = { wake: true, text: "Mind flock" };
    const pageP = await ctx.newPage();
    pageP.on("pageerror", (e) => errors.push(String(e)));
    pageP.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
    await pageP.route("**/api/**", mock);
    await pageP.addInitScript(wakeInit, false);
    await pageP.addInitScript(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ wake: true })); } catch (e) {} });
    await pageP.goto(`http://127.0.0.1:${PORT}/mobile.html?s=invoice-parser`, { waitUntil: "load" });
    await waitFor(pageP, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Invoice parser", 6000, "phone wake page loaded");
    const P = (fn) => pageP.evaluate(fn);
    await pageP.click("#voice-btn");
    await waitFor(pageP, () => window.mfVoice.state === "listening" && window.mfVoice.stt.name === "phone", 4000, "phone listening");
    await pageP.click("#voice-cancel");
    await waitFor(pageP, () => window.mfVoice.state === "idle" && document.getElementById("voice-btn").hasAttribute("data-standby") && window.__tone, 3000, "phone standby");
    check("wake: with the phone's recognizer the standby still runs (the server's warm mic)", (await P(() => document.getElementById("voice-btn").hasAttribute("data-standby"))));
    await sleep(700);
    await pageP.evaluate(() => window.__tone(0.4)); await sleep(600); await pageP.evaluate(() => window.__tone(0.002));
    await waitFor(pageP, () => window.mfVoice.state === "listening", 4000, "phone woke");
    await sleep(200);
    check("wake: woke → the phone's recognizer listens and the stream was given back to it", (await P(() => window.mfVoice.state === "listening" && window.mfVoice.stt.name === "phone" && !document.getElementById("voice-btn").hasAttribute("data-standby"))) && (await P(() => window.__stops)) >= 1 && state.wakes.length === 1, JSON.stringify([state.wakes.length, await P(() => window.__stops)]));
    await P(() => { window.__deny = true; });
    await pageP.click("#voice-cancel");
    await waitFor(pageP, () => !window.mfVoice.prefs.wake, 3000, "denied → off");
    const deny = await P(() => ({ on: window.mfVoice.prefs.wake, err: document.getElementById("voice-error").textContent, attr: document.getElementById("voice-btn").hasAttribute("data-standby") }));
    check("wake: the microphone refused → the standby off, the strip says why", !deny.on && !deny.attr && /permission denied/.test(deny.err), JSON.stringify(deny));
    await pageP.close();
    state.voiceCfg = null;

    // ---- the server's voice, sentence by sentence: a slow voice (8 ms a
    // character to make, like the owner's own on a CPU) starts after the
    // first sentence is made, not the whole line; the next is asked for as
    // one starts playing; back to back; cancel drops the rest.
    state.voiceCfg = {
      stt: "phone", tts: "server", approve_dialogs: true,
      server_ready: { stt: false, tts: true, tts_backend: "voicelab" },
      backends: { stt: [], tts: ["voicelab"] },
    };
    state.speaks = [];
    const pageS = await ctx.newPage();
    pageS.on("pageerror", (e) => errors.push(String(e)));
    await pageS.route("**/api/**", mock);
    await pageS.addInitScript(() => {
      // Every clip the page starts and stops, on the wall clock. A clip
      // "plays" for its duration by the wall clock (headless Chrome's fake
      // audio sink can render in bursts, ending a clip early): `ended`
      // fires then, unless it was stopped.
      window.__plays = [];
      AudioBufferSourceNode.prototype.start = function () {
        const rec = { at: Date.now(), dur: this.buffer ? this.buffer.duration : 0, stopped: 0 }, src = this;
        window.__plays.push(rec);
        this.__rec = rec;
        setTimeout(() => { rec.endedAt = Date.now(); if (!rec.stopped && src.onended) src.onended({}); }, rec.dur * 1000);
      };
      AudioBufferSourceNode.prototype.stop = function () { if (this.__rec) this.__rec.stopped = Date.now(); };
      // Confirmations off here: their prefetch would add /speak requests to the sentence checks (tested below).
      try { localStorage.setItem("cs_voice", JSON.stringify({ replies: "all", confirm: false })); } catch (e) {}
      window.__caps = [];
      document.addEventListener("DOMContentLoaded", () => {
        const el = document.getElementById("voice-caption");
        new MutationObserver(() => window.__caps.push(el.textContent)).observe(el, { childList: true, characterData: true, subtree: true });
      });
    });
    await pageS.goto(`http://127.0.0.1:${PORT}/mobile.html?s=api-docs`, { waitUntil: "load" });
    await waitFor(pageS, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "API docs", 5000, "speak page loaded");
    await pageS.click("#voice-btn");   // the tap: the AudioContext, voice on
    await waitFor(pageS, () => window.mfVoice.state === "listening", 3000, "speak page listening");
    await pageS.click("#voice-cancel");
    await waitFor(pageS, () => window.mfVoice.state === "idle" && window.mfVoice.tts.name === "server", 3000, "server voice");
    const S = (fn, arg) => pageS.evaluate(fn, arg);
    const LINE = "I fixed the login bug in the auth module today. The token check was off by one. Should I push it now?";
    const SENTS = ["I fixed the login bug in the auth module today.", "The token check was off by one.", "Should I push it now?"];
    check("speak: the AudioContext is running (the server's voice plays through it)", (await S(() => window.mfVoice.audioCtx && window.mfVoice.audioCtx.state)) === "running");
    const t0S = await S((line) => { window.__said = 0; const t = Date.now(); window.mfVoice.tts.speak(line).then(() => { window.__said = Date.now(); }); return t; }, LINE);
    await waitFor(pageS, () => window.__said > 0, 8000, "the line said");
    const playsS = await S(() => window.__plays.slice());
    const sp = state.speaks.slice();
    check("speak: one /api/voice/speak per sentence, in order", JSON.stringify(sp.map((x) => x.text)) === JSON.stringify(SENTS), JSON.stringify(sp.map((x) => x.text)));
    const firstSound = playsS.length ? playsS[0].at - t0S : -1, wholeMs = LINE.length * state.speakMsPerChar;
    check("speak: the first sound starts once sentence 1 is made — not the whole line (" + firstSound + " ms; the whole line takes " + wholeMs + " ms to make)", playsS.length === 3 && firstSound >= SENTS[0].length * state.speakMsPerChar - 20 && firstSound < SENTS[0].length * state.speakMsPerChar + 250 && firstSound < wholeMs - 300, JSON.stringify({ firstSound, wholeMs, plays: playsS }));
    console.log("  speak: first sound " + firstSound + " ms after speak() (sentence 1 takes " + SENTS[0].length * state.speakMsPerChar + " ms to make, the whole line " + wholeMs + " ms)");
    check("speak: one ahead — sentence 2 asked for only as 1 starts playing, 3 as 2 does", sp.length === 3 && sp[1].at >= playsS[0].at - 60 && sp[2].at >= playsS[1].at - 60 && sp[1].at >= sp[0].done - 5, JSON.stringify({ sp, plays: playsS }));
    // Against the moment the sink reported the clip ended (headless Chrome's
    // timers can fire late or early; what matters is that the next clip
    // waited for `ended`), never the nominal at + duration.
    const gaps = playsS.slice(1).map((x, i) => x.at - (playsS[i].endedAt || (playsS[i].at + playsS[i].dur * 1000)));
    check("speak: back to back — each sentence starts as the one before ends (no overlap, gaps < 120 ms)", gaps.length === 2 && gaps.every((g) => g > -30 && g < 120), JSON.stringify(gaps));
    check("speak: speak() resolves once the whole line is said", (await S(() => window.__said)) >= playsS[2].at + playsS[2].dur * 1000 - 60);
    const diagS = await S(() => { window.mfVoice.diag && window.mfVoice.diag(); return window.mfVoice.rec.lastSpeak; });
    check("speak: Diagnostics has the first sound and the sentence count", diagS && diagS.first > 0 && diagS.parts === 3 && diagS.backend === "voicelab", JSON.stringify(diagS));
    // Cancel while sentence 1 plays: it stops, 2 (asked for already) is
    // aborted, 3 is never asked for, nothing else plays.
    state.speaks = [];
    await S(() => { window.__plays = []; });
    const LINE2 = "Starting the long deploy to staging right now. Then the database migration runs. Then the smoke tests run. Then I report back.";
    await S((line) => { window.__said2 = 0; window.mfVoice.tts.speak(line).then(() => { window.__said2 = Date.now(); }); }, LINE2);
    await waitFor(pageS, () => window.__plays.length >= 1, 4000, "sentence 1 playing");
    await S(() => window.mfVoice.tts.cancel());
    await sleep(2500);
    const playsC = await S(() => window.__plays.slice());
    check("speak: cancel stops the sentence playing and drops the rest (one clip played, it was stopped)", playsC.length === 1 && playsC[0].stopped > 0, JSON.stringify(playsC));
    check("speak: …sentence 3 was never asked for (only 1 and the one ahead)", state.speaks.length === 2, JSON.stringify(state.speaks.map((x) => x.text)));
    // Through the queue: the strip says Speaking… across the whole line —
    // never the line's text, never blank between sentences.
    await S(() => { window.__caps = []; window.__plays = []; });
    state.speaks = [];
    await S((say) => window.mfVoice.onEvent({ seq: 0, event: "session.activity_changed", session: "api-docs", old: "working", new: "idle", ts: Date.now() / 1000, data: { title: "api-docs", say } }), LINE);
    await waitFor(pageS, () => window.__plays.length >= 3 && !window.mfVoice.speech.playing, 8000, "the queued line said");
    const capsS = await S(() => window.__caps.slice());
    const firstSp = capsS.indexOf("Speaking…"), lastSp = capsS.lastIndexOf("Speaking…");
    check("speak: the strip says 'Speaking…' for the whole line (no gap between sentences) and never the line's text", firstSp >= 0 && capsS.every((c) => ["", "Listening…", "Speaking…", "Quiet."].includes(c)) && capsS.slice(firstSp, lastSp + 1).every((c) => c === "Speaking…") && state.speaks.length === 3, JSON.stringify(capsS));
    // ---- spoken confirmations through the server voice: made ahead only
    // for a voice made on this machine, then played from memory.
    await waitFor(pageS, () => !window.mfVoice.speech.playing, 4000, "quiet before the nods");
    state.speaks = [];
    await S(() => { window.mfVoice.prefs.confirm = true; window.mfVoice.nodPre.done = false; window.mfVoice.nodPrefetch(false); });
    await sleep(500);
    check("nod: no prefetch for a voice that may cost per line (voicelab), before a line it made has played", state.speaks.length === 0, JSON.stringify(state.speaks.map((x) => x.text)));
    await S(() => { window.mfVoice.cfg.server_ready.tts_backend = "local"; window.mfVoice.nodPrefetch(false); });
    for (let t = Date.now(); state.speaks.length < 4 && Date.now() - t < 5000; ) await sleep(50);
    await sleep(300);
    const pre = state.speaks.map((x) => x.text);
    check("nod: a local voice → the confirmations made ahead, once each", ["Cleared.", "Stopped.", "Over and out."].every((t) => pre.includes(t)) && pre.some((t) => /^Sent\./.test(t)) && new Set(pre).size === pre.length, JSON.stringify(pre));
    await S(() => window.mfVoice.nodPrefetch(false));
    await sleep(300);
    check("nod: …never twice", state.speaks.length === pre.length);
    state.speaks = [];
    await S(() => { window.__plays = []; });
    await S(() => window.mfVoice.tts.speak("Cleared.", { cache: true }));
    check("nod: a line made ahead plays from memory — no request", state.speaks.length === 0 && (await S(() => window.__plays.length)) === 1, JSON.stringify(state.speaks));
    await S(() => window.mfVoice.tts.speak("All set here.", { cache: true }));
    await S(() => window.mfVoice.tts.speak("All set here.", { cache: true }));
    check("nod: …and a new one is asked for once, the second play from memory", state.speaks.filter((x) => x.text === "All set here.").length === 1, JSON.stringify(state.speaks.map((x) => x.text)));
    await pageS.close();
    state.voiceCfg = null;

    // ---- chimes and confirmations on the phone's own voice: a rising chime
    // whenever listening starts, a falling one whenever it stops (never on
    // "over", which listens on), held while a line is said, off with Sounds;
    // "Sent." / "Cleared." / "Stopped." / "Sent. Over and out." spoken.
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    const pageC = await ctx.newPage();
    pageC.on("pageerror", (e) => errors.push(String(e)));
    await pageC.addInitScript(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} });
    await pageC.route("**/api/**", mock);
    await pageC.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(pageC, () => window.mfVoice && document.getElementById("picker-title").textContent === "Login fix", 5000, "chime page loaded");
    const C = (fn, arg) => pageC.evaluate(fn, arg);
    const chimes = () => C(() => Object.assign({ start: 0, stop: 0 }, window.mfVoice.chimes || {}));
    const ch0 = await chimes();
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "chime listening");
    check("chime: Sounds on by default; the tap's start → the rising chime", (await C(() => window.mfVoice.prefs.sounds)) === true && (await chimes()).start === ch0.start + 1 && (await chimes()).stop === ch0.stop, JSON.stringify(await chimes()));
    const sendsC = posts("/send").length;
    await C(() => window.__hear("fix the login bug"));
    await sleep(200);
    await C(() => window.__hear("now over"));
    for (let t = Date.now(); posts("/send").length === sendsC && Date.now() - t < 3000; ) await sleep(30);
    await sleep(300);
    check("chime: 'over' sends and listens on — no chime; 'Sent.' said", (await chimes()).stop === ch0.stop && (await chimes()).start === ch0.start + 1 && (await vstate(pageC)) === "listening" && (await said(pageC, "Sent.")), JSON.stringify(await chimes()));
    await C(() => window.__hear("disregard"));
    await sleep(300);
    check("chime: 'disregard' — no chime either", (await chimes()).stop === ch0.stop);
    await C(() => window.__hear("fix the docs"));
    await sleep(150);
    await C(() => window.__hear("over and out"));
    await waitFor(pageC, () => window.mfVoice.state === "working", 4000, "over and out sent");
    // The chime comes once "Sent. Over and out." is over (never over it).
    await pageC.waitForFunction((n) => ((window.mfVoice.chimes || {}).stop || 0) > n, ch0.stop, { timeout: 3000, polling: 50 }).catch(() => console.log("  timeout waiting for the stop chime"));
    check("chime: 'over and out' → the falling chime", (await chimes()).stop === ch0.stop + 1, JSON.stringify(await chimes()));
    await C(() => { window.mfVoice.state = "idle"; });
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "chime listening 2");
    await pageC.click("#voice-cancel");
    await waitFor(pageC, () => window.mfVoice.state === "idle", 3000, "cancelled");
    check("chime: start again, then Cancel → the falling chime", (await chimes()).start === ch0.start + 2 && (await chimes()).stop === ch0.stop + 2, JSON.stringify(await chimes()));
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "chime listening 3");
    await C(() => window.__hear("that's all"));
    await waitFor(pageC, () => window.mfVoice.state === "idle", 3000, "stopped by phrase");
    await sleep(200);
    check("chime: a stop phrase → the falling chime, and 'Stopped.' said", (await chimes()).stop === ch0.stop + 3 && (await said(pageC, "Stopped.")), JSON.stringify(await chimes()));
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "chime listening 4");
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "idle", 3000, "tap stopped");
    check("chime: the second tap → the falling chime", (await chimes()).start === ch0.start + 4 && (await chimes()).stop === ch0.stop + 4, JSON.stringify(await chimes()));
    // Held while a line is said, then played.
    await C(() => { window.__speakMs = 500; window.mfVoice.speakLine("A long enough line to hold a chime."); });
    await sleep(150);
    const heldC = await C(() => window.mfVoice.chime("stop"));
    check("chime: muted while a line is said (held)", heldC === false && (await chimes()).stop === ch0.stop + 4);
    await waitFor(pageC, () => !window.mfVoice.speech.playing, 3000, "line said");
    await sleep(100);
    check("chime: …and played once it is over", (await chimes()).stop === ch0.stop + 5, JSON.stringify(await chimes()));
    await C(() => { window.__speakMs = 15; window.mfVoice.state = "idle"; });
    // Sounds off: no chime either way; saved.
    await C(() => { const el = document.getElementById("voice-sounds"); el.checked = false; el.dispatchEvent(new Event("change")); });
    const chOff = await chimes();

    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "listening, sounds off");
    await pageC.click("#voice-cancel");
    await waitFor(pageC, () => window.mfVoice.state === "idle", 3000, "idle, sounds off");
    check("chime: Sounds off → no chime at the start or the stop; the choice saved", JSON.stringify(await chimes()) === JSON.stringify(chOff) && (await C(() => JSON.parse(localStorage.getItem("cs_voice")).sounds)) === false, JSON.stringify([chOff, await chimes()]));
    // Confirmations off: silent.
    await C(() => { const el = document.getElementById("voice-confirm"); el.checked = false; el.dispatchEvent(new Event("change")); window.__spoken = []; });
    await pageC.click("#voice-btn");
    await waitFor(pageC, () => window.mfVoice.state === "listening", 3000, "listening, confirmations off");
    await C(() => window.__hear("disregard"));
    await sleep(300);
    check("nod: Spoken confirmations off → 'disregard' clears silently", !(await said(pageC, "Cleared.")) && (await C(() => JSON.parse(localStorage.getItem("cs_voice")).confirm)) === false);
    await pageC.click("#voice-cancel");
    await pageC.close();
    state.voiceCfg = null;

    // ---- a stop never cuts its own confirmation: "Stopped." (a stop phrase),
    // "Over and out." (nothing to send), "Sent. Over and out." and "Cleared."
    // are said whole — the speaker reports each line finished, nothing is
    // cancelled, the falling chime comes after the line (never over it) and a
    // reply that waited for the mic after both; "disregard" listens on, and
    // the phone's recognizer, ended meanwhile (Android), restarts only once
    // "Cleared." is over.
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true };
    const pageK = await ctx.newPage();
    pageK.on("pageerror", (e) => errors.push(String(e)));
    await pageK.addInitScript(() => { try { localStorage.removeItem("cs_voice"); } catch (e) {} });
    await pageK.route("**/api/**", mock);
    await pageK.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(pageK, () => window.mfVoice && document.getElementById("picker-title").textContent === "Login fix", 5000, "stop-nod page loaded");
    const K = (fn, arg) => pageK.evaluate(fn, arg);
    // The line's log entry, the cancels and the chime tones since `mark`.
    const markK = () => K(() => ({ cancels: window.__cancels, osc: (window.__oscAt || []).length, log: (window.__speakLog || []).length, stops: (window.mfVoice.chimes || {}).stop || 0 }));
    const sinceK = (m, text) => K(([m, text]) => {
      const log = (window.__speakLog || []).slice(m.log);
      return { line: log.find((x) => x.t === text) || null, lines: log.map((x) => x.t), cancels: window.__cancels - m.cancels,
               osc: (window.__oscAt || []).slice(m.osc), stops: ((window.mfVoice.chimes || {}).stop || 0) - m.stops };
    }, [m, text]);
    // Whole, not cut, the chime after it: what each check asks of `r`.
    const wholeK = (r) => !!r.line && r.line.end > 0 && r.cancels === 0 && r.stops === 1 && r.osc.length >= 1 && r.osc[0] >= r.line.end;
    const listenK = async (label) => {
      await K(() => { window.mfVoice.state = "idle"; });
      await pageK.click("#voice-btn");
      return waitFor(pageK, () => window.mfVoice.state === "listening", 3000, label);
    };
    // waitFor passes no argument: these take one.
    const waitK = (fn, arg, ms, label) => pageK.waitForFunction(fn, arg, { timeout: ms, polling: 50 }).then(() => true, () => { console.log("  timeout waiting for " + label); return false; });
    // Said and over, and the chime held for it played (or nothing more to come).
    const saidK = (m, text) => waitK(([n, t]) => (window.__speakLog || []).slice(n).some((x) => x.t === t && x.end > 0) && !window.mfVoice.speech.playing && !window.mfVoice.chimeWait, [m, text], 4000, "said " + text);
    await K(() => { window.__speakMs = 400; });

    // A stop phrase, with a reply waiting for the mic.
    await listenK("stop-nod listening");
    await K(() => window.__hear("fix the docs"));
    await sleep(150);
    let mK = await markK();
    await K(() => { window.mfVoice.pendingSay = { say: "Docs look fine.", ts: Date.now() / 1000 }; });
    await K(() => window.__hear("that's all"));
    await saidK(mK.log, "Stopped.");
    await waitK((n) => (window.__speakLog || []).slice(n).some((x) => x.t === "Docs look fine." && x.end > 0), mK.log, 4000, "reply after the stop");
    let rK = await sinceK(mK, "Stopped.");
    check("stop-nod: a stop phrase → 'Stopped.' said whole (finished, nothing cancelled), the falling chime after it", wholeK(rK), JSON.stringify(rK));
    const replyK = (await K((n) => (window.__speakLog || []).slice(n).find((x) => x.t === "Docs look fine."), mK.log)) || {};
    check("stop-nod: …the reply that waited for the mic after both, the words kept in the box", rK.lines[0] === "Stopped." && replyK.start >= rK.osc[0] && (await K(() => document.getElementById("compose").value)) === "fix the docs", JSON.stringify([rK.lines, rK.osc, replyK]));
    await K(() => { document.getElementById("compose").value = ""; });

    // "over and out" with nothing to send.
    await listenK("stop-nod listening 2");
    mK = await markK();
    await K(() => window.__hear("over and out"));
    await saidK(mK.log, "Over and out.");
    await sleep(150);
    rK = await sinceK(mK, "Over and out.");
    check("stop-nod: 'over and out', nothing to send → 'Over and out.' whole, the chime after it", wholeK(rK) && (await vstate(pageK)) === "idle", JSON.stringify(rK));

    // "over and out" with words: "Sent. Over and out." whole, then the chime.
    await listenK("stop-nod listening 3");
    await K(() => window.__hear("fix the docs"));
    await sleep(150);
    mK = await markK();
    await K(() => window.__hear("over and out"));
    // The phone's voice says it sentence by sentence: the chime after the last.
    await saidK(mK.log, "Over and out.");
    await sleep(150);
    rK = await sinceK(mK, "Over and out.");
    check("stop-nod: 'over and out' with words → 'Sent. Over and out.' whole, the chime after it, sent", wholeK(rK) && rK.lines.slice(0, 2).join(" ") === "Sent. Over and out." && ["working", "idle", "speaking"].includes(await vstate(pageK)), JSON.stringify(rK));

    // "disregard": "Cleared." over a listen that goes on — no chime, nothing
    // cancelled; the recognizer that ended meanwhile restarts after the line.
    await listenK("stop-nod listening 4");
    await K(() => window.__hear("fix the docs"));
    await sleep(150);
    mK = await markK();
    const startsK = await K(() => window.__recStarts || 0);
    await K(() => window.__hear("disregard"));
    await sleep(100);
    await K(() => { const r = window.__rec; r.started = false; if (r.onend) r.onend(); });   // Android: it ends after every phrase
    await saidK(mK.log, "Cleared.");
    await waitK((n) => (window.__recStarts || 0) > n, startsK, 2000, "recognizer back");
    rK = await sinceK(mK, "Cleared.");
    const recK = await K(() => ({ at: window.__recStartAt, starts: window.__recStarts || 0 }));
    check("stop-nod: 'disregard' → 'Cleared.' whole over the listen (no chime, nothing cancelled), still listening", !!rK.line && rK.line.end > 0 && rK.cancels === 0 && rK.stops === 0 && rK.osc.length === 0 && (await vstate(pageK)) === "listening", JSON.stringify(rK));
    check("stop-nod: …the phone's recognizer, ended meanwhile, restarts only once 'Cleared.' is over", recK.starts > startsK && recK.at >= rK.line.end, JSON.stringify([recK, rK.line]));
    await sleep(250);   // past the line's echo window (voiceEchoing)
    await K(() => window.__hear("fix the tests"));
    await sleep(150);
    check("stop-nod: …and hears the next phrase", /fix the tests/.test(await K(() => document.getElementById("compose").value)) && (await vstate(pageK)) === "listening");
    await K(() => { window.__speakMs = 15; });
    await pageK.click("#voice-cancel");
    await pageK.close();
    state.voiceCfg = null;

    // ---- control words act from the streamed guess: once the voice has been
    // quiet 250 ms after one (never before), "over" ends the phrase at once
    // and goes with /end's final (or the fast words after 2.5 s);
    // "disregard" acts on the guess alone.
    state.voiceCfg = {
      stt: "server", tts: "phone", approve_dialogs: true,
      server_ready: { stt: true, tts: false, stt_backend: "whisper", stt_stream: true },
      backends: { stt: ["whisper"], tts: [] },
    };
    state.voiceGet = { on: false, say: null, has_question: false, source: "none", ts: null };
    const pageG = await ctx.newPage();
    pageG.on("pageerror", (e) => errors.push(String(e)));
    await pageG.addInitScript(() => {
      navigator.mediaDevices.getUserMedia = function () {
        const ac = new AudioContext();
        const osc = ac.createOscillator(), gain = ac.createGain(), dst = ac.createMediaStreamDestination();
        gain.gain.value = 0.002;
        osc.frequency.value = 440;
        osc.connect(gain); gain.connect(dst); osc.start();
        window.__tone = (g) => { gain.gain.value = g; };
        return Promise.resolve(dst.stream);
      };
      window.__boxes = [];
      setInterval(() => { const b = document.getElementById("compose"); if (b && b.value !== window.__lastBox) { window.__lastBox = b.value; window.__boxes.push(b.value); } }, 10);
      try { localStorage.setItem("cs_voice", JSON.stringify({ confirm: false })); } catch (e) {}
    });
    await pageG.route("**/api/**", mock);
    await pageG.goto(`http://127.0.0.1:${PORT}/mobile.html?s=api-docs`, { waitUntil: "load" });
    await waitFor(pageG, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "API docs", 5000, "guess page loaded");
    await pageG.evaluate(() => { document.getElementById("compose").value = "draft:"; });
    await pageG.click("#voice-btn");
    await waitFor(pageG, () => window.mfVoice.state === "listening" && window.mfVoice.stt.name === "server" && window.__tone, 4000, "guess listening");
    // The next phrase's stream: wait for it to open, script it, speak.
    let usedG = 0;
    const nextSid = async () => {
      for (let t = Date.now(); state.streamOpens.length <= usedG && Date.now() - t < 4000; ) await sleep(20);
      usedG = state.streamOpens.length;
      return "s" + usedG;
    };
    const sidGA = await nextSid();
    state.streamScript[sidGA] = [{ committed: "", partial: "fix the" }, { committed: "fix the", partial: "bug over" }, { committed: "bug over", partial: "" }];
    state.streamEndReply[sidGA] = { committed: "", final: "fix the bug over", final_ms: 300, same: true };
    const sendsG = posts("/send").length;
    await sleep(400);
    await pageG.evaluate(() => window.__tone(0.4));
    for (let t = Date.now(); state.streamChunks.filter((c) => c.id === sidGA).length < 4 && Date.now() - t < 4000; ) await sleep(20);
    await sleep(200);
    check("guess: 'over' in the guess while the voice goes on → nothing yet (no /end, nothing sent)", state.streamEnds.indexOf(sidGA) < 0 && posts("/send").length === sendsG, JSON.stringify([state.streamEnds, state.streamChunks.filter((c) => c.id === sidGA).length]));
    const offGA = Date.now();
    await pageG.evaluate(() => window.__tone(0.002));
    for (let t = Date.now(); posts("/send").length === sendsG && Date.now() - t < 4000; ) await sleep(10);
    const tookGA = Date.now() - offGA;
    const sentGA = posts("/send").slice(sendsG);
    check("guess: …quiet after it → /end at once and the box sent, well before the silence stop (" + tookGA + " ms)", state.streamEnds.indexOf(sidGA) >= 0 && sentGA.length === 1 && sentGA[0].body.text === "draft: fix the bug" && tookGA < 1000, JSON.stringify([tookGA, sentGA.map((x) => x.body.text)]));
    check("guess: …'over' never reached the box, and listening goes on", !(await pageG.evaluate(() => window.__boxes.some((b) => /over/.test(b)))) && (await vstate(pageG)) === "listening");
    await pageG.click("#voice-gear");
    await sleep(200);
    const diagG = await pageG.evaluate(() => document.getElementById("voice-diag").textContent);
    check("guess: Diagnostics says 'over → sent N s' (the word's end → the Enter)", /Last control: over → sent \d\.\d s/.test(diagG), diagG.replace(/\n/g, " | "));
    await pageG.click("#voice-done");
    // A slow accurate pass: past 2.5 s the fast words go, a late final is ignored.
    const sidGB = await nextSid();
    state.streamScript[sidGB] = [{ committed: "", partial: "ship" }, { committed: "ship", partial: "it over" }, { committed: "it over", partial: "" }];
    state.streamEndReply[sidGB] = { committed: "", final: "skip it over", final_ms: 3900, same: false };
    state.streamEndDelay[sidGB] = 4000;
    const sendsG2 = posts("/send").length;
    await sleep(300);
    await pageG.evaluate(() => window.__tone(0.4));
    for (let t = Date.now(); state.streamChunks.filter((c) => c.id === sidGB).length < 4 && Date.now() - t < 4000; ) await sleep(20);
    const offGB = Date.now();
    await pageG.evaluate(() => window.__tone(0.002));
    for (let t = Date.now(); posts("/send").length === sendsG2 && Date.now() - t < 6000; ) await sleep(10);
    const tookGB = Date.now() - offGB;
    const sentGB = posts("/send").slice(sendsG2);
    check("guess: /end slower than 2.5 s → the fast words sent at ~2.5 s (" + tookGB + " ms)", sentGB.length === 1 && sentGB[0].body.text === "ship it" && tookGB >= 2400 && tookGB < 3600, JSON.stringify([tookGB, sentGB.map((x) => x.body.text)]));
    await sleep(2000);
    check("guess: …and the late final changed nothing (no second send)", posts("/send").length === sendsG2 + 1 && (await vstate(pageG)) === "listening");
    // "disregard": acts on the guess, the stream dropped (never ended).
    await pageG.evaluate(() => { document.getElementById("compose").value = "throw this away"; });
    const sidGD = await nextSid();
    state.streamScript[sidGD] = [{ committed: "", partial: "no" }, { committed: "no", partial: "disregard" }, { committed: "disregard", partial: "" }];
    await sleep(300);
    await pageG.evaluate(() => window.__tone(0.4));
    for (let t = Date.now(); state.streamChunks.filter((c) => c.id === sidGD).length < 4 && Date.now() - t < 4000; ) await sleep(20);
    const offGD = Date.now();
    await pageG.evaluate(() => window.__tone(0.002));
    await waitFor(pageG, () => document.getElementById("compose").value === "", 2000, "disregarded");
    const tookGD = Date.now() - offGD;
    check("guess: 'disregard' acts on the guess at once (" + tookGD + " ms), no /end, the stream DELETEd", tookGD < 800 && state.streamEnds.indexOf(sidGD) < 0 && state.streamDeletes.indexOf(sidGD) >= 0 && (await vstate(pageG)) === "listening", JSON.stringify([tookGD, state.streamEnds, state.streamDeletes]));
    // A held word the next word rules out is typed, and nothing fires.
    const sidGH = await nextSid();
    state.streamScript[sidGH] = [{ committed: "", partial: "go over" }, { committed: "go over", partial: "the bridge" }, { committed: "the bridge", partial: "" }];
    await pageG.evaluate(() => { document.getElementById("compose").value = "note:"; window.__boxes = []; window.__lastBox = null; });
    const sendsGH = posts("/send").length;
    await sleep(300);
    await pageG.evaluate(() => window.__tone(0.4));
    for (let t = Date.now(); state.streamChunks.filter((c) => c.id === sidGH).length < 4 && Date.now() - t < 4000; ) await sleep(20);
    await sleep(150);
    await pageG.evaluate(() => window.__tone(0.002));
    await sleep(700);
    const boxesGH = await pageG.evaluate(() => window.__boxes.slice());
    check("guess: 'over' held back until the next word rules it out ('go' → 'go over the bridge'), nothing fired", boxesGH.includes("note: go") && boxesGH.includes("note: go over the bridge") && !boxesGH.includes("note: go over") && posts("/send").length === sendsGH && state.streamEnds.indexOf(sidGH) < 0, JSON.stringify([boxesGH, state.streamEnds.slice(-2)]));
    await pageG.click("#voice-cancel");
    await pageG.close();
    state.voiceCfg = null;

    // ---- the words from the server (Settings → Voice) and How to talk
    // A browser of its own (no tutorial flag), spoken confirmations off (no
    // echo to wait out); GET /api/voice slow, then the owner's words.
    const ctxT = await browser.newContext({ viewport: { width: 390, height: 780 }, isMobile: true, hasTouch: true, userAgent: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36" });
    await ctxT.addInitScript(INIT + `try { if (!localStorage.getItem("cs_voice")) localStorage.setItem("cs_voice", JSON.stringify({ confirm: false })); } catch (e) {}`);
    const WORDS = {
      send: ["roger"], end: ["roger and out", "roger, out"], clear: ["scrap that"],
      stop: ["halt", "halt listening", "we're done"], hush: ["silence", "pipe down"], wake: "hey jarvis",
    };
    state.voiceCfg = { stt: "phone", tts: "phone", approve_dialogs: true, server_ready: {}, backends: { stt: [], tts: [] }, words: WORDS };
    state.voiceCfgDelay = 1500;
    const pageT = await ctxT.newPage();
    pageT.on("pageerror", (e) => errors.push(String(e)));
    pageT.on("console", (msg) => { if (msg.type() === "error" && !/WebSocket|ws:\/\/|404|409/.test(msg.text())) errors.push(msg.text()); });
    await pageT.route("**/api/**", mock);
    await pageT.goto(`http://127.0.0.1:${PORT}/mobile.html?s=login-fix`, { waitUntil: "load" });
    await waitFor(pageT, () => window.mfVoice && document.getElementById("picker-title").textContent === "Login fix", 5000, "words page loaded");
    const wordsT = () => pageT.evaluate(() => window.mfVoice.wordsNow());
    const wakeLabel = () => pageT.evaluate(() => document.querySelector("#voice-sheet .voice-wake-word").textContent);
    const tutOpen = () => pageT.evaluate(() => !document.getElementById("voice-tut").classList.contains("hidden"));
    const tutText = () => pageT.evaluate(() => document.getElementById("voice-tut-list").textContent);
    const boxT = () => pageT.evaluate(() => document.getElementById("compose").value);
    const wT0 = await wordsT();
    check("words: ours before GET /api/voice answers (over / over and out / disregard, MindFlock)", !(await pageT.evaluate(() => window.mfVoice.cfg.loaded)) && wT0.send[0] === "over" && wT0.end[0] === "over and out" && wT0.clear[0] === "disregard" && (await wakeLabel()) === "MindFlock", JSON.stringify(wT0));
    await waitFor(pageT, () => window.mfVoice.cfg.loaded, 5000, "words: cfg loaded");
    const wT1 = await wordsT();
    check("words: the server's once it answers — and the sheet's wake label names its word", wT1.send[0] === "roger" && wT1.stop.join() === WORDS.stop.join() && (await wakeLabel()) === "Hey Jarvis", JSON.stringify([wT1, await wakeLabel()]));
    state.voiceCfgDelay = 0;
    // The first tap that turns voice on: How to talk, the listen waiting for Got it.
    await pageT.click("#voice-btn");
    await waitFor(pageT, () => !document.getElementById("voice-tut").classList.contains("hidden"), 3000, "tutorial opened");
    const ttT = await tutText();
    check("tutorial: opens the first time voice is turned on, and the mic waits for it", (await tutOpen()) && (await vstate(pageT)) === "idle", await vstate(pageT));
    check("tutorial: its lines name the owner's words (send, end, clear, stop, hush, wake), never ours", ["“roger”", "“roger and out”", "“scrap that”", "“halt” or “halt listening”", "“silence”", "“Hey Jarvis”"].every((x) => ttT.indexOf(x) >= 0) && !/“over|disregard|MindFlock/.test(ttT), ttT);
    check("tutorial: the gesture, the box, switching, answering, Spoken replies", /Tap the mic/.test(ttT) && /appear in the box/.test(ttT) && /session three/.test(ttT) && /next session/.test(ttT) && /“yes”/.test(ttT) && /Off, Mine/.test(ttT) && /Everything/.test(ttT), ttT);
    check("tutorial: not remembered before Got it", (await pageT.evaluate(() => localStorage.getItem("cs_voice_tutorial"))) === null);
    await pageT.click("#voice-tut-ok");
    await waitFor(pageT, () => window.mfVoice.state === "listening", 3000, "listening after Got it");
    check("tutorial: Got it closes it, remembers, and starts the listen the tap asked for", !(await tutOpen()) && (await vstate(pageT)) === "listening" && (await pageT.evaluate(() => localStorage.getItem("cs_voice_tutorial"))) === "1", await vstate(pageT));
    const hintT = await pageT.evaluate(() => window.mfVoice.caption);
    check("words: the hint names the owner's words", hintT === "Say “roger” to send, “roger and out” to stop, “scrap that” to start over", hintT);
    // The radio words, the owner's.
    const sendsT = posts("/send").length;
    await pageT.evaluate(() => window.__hear("fix the login bug"));
    await waitFor(pageT, () => document.getElementById("compose").value === "fix the login bug", 3000, "words: routed phrase");
    await pageT.evaluate(() => window.__hear("and add a test, roger."));
    for (let t = Date.now(); posts("/send").length < sendsT + 1 && Date.now() - t < 3000; ) await sleep(30);
    check("words: '… roger' sends, listening on", posts("/send").length === sendsT + 1 && posts("/send")[sendsT].body.text === "fix the login bug and add a test" && (await vstate(pageT)) === "listening", JSON.stringify(posts("/send").slice(sendsT).map((x) => x.body)));
    await pageT.evaluate(() => window.__hear("move it over"));
    await sleep(200);
    check("words: ours are words now ('… over' is typed, nothing sent)", (await boxT()) === "move it over" && posts("/send").length === sendsT + 1, await boxT());
    await pageT.evaluate(() => window.__hear("and disregard"));
    await sleep(200);
    check("words: …'disregard' too", (await boxT()) === "move it over and disregard", await boxT());
    await pageT.evaluate(() => window.__hear("Scrap that."));
    await sleep(200);
    check("words: 'scrap that' empties the box and listens on", (await boxT()) === "" && (await vstate(pageT)) === "listening", await boxT());
    await pageT.evaluate(() => window.__hear("silence"));
    await sleep(200);
    check("words: 'silence' is the hush (nothing typed, still listening)", (await boxT()) === "" && (await vstate(pageT)) === "listening", await boxT());
    await pageT.evaluate(() => window.__hear("note the logs"));
    await waitFor(pageT, () => document.getElementById("compose").value === "note the logs", 3000, "words: second routed phrase");
    await pageT.evaluate(() => window.__hear("and the trace halt listening"));
    await waitFor(pageT, () => window.mfVoice.state !== "listening", 3000, "words: halt listening");
    check("words: '… halt listening' stops, the words kept, unsent", (await vstate(pageT)) !== "listening" && (await boxT()) === "note the logs and the trace" && posts("/send").length === sendsT + 1, JSON.stringify([await vstate(pageT), await boxT()]));
    await pageT.click("#voice-btn");
    await waitFor(pageT, () => window.mfVoice.state === "listening", 3000, "words: listening again");
    check("tutorial: a later turn-on or tap never opens it again", !(await tutOpen()) && (await vstate(pageT)) === "listening");
    await pageT.evaluate(() => window.__hear("ship it, roger and out"));
    for (let t = Date.now(); posts("/send").length < sendsT + 2 && Date.now() - t < 3000; ) await sleep(30);
    await sleep(200);
    check("words: '… roger and out' (the longest first) sends the box and stops", posts("/send").length === sendsT + 2 && posts("/send")[sendsT + 1].body.text === "note the logs and the trace ship it" && (await vstate(pageT)) !== "listening", JSON.stringify([posts("/send").slice(sendsT + 1).map((x) => x.body), await vstate(pageT)]));
    // Settings → Voice changed them meanwhile: the next refetch brings them.
    state.voiceCfg = Object.assign({}, state.voiceCfg, { words: { send: ["copy"], wake: "computer" } });
    await pageT.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(pageT, () => window.mfVoice.wordsNow().send[0] === "copy", 3000, "words: refetched");
    const wT2 = await wordsT();
    check("words: a refetch (the page in front again) brings the new ones; a kind left out is ours", wT2.send[0] === "copy" && wT2.end[0] === "over and out" && wT2.clear[0] === "disregard" && (await wakeLabel()) === "Computer", JSON.stringify([wT2, await wakeLabel()]));
    await pageT.click("#voice-btn");
    await waitFor(pageT, () => window.mfVoice.state === "listening", 3000, "words: listening after the refetch");
    const hintT2 = await pageT.evaluate(() => window.mfVoice.caption);
    check("words: …the hint says them", hintT2 === "Say “copy” to send, “over and out” to stop, “disregard” to start over", hintT2);
    const sendsT2 = posts("/send").length;
    await pageT.evaluate(() => window.__hear("fix it roger"));
    await sleep(200);
    await pageT.evaluate(() => window.__hear("then copy"));
    for (let t = Date.now(); posts("/send").length < sendsT2 + 1 && Date.now() - t < 3000; ) await sleep(30);
    check("words: …'copy' sends, 'roger' is a word again", posts("/send").length === sendsT2 + 1 && posts("/send")[sendsT2].body.text === "fix it roger then", JSON.stringify(posts("/send").slice(sendsT2).map((x) => x.body)));
    await pageT.click("#voice-cancel");
    // How to talk from the ⚙ sheet: the current words, Got it closes it.
    await pageT.click("#voice-gear");
    await waitFor(pageT, () => !document.getElementById("voice-sheet").classList.contains("hidden"), 2000, "voice sheet");
    await pageT.click("#voice-tut-open");
    await waitFor(pageT, () => !document.getElementById("voice-tut").classList.contains("hidden"), 2000, "tutorial from the sheet");
    const ttT2 = await tutText();
    check("tutorial: opens again from the ⚙ sheet's How to talk, with the current words", (await tutOpen()) && ttT2.indexOf("“copy”") >= 0 && ttT2.indexOf("“Computer”") >= 0 && ttT2.indexOf("“roger”") < 0, ttT2);
    await pageT.click("#voice-tut-ok");
    await sleep(100);
    check("tutorial: Got it closes it (the voice sheet still open under it, nothing started)", !(await tutOpen()) && (await pageT.evaluate(() => !document.getElementById("voice-sheet").classList.contains("hidden"))) && (await vstate(pageT)) !== "listening", await vstate(pageT));
    await pageT.click("#voice-done");
    // Remembered across a reload: voice on again, no card.
    await pageT.evaluate(() => { try { localStorage.setItem("cs_voice", JSON.stringify({ confirm: false, on: false })); } catch (e) {} });
    await pageT.reload({ waitUntil: "load" });
    await waitFor(pageT, () => window.mfVoice && window.mfVoice.cfg.loaded && document.getElementById("picker-title").textContent === "Login fix", 5000, "words page reloaded");
    await pageT.click("#voice-btn");
    await waitFor(pageT, () => window.mfVoice.state === "listening", 3000, "listening after reload");
    check("tutorial: remembered per browser — after a reload the first tap just listens", !(await tutOpen()) && (await vstate(pageT)) === "listening", await vstate(pageT));
    await pageT.click("#voice-cancel");
    await pageT.close();
    await ctxT.close();
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
