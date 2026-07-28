/* MindFlock client event bus — the public extension API (window.mindflock).
 *
 * Loaded as a plain script BEFORE app.js and core/slots.js so both the SPA and
 * client-side addons can subscribe to live session events without polling.
 *
 * Connects to ws(s)://<host>/api/events, which replays a short backlog and
 * then streams envelopes of the shape:
 *   {"seq": int, "event": str, "session": str, "old": str|null,
 *    "new": str|null, "ts": epoch-float, "data": object}
 * Event names: session.created | session.deleted | session.paused |
 * session.resumed | session.status_changed | session.activity_changed |
 * session.stage_changed.
 *
 * Public contract (stable — extensions rely on it):
 *   mindflock.events.subscribe(eventName, cb) -> unsubscribe fn
 *       eventName is one of the session.* names or "*" (all events);
 *       cb receives the full envelope.
 *   mindflock.events.lastSeq   -> number (highest seq seen; 0 before any)
 *   mindflock.events.connected -> bool (event-stream connection state)
 *   mindflock.events.isReplay(envelope) -> bool — true when the envelope is
 *       part of the backlog replayed on (re)connect rather than a live event
 *       (its ts predates the connect epoch). One-shot consumers (toasts,
 *       notifications) should skip replays; state repaints may consume them.
 *   mindflock.events.onStatus(cb) -> unsubscribe fn;
 *       cb("connected" | "disconnected") on connection-state changes.
 *   mindflock.sessions() -> latest /api/instances snapshot (array; [] before
 *       the first poll). Fed internally by app.js via mindflock.__setSessions.
 *
 * NOTE: the server only computes *_changed events while something polls
 * GET /api/instances — the SPA keeps its 4s poll for exactly that reason.
 */
(function () {
  "use strict";

  window.mindflock = window.mindflock || {};
  var mf = window.mindflock;

  // --- session snapshot (fed by app.js) ------------------------------------ //
  var _sessions = [];
  mf.sessions = function () { return _sessions; };
  mf.__setSessions = function (arr) { _sessions = Array.isArray(arr) ? arr : []; };

  // --- subscriptions -------------------------------------------------------- //
  var _subs = {};        // eventName ("*" for all) -> array of callbacks
  var _statusSubs = [];  // connection-state callbacks

  function subscribe(eventName, cb) {
    if (typeof cb !== "function") return function () {};
    var name = eventName || "*";
    (_subs[name] = _subs[name] || []).push(cb);
    return function () {
      var list = _subs[name] || [];
      var i = list.indexOf(cb);
      if (i >= 0) list.splice(i, 1);
    };
  }

  function onStatus(cb) {
    if (typeof cb !== "function") return function () {};
    _statusSubs.push(cb);
    return function () {
      var i = _statusSubs.indexOf(cb);
      if (i >= 0) _statusSubs.splice(i, 1);
    };
  }

  // Each subscriber is isolated: one throwing callback never breaks dispatch.
  function _dispatch(env) {
    var name = env && env.event;
    var lists = [].concat(_subs[name] || [], _subs["*"] || []);
    for (var i = 0; i < lists.length; i++) {
      try { lists[i](env); } catch (e) {
        try { console.error("mindflock.events subscriber failed:", e); } catch (e2) {}
      }
    }
  }

  var _state = { lastSeq: 0, connected: false };

  // --- cursor persistence (incremental reconnect across refresh) -------------
  // Persist the highest seq seen (per tab) so a page refresh reconnects with
  // ?since=<lastSeq> and the server replays only what we missed, instead of the
  // whole ~100-envelope backlog re-dispatching (and re-repainting) on every
  // load. sessionStorage keeps this per-tab and clears on tab close, so tabs
  // don't contend over a shared cursor. _bootId pins the cursor to a specific
  // server instance — see the hello handling in onmessage.
  var _STORE_KEY = "mindflock.events.cursor";
  var _bootId = "";
  function _loadCursor() {
    try {
      var raw = sessionStorage.getItem(_STORE_KEY);
      if (!raw) return;
      var o = JSON.parse(raw);
      if (o && typeof o.lastSeq === "number" && o.lastSeq > 0) _state.lastSeq = o.lastSeq;
      if (o && typeof o.bootId === "string") _bootId = o.bootId;
    } catch (e) { /* storage unavailable (private mode / disabled) — no persistence */ }
  }
  function _saveCursor() {
    try {
      sessionStorage.setItem(_STORE_KEY,
        JSON.stringify({ lastSeq: _state.lastSeq, bootId: _bootId }));
    } catch (e) { /* ignore quota / disabled storage */ }
  }

  // --- replay detection (L6) -------------------------------------------------
  // /api/events replays a backlog (~100 envelopes) on every (re)connect, so
  // one-shot consumers (budget/clarify/PR toasts) would re-fire on stale
  // history at each page load. We record a connect epoch per connection —
  // preferring the server's own clock from the initial hello frame
  // ({"event": "hello", "server_time": epoch-float}, feature-detected; older
  // servers don't send one) and falling back to the client clock at open —
  // and expose isReplay(env): ts older than the epoch minus a small skew.
  var REPLAY_SKEW_S = 2.0;
  var _connectEpoch = 0;   // epoch seconds of the current connection

  function isReplay(env) {
    if (!env || typeof env.ts !== "number" || !_connectEpoch) return false;
    return env.ts < (_connectEpoch - REPLAY_SKEW_S);
  }

  function _setConnected(connected) {
    if (_state.connected === connected) return;
    _state.connected = connected;
    var status = connected ? "connected" : "disconnected";
    var subs = _statusSubs.slice();
    for (var i = 0; i < subs.length; i++) {
      try { subs[i](status); } catch (e) {
        try { console.error("mindflock.events status subscriber failed:", e); } catch (e2) {}
      }
    }
  }

  mf.events = {
    subscribe: subscribe,
    onStatus: onStatus,
    isReplay: isReplay,
    get lastSeq() { return _state.lastSeq; },
    get connected() { return _state.connected; },
  };

  // --- /api/events websocket with exponential-backoff reconnect ------------- //
  var RECONNECT_MIN_MS = 1000;
  var RECONNECT_MAX_MS = 30000;
  var _backoff = RECONNECT_MIN_MS;
  var _reconnectTimer = null;

  function _scheduleReconnect() {
    if (_reconnectTimer) return;
    _reconnectTimer = setTimeout(function () {
      _reconnectTimer = null;
      _connect();
    }, _backoff);
    _backoff = Math.min(_backoff * 2, RECONNECT_MAX_MS);
  }

  function _connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    // Reconnect with ?since=<lastSeq> so the backlog replay skips events we
    // already dispatched (dedupe across reconnects).
    var url = proto + "://" + location.host + "/api/events" +
      (_state.lastSeq ? "?since=" + _state.lastSeq : "");
    var ws;
    try { ws = new WebSocket(url); } catch (e) { _scheduleReconnect(); return; }
    ws.onopen = function () {
      _backoff = RECONNECT_MIN_MS;
      // Client-clock fallback; the hello frame (if the server sends one)
      // overrides this with the server's own clock so skewed clients still
      // classify replays correctly.
      _connectEpoch = Date.now() / 1000;
      _setConnected(true);
    };
    ws.onmessage = function (ev) {
      var env;
      try { env = JSON.parse(ev.data); } catch (e) { return; }
      if (env && typeof env.server_time === "number") {
        // Initial hello frame — adopt the server clock as the connect epoch.
        _connectEpoch = env.server_time;
      }
      if (env && env.event === "hello") {
        // A boot_id change means we're talking to a different server instance
        // than our persisted cursor belongs to (server restarted → seq counter
        // reset to 0). A stale ?since=<high> would make the server skip every
        // fresh event (seq <= our cursor) and strand this client — so reset the
        // cursor and reconnect clean to pull a correct backlog. Feature-detected:
        // an older server sends no boot_id, so this is a no-op there.
        var boot = typeof env.boot_id === "string" ? env.boot_id : "";
        if (boot && boot !== _bootId) {
          var hadCursor = _state.lastSeq > 0;
          _bootId = boot;
          _state.lastSeq = 0;
          _saveCursor();
          if (hadCursor) { try { ws.close(); } catch (e) {} return; }
        }
        return;   // hello carries no session state; never dispatch
      }
      if (env && typeof env.seq === "number" && env.seq > _state.lastSeq) {
        _state.lastSeq = env.seq;
        _saveCursor();
      }
      _dispatch(env);
    };
    ws.onclose = function (ev) {
      _setConnected(false);
      // 4401 = auth gate rejected us (cookie expired). Reload so the server
      // serves the login page instead of reconnecting in a tight loop.
      if (ev && ev.code === 4401) { location.reload(); return; }
      _scheduleReconnect();
    };
    ws.onerror = function () {
      // onclose follows and handles the reconnect; just make sure it fires.
      try { ws.close(); } catch (e) {}
    };
  }

  _loadCursor();   // resume from the last seen seq (per-tab) before connecting
  _connect();
})();
