// Shared WS-backed xterm helper.
//
// One implementation of the bridge every terminal pane uses: an xterm.js
// terminal wired to a backend PTY websocket. Collapses the logic that was
// hand-copied across the desktop grid, the MindFlock logs pane, the Assistant
// chat pane, and the mobile view. Addon UI (slots.js) uses this so a new
// addon's terminal pane is a one-liner.
//
// Protocol (must match webui/core/terminal.py::pump_pty):
//   - binary frames from the server are raw PTY bytes -> term.write
//   - a text frame {"type":"error","message":...} surfaces a spawn error
//   - we send {"type":"resize","cols","rows"} on open + on fit
//   - close code 4404 (instance gone) / 4409 (workspace gone) => stop, don't
//     reconnect; any other close => reconnect after 2.5s.
//
// Requires the global xterm.js (window.Terminal) + fit addon, loaded by the
// host page from /vendor/.

export class WsXterm {
  constructor({ host, wsPath, interactive = true, onGone = null, fontSize = 13 }) {
    this.host = host;
    this.wsPath = wsPath;
    this.interactive = interactive;
    this.onGone = onGone;
    this.fontSize = fontSize;
    this.ws = null;
    this.term = null;
    this.fit = null;
    this._stop = false;
    this._reconnectTimer = null;
  }

  start() {
    const Term = window.Terminal;
    const FitAddon = window.FitAddon && window.FitAddon.FitAddon;
    this.term = new Term({
      fontSize: this.fontSize,
      cursorBlink: this.interactive,
      disableStdin: !this.interactive,
      convertEol: false,
      // 2000, matching app.js's terminals: scrollback buffers were the
      // dominant heap cost with many panes; full history stays in tmux.
      scrollback: 2000,
    });
    if (FitAddon) {
      this.fit = new FitAddon();
      this.term.loadAddon(this.fit);
    }
    this.term.open(this.host);
    this._doFit();
    if (this.interactive) {
      this.term.onData((d) => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(d);
      });
      // Right-click behaves like most terminals (Windows Terminal / PuTTY):
      // copy the current selection if there is one, otherwise paste the
      // clipboard into the PTY.
      this.host.addEventListener("contextmenu", this._onContextMenu);
      // Ctrl+V pastes the local clipboard instead of forwarding ^V to the PTY
      // (mirrors app.js attachCopyOnSelect — the server-side CLI can't read
      // this machine's clipboard). Ctrl+Shift+V keeps the browser default.
      this.term.attachCustomKeyEventHandler((ev) => {
        if (ev.type === "keydown" && (ev.ctrlKey || ev.metaKey) && !ev.shiftKey &&
            !ev.altKey && (ev.key === "v" || ev.key === "V")) {
          ev.preventDefault();
          Promise.resolve(this._clipRead())
            .then((text) => { if (text && this.term && this.term.paste) this.term.paste(text); })
            .catch(() => {});
          return false;
        }
        return true;
      });
      // Auto-scroll while drag-selecting past the top/bottom edge so a
      // selection can span far more than one screen. xterm only auto-scrolls
      // when the cursor is strictly outside the terminal, but the terminal is
      // pinned to the window edge, so the cursor can never get there. We detect
      // an edge band during a drag and feed xterm a synthetic mousemove just
      // beyond the edge, reusing its own scroll + selection-extension logic.
      const doc = this.host.ownerDocument;
      this.host.addEventListener("mousedown", this._onDragStart);
      doc.addEventListener("mousemove", this._onDragMove);
      doc.addEventListener("mouseup", this._onDragEnd);
    }
    window.addEventListener("resize", this._onResize);
    this._connect();
    return this;
  }

  _onResize = () => this._doFit();

  // px band inside the top/bottom edge that arms drag auto-scroll.
  _edgeBand = 28;
  _dragging = false;

  _screenEl() {
    return this.host.querySelector(".xterm-screen") || this.host.querySelector(".xterm");
  }

  _onDragStart = (ev) => {
    if (ev.button === 0) this._dragging = true;
  };

  _onDragEnd = () => {
    this._dragging = false;
  };

  _onDragMove = (ev) => {
    // Ignore our own synthetic events (isTrusted === false) to avoid recursion.
    if (!this._dragging || !ev.isTrusted) return;
    const screen = this._screenEl();
    if (!screen) return;
    const rect = screen.getBoundingClientRect();
    const band = this._edgeBand;
    let forcedY = null;
    if (ev.clientY >= rect.bottom - band && ev.clientY <= rect.bottom) {
      // In the bottom band: push just past the bottom edge; deeper = faster.
      forcedY = rect.bottom + Math.max(1, ev.clientY - (rect.bottom - band));
    } else if (ev.clientY <= rect.top + band && ev.clientY >= rect.top) {
      // In the top band: push just past the top edge.
      forcedY = rect.top - Math.max(1, rect.top + band - ev.clientY);
    }
    if (forcedY === null) return; // outside the bands => let xterm handle it.
    // Re-dispatch at the forced Y so xterm computes a non-zero scroll amount.
    // Deferred a microtask: xterm's own document mousemove listener (added at
    // mousedown, so it runs AFTER this one) recomputes its drag-scroll amount
    // from the REAL event — inside the canvas — which would zero out what the
    // synthetic event just armed if we dispatched synchronously.
    const doc = this.host.ownerDocument;
    const cx = ev.clientX, fy = forcedY;
    queueMicrotask(() => {
      if (!this._dragging) return;
      doc.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          cancelable: true,
          view: doc.defaultView,
          clientX: cx,
          clientY: fy,
          buttons: 1,
        })
      );
    });
  };

  _onContextMenu = (ev) => {
    ev.preventDefault();
    const sel = this.term && this.term.getSelection ? this.term.getSelection() : "";
    if (sel) {
      // Copy selection, then clear it (matches Windows Terminal / PuTTY).
      this._clipWrite(sel);
      try {
        this.term.clearSelection();
      } catch (e) {}
      return;
    }
    // No selection => paste the clipboard into the PTY.
    Promise.resolve(this._clipRead())
      .then((text) => {
        if (!text) return;
        // term.paste() honors bracketed-paste mode and fires onData, which the
        // handler above forwards to the websocket.
        if (this.term && this.term.paste) this.term.paste(text);
        else if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(text);
      })
      .catch(() => {});
  };

  // Prefer Electron's native clipboard (navigator.clipboard.readText is blocked
  // in the desktop renderer); fall back to the async Clipboard API on the web.
  _clipRead() {
    if (window.mfclip && window.mfclip.readText) return window.mfclip.readText();
    if (navigator.clipboard && navigator.clipboard.readText) return navigator.clipboard.readText();
    return "";
  }

  _clipWrite(text) {
    try {
      if (window.mfclip && window.mfclip.writeText) return window.mfclip.writeText(text);
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text);
    } catch (e) {}
  }

  _doFit() {
    try {
      if (this.fit) this.fit.fit();
    } catch (e) {}
    this._sendResize();
  }

  _sendResize() {
    if (!this.term || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    // Mirror of app.js doFit dedupe: only tell tmux when the size actually
    // changed on this connection (window-size latest — see mkTerm.doFit).
    const size = this.term.cols + "x" + this.term.rows;
    if (this.ws._sentSize === size) return;
    try {
      this.ws.send(
        JSON.stringify({ type: "resize", cols: this.term.cols, rows: this.term.rows })
      );
      this.ws._sentSize = size;
    } catch (e) {}
  }

  _connect() {
    if (this._stop) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + this.wsPath);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => this._sendResize();
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const j = JSON.parse(ev.data);
          if (j && j.type === "error") this.term.write("\r\n[" + (j.message || "error") + "]\r\n");
        } catch (e) {}
        return;
      }
      this.term.write(new Uint8Array(ev.data));
    };
    ws.onclose = (ev) => {
      // 4404 instance gone / 4409 workspace gone => don't reconnect.
      if (ev.code === 4404 || ev.code === 4409) {
        this._stop = true;
        if (this.onGone) this.onGone(ev.code);
        return;
      }
      if (this._stop) return;
      this._reconnectTimer = setTimeout(() => this._connect(), 2500);
    };
  }

  dispose() {
    this._stop = true;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    window.removeEventListener("resize", this._onResize);
    try {
      this.host.removeEventListener("contextmenu", this._onContextMenu);
      this.host.removeEventListener("mousedown", this._onDragStart);
      const doc = this.host.ownerDocument;
      doc.removeEventListener("mousemove", this._onDragMove);
      doc.removeEventListener("mouseup", this._onDragEnd);
    } catch (e) {}
    try {
      if (this.ws) this.ws.close();
    } catch (e) {}
    try {
      if (this.term) this.term.dispose();
    } catch (e) {}
  }
}
