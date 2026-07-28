// Session Templates addon frontend: reusable session recipes.
//
// The generic addon-module contract (docs/extensions.md): core/slots.js imports
// this file and calls window.mindflockAddons.templates.init(ctx). It appends a
// "Templates" button to this addon's sidebar bar; clicking it opens a
// self-built modal to save, run, and delete templates. A template bundles the
// New-session inputs (program, repo, provisioning, seed prompt); "Run" posts
// them to the existing POST /api/instances, so session creation keeps one path.
// No edits to app.js/index.html/style.css — styling reuses the app's CSS vars
// and the shared `.modal` overlay.

import { activateModalA11y } from "/core/addon-modal.js";

const API = "/api/templates";

// Shown only when the user has no templates yet — one tap prefills the form
// (they save their own copy), so the feature teaches itself instead of being an
// empty box. Generic + repo-less, so they run against the managed repo as-is.
const STARTERS = [
  { name: "fix-failing-tests", prompt: "Find and fix the failing tests. Run the suite and iterate until it is green." },
  { name: "address-pr-review", prompt: "Address the open review comments on this PR, then push the changes." },
  { name: "write-tests", prompt: "Write tests covering the most recent changes and make them pass." },
];

function injectStyles() {
  if (document.getElementById("mft-styles")) return;
  const css = `
  .mft-panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; width: min(560px, 92vw); max-height: 86vh; overflow-y: auto;
    color: var(--text); box-shadow: 0 12px 40px rgba(0,0,0,0.35);
  }
  .mft-intro { margin: 2px 0 14px; font-size: 12.5px; }
  .mft-list { display: flex; flex-direction: column; gap: 10px; }
  .mft-empty { font-size: 12.5px; color: var(--muted); padding: 8px 0 4px; }
  .mft-filter { display: none; margin: 0 0 10px; }
  .mft-filter-input { width: 100%; box-sizing: border-box; background: var(--panel);
    color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 6px 9px; font: inherit; }
  .mft-starters { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 0 4px; }
  .mft-card {
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px;
  }
  .mft-card-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .mft-name { font-weight: 600; font-size: 14px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mft-meta { font-size: 12px; color: var(--muted); margin-top: 3px; word-break: break-word; }
  .mft-badge {
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--green); border: 1px solid currentColor; border-radius: 4px;
    padding: 0 5px; margin-left: 6px;
  }
  .mft-actions { display: flex; gap: 6px; flex: none; }
  .mft-btn {
    border-radius: 7px; padding: 6px 12px; font-size: 12px; cursor: pointer;
    border: 1px solid var(--border); background: var(--panel); color: var(--text); white-space: nowrap;
  }
  .mft-btn:hover { border-color: var(--accent); }
  .mft-btn.mft-run { background: color-mix(in srgb, var(--accent) 18%, var(--panel)); border-color: var(--accent); }
  .mft-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .mft-run-row { display: flex; gap: 6px; margin-top: 8px; }
  .mft-run-row input { flex: 1 1 auto; }
  .mft-runerr { color: var(--red); font-size: 12px; margin-top: 5px; min-height: 0; }
  .mft-send-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 6px; }
  .mft-send-label { font-size: 12px; color: var(--muted); }
  .mft-send-sel { flex: 1 1 120px; min-width: 120px; background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 7px; padding: 5px 7px; font: inherit; }
  .mft-form { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
  .mft-form summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  .mft-form label { display: block; font-size: 12px; color: var(--muted); margin-top: 8px; }
  .mft-form input[type=text], .mft-form textarea, .mft-form select {
    width: 100%; box-sizing: border-box; margin-top: 3px; background: var(--panel);
    color: var(--text); border: 1px solid var(--border); border-radius: 7px; padding: 6px 8px; font: inherit;
  }
  .mft-form .mft-check { display: flex; align-items: center; gap: 6px; margin-top: 10px; color: var(--text); }
  .mft-form .mft-check input { margin: 0; }
  .mft-save-row { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  .mft-io { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }
  .mft-io summary { cursor: pointer; font-size: 13px; font-weight: 600; }
  .mft-io-box { width: 100%; box-sizing: border-box; margin-top: 8px; min-height: 84px;
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 7px; padding: 6px 8px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px; }
  .mft-io-msg { color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 0; }
  .mft-manage-badge {
    display: inline-block; min-width: 15px; text-align: center; margin-left: 6px;
    font-size: 10px; font-weight: 700; line-height: 15px; border-radius: 999px;
    color: #fff; background: var(--accent);
  }
  /* Narrow viewports (small window / tablet grid): stack the action buttons
     below the name so Run/Edit/Duplicate/Delete don't crush the title to an
     unreadable "respon…" ellipsis. */
  @media (max-width: 600px) {
    .mft-card-top { flex-direction: column; align-items: stretch; gap: 8px; }
    .mft-name { white-space: normal; }
    .mft-actions { flex-wrap: wrap; }
  }
  `;
  const style = document.createElement("style");
  style.id = "mft-styles";
  style.textContent = css;
  document.head.appendChild(style);
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

function metaLine(t) {
  const bits = [];
  bits.push(t.program || "default program");
  if (t.repo_path) bits.push("repo: " + t.repo_path.split("/").filter(Boolean).pop());
  if (t.in_place) bits.push("in-place");
  if (t.prompt) {
    const p = t.prompt.replace(/\s+/g, " ").trim();
    bits.push('“' + (p.length > 48 ? p.slice(0, 48) + "…" : p) + '”');
  }
  return bits.join(" · ");
}

// A session title not already taken by a live session, so repeat-runs of the
// same template don't collide (append -2, -3, …). Falls back to base on error.
async function uniqueTitle(base) {
  try {
    const data = await getJSON("/api/instances");
    const taken = new Set(
      (Array.isArray(data) ? data : []).map((s) => String(s.title || "").toLowerCase())
    );
    if (!taken.has(base.toLowerCase())) return base;
    let i = 2;
    let name;
    do {
      name = base + "-" + i;
      i++;
    } while (taken.has(name.toLowerCase()));
    return name;
  } catch (e) {
    return base;
  }
}

window.mindflockAddons = window.mindflockAddons || {};
window.mindflockAddons.templates = {
  init(ctx) {
    injectStyles();
    const toast = ctx && typeof ctx.toast === "function" ? ctx.toast : (m) => console.log(m);

    // No sidebar bar anymore — templates are surfaced from the + New dialog,
    // which calls the exposed window.mindflockAddons.templates.open(). We keep a
    // captured opener so focus returns correctly when the modal closes.
    let opener = null;

    let modal = null;
    let listEl = null;
    let errEl = null;
    let formApi = null; // set by buildForm: { fill(template) } to edit an existing one
    let releaseA11y = null;
    let currentNames = []; // lowercased names of loaded templates (for unique-copy)
    let currentItems = []; // last-loaded templates (for client-side filtering)
    let filterWrap = null;
    let filterInput = null;
    const FILTER_MIN = 6; // show the filter box only past this many (like the app's session filter)

    // Broadcast the template count so the + New dialog can label its Templates
    // entry (e.g. "Templates (3)"). No sidebar button exists to paint anymore.
    function paintBadge(n) {
      try {
        document.dispatchEvent(
          new CustomEvent("mf-templates-count", { detail: { count: n || 0 } })
        );
      } catch (e) {
        /* CustomEvent unsupported — the dialog still lists templates */
      }
    }

    async function runTemplate(t, titleInput, runErr) {
      const title = (titleInput.value || "").trim();
      runErr.textContent = "";
      if (!title) {
        runErr.textContent = "Enter a name for the new session.";
        return;
      }
      try {
        const r = await fetch("/api/instances", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title,
            program: t.program || "",
            repo_path: t.repo_path || "",
            prompt: t.prompt || "",
            provisioned: !!t.provisioned,
            workspace_strategy: t.workspace_strategy || "worktree",
            in_place: !!t.in_place,
            init_repo: !!t.init_repo,
          }),
        });
        if (r.status === 202) {
          hide();
          toast('Started “' + title + '” from template “' + t.name + '”');
          return;
        }
        let msg = "could not start session (HTTP " + r.status + ")";
        try {
          const d = await r.json();
          if (d && d.error) msg = d.error;
        } catch (e) {
          /* keep default */
        }
        runErr.textContent = msg;
      } catch (e) {
        runErr.textContent = "could not reach the server";
      }
    }

    function renderCard(t) {
      const card = document.createElement("div");
      card.className = "mft-card";

      const top = document.createElement("div");
      top.className = "mft-card-top";
      const nameWrap = document.createElement("div");
      nameWrap.style.minWidth = "0";
      const name = document.createElement("div");
      name.className = "mft-name";
      name.textContent = t.name;
      if (t.provisioned) {
        const b = document.createElement("span");
        b.className = "mft-badge";
        b.textContent = "provision";
        name.appendChild(b);
      }
      const meta = document.createElement("div");
      meta.className = "mft-meta";
      meta.textContent = metaLine(t);
      nameWrap.append(name, meta);

      const actions = document.createElement("div");
      actions.className = "mft-actions";
      const runBtn = document.createElement("button");
      runBtn.className = "mft-btn mft-run";
      runBtn.textContent = "Run";
      const editBtn = document.createElement("button");
      editBtn.className = "mft-btn";
      editBtn.textContent = "Edit";
      editBtn.title = "Load this template into the form to change it";
      const dupBtn = document.createElement("button");
      dupBtn.className = "mft-btn";
      dupBtn.textContent = "Duplicate";
      dupBtn.title = "Fork this template into a new one you can tweak";
      const delBtn = document.createElement("button");
      delBtn.className = "mft-btn";
      delBtn.textContent = "Delete";
      actions.append(runBtn, editBtn, dupBtn, delBtn);
      top.append(nameWrap, actions);

      // Inline "name this session" row, revealed by Run.
      const runRow = document.createElement("div");
      runRow.className = "mft-run-row";
      runRow.style.display = "none";
      const titleInput = document.createElement("input");
      titleInput.type = "text";
      titleInput.placeholder = "New session name…";
      titleInput.value = t.name;
      const startBtn = document.createElement("button");
      startBtn.className = "mft-btn mft-run";
      startBtn.textContent = "Start";
      runRow.append(titleInput, startBtn);
      const runErr = document.createElement("div");
      runErr.className = "mft-runerr";
      runErr.setAttribute("role", "status");
      runErr.setAttribute("aria-live", "polite");

      // Optional: send this recipe's prompt to an already-running session
      // (reuses the /send endpoint). Only meaningful when the template carries
      // a prompt; revealed alongside the run row.
      const sendRow = document.createElement("div");
      sendRow.className = "mft-send-row";
      sendRow.style.display = "none";
      const sendLabel = document.createElement("span");
      sendLabel.className = "mft-send-label";
      sendLabel.textContent = "or send prompt to:";
      const sendSel = document.createElement("select");
      sendSel.className = "mft-send-sel";
      const sendBtn = document.createElement("button");
      sendBtn.className = "mft-btn";
      sendBtn.textContent = "Send";
      sendRow.append(sendLabel, sendSel, sendBtn);
      let sendPopulated = false;
      let sendTargets = []; // titles of running sessions (for broadcast)
      async function populateSend() {
        if (sendPopulated) return;
        sendPopulated = true;
        try {
          const data = await getJSON("/api/instances");
          const running = (Array.isArray(data) ? data : []).filter(
            (s) => s && s.title && (s.status === "running" || s.started)
          );
          sendSel.innerHTML = "";
          sendTargets = running.map((s) => s.title);
          if (!running.length) {
            const o = document.createElement("option");
            o.value = "";
            o.textContent = "no running sessions";
            sendSel.appendChild(o);
            sendSel.disabled = sendBtn.disabled = true;
          } else {
            // Broadcast option first, only when it's meaningful (≥2 sessions).
            if (running.length >= 2) {
              const all = document.createElement("option");
              all.value = "*";
              all.textContent = "All running sessions (" + running.length + ")";
              sendSel.appendChild(all);
            }
            for (const s of running) {
              const o = document.createElement("option");
              o.value = s.title;
              o.textContent = s.alias || s.title;
              sendSel.appendChild(o);
            }
            sendSel.disabled = sendBtn.disabled = false;
          }
        } catch (e) {
          sendSel.innerHTML = "";
          sendTargets = [];
          const o = document.createElement("option");
          o.textContent = "couldn't load sessions";
          sendSel.appendChild(o);
          sendSel.disabled = sendBtn.disabled = true;
        }
      }
      async function sendPromptTo(title) {
        const r = await fetch("/api/instances/" + encodeURIComponent(title) + "/send", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: t.prompt }),
        });
        return r.ok;
      }
      sendBtn.addEventListener("click", async () => {
        const target = sendSel.value;
        runErr.textContent = "";
        if (!target) {
          runErr.textContent = "No running session to send to.";
          return;
        }
        sendBtn.disabled = true;
        try {
          if (target === "*") {
            let ok = 0;
            for (const title of sendTargets) {
              if (await sendPromptTo(title)) ok++;
            }
            hide();
            toast('Sent "' + t.name + '" prompt to ' + ok + " session" + (ok === 1 ? "" : "s"));
          } else if (await sendPromptTo(target)) {
            hide();
            toast('Sent "' + t.name + '" prompt to ' + target);
          } else {
            runErr.textContent = "could not send (the session may be busy)";
          }
        } catch (e) {
          runErr.textContent = "could not reach the server";
        } finally {
          sendBtn.disabled = false;
        }
      });

      runBtn.addEventListener("click", () => {
        const showing = runRow.style.display !== "none";
        const disp = showing ? "none" : "flex";
        runRow.style.display = disp;
        // Reveal the send-to-session control only if this recipe has a prompt.
        if (t.prompt) {
          sendRow.style.display = disp;
          if (!showing) populateSend();
        }
        if (!showing) {
          titleInput.focus();
          titleInput.select();
          // Suggest a non-colliding session name (only if the user hasn't typed).
          uniqueTitle(t.name).then((n) => {
            if (titleInput.value === t.name && n !== t.name) {
              titleInput.value = n;
              titleInput.select();
            }
          });
        }
      });
      startBtn.addEventListener("click", () => runTemplate(t, titleInput, runErr));
      titleInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          runTemplate(t, titleInput, runErr);
        }
      });
      editBtn.addEventListener("click", () => {
        if (formApi) formApi.fill(t);
      });
      dupBtn.addEventListener("click", () => {
        // Fork: prefill the form with a UNIQUE name so saving creates a new
        // template and repeated duplicates don't overwrite each other.
        if (!formApi) return;
        const base = t.name + "-copy";
        let name = base;
        let i = 2;
        while (currentNames.includes(name.toLowerCase())) {
          name = base + "-" + i;
          i++;
        }
        formApi.fill(Object.assign({}, t, { name }));
      });
      delBtn.addEventListener("click", async () => {
        delBtn.disabled = true;
        try {
          await fetch(API + "/" + encodeURIComponent(t.name), { method: "DELETE" });
          await load();
        } catch (e) {
          runErr.textContent = "could not delete";
          delBtn.disabled = false;
        }
      });

      card.append(top, runRow, sendRow, runErr);
      return card;
    }

    function buildForm() {
      const form = document.createElement("details");
      form.className = "mft-form";
      const summary = document.createElement("summary");
      summary.textContent = "+ New template";
      form.appendChild(summary);

      const mk = (labelText, el) => {
        const label = document.createElement("label");
        label.textContent = labelText;
        label.appendChild(el);
        return label;
      };
      const nameIn = document.createElement("input");
      nameIn.type = "text";
      nameIn.placeholder = "e.g. fix-failing-tests";
      const progIn = document.createElement("input");
      progIn.type = "text";
      progIn.placeholder = "program (blank = default)";
      const repoIn = document.createElement("input");
      repoIn.type = "text";
      repoIn.placeholder = "repo folder (blank = managed repo)";
      const promptIn = document.createElement("textarea");
      promptIn.rows = 2;
      promptIn.placeholder = "seed prompt sent to the agent (optional)";

      const provWrap = document.createElement("label");
      provWrap.className = "mft-check";
      const provIn = document.createElement("input");
      provIn.type = "checkbox";
      provWrap.append(provIn, document.createTextNode("Provision workspace (repo setup + warm caches)"));

      const stratIn = document.createElement("select");
      for (const [v, t] of [["worktree", "shared base clone (worktree) — fast"], ["clone", "full clone — standalone"]]) {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = t;
        stratIn.appendChild(o);
      }

      const err = document.createElement("div");
      err.className = "mft-runerr";
      err.setAttribute("role", "status");
      err.setAttribute("aria-live", "polite");
      const saveRow = document.createElement("div");
      saveRow.className = "mft-save-row";
      const saveBtn = document.createElement("button");
      saveBtn.className = "mft-btn mft-run";
      saveBtn.textContent = "Save template";
      saveRow.appendChild(saveBtn);

      saveBtn.addEventListener("click", async () => {
        err.textContent = "";
        const name = (nameIn.value || "").trim();
        if (!name) {
          err.textContent = "Give the template a name.";
          return;
        }
        try {
          const r = await fetch(API, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              name,
              program: progIn.value.trim(),
              repo_path: repoIn.value.trim(),
              prompt: promptIn.value,
              provisioned: provIn.checked,
              workspace_strategy: stratIn.value,
            }),
          });
          if (!r.ok) {
            let m = "could not save (HTTP " + r.status + ")";
            try {
              const d = await r.json();
              if (d && d.error) m = d.error;
            } catch (e) {}
            err.textContent = m;
            return;
          }
          nameIn.value = progIn.value = repoIn.value = promptIn.value = "";
          provIn.checked = false;
          form.open = false;
          summary.textContent = "+ New template";
          await load();
        } catch (e) {
          err.textContent = "could not reach the server";
        }
      });

      form.append(
        mk("Template name", nameIn),
        mk("Program", progIn),
        mk("Repo folder", repoIn),
        mk("Seed prompt", promptIn),
        provWrap,
        mk("Workspace strategy", stratIn),
        err,
        saveRow
      );

      // Editing = load an existing template back into this form. Saving upserts
      // by name (server-side), so filling the same name updates it in place.
      formApi = {
        fill(t) {
          nameIn.value = t.name || "";
          progIn.value = t.program || "";
          repoIn.value = t.repo_path || "";
          promptIn.value = t.prompt || "";
          provIn.checked = !!t.provisioned;
          stratIn.value = t.workspace_strategy === "clone" ? "clone" : "worktree";
          err.textContent = "";
          summary.textContent = "Edit “" + (t.name || "") + "”";
          form.open = true;
          form.scrollIntoView({ block: "nearest" });
          nameIn.focus();
        },
      };
      return form;
    }

    // Import / export: share a template set across machines or teammates. Calm
    // by default (collapsed); paste JSON to import, or export the current set.
    function buildIO() {
      const d = document.createElement("details");
      d.className = "mft-io";
      const s = document.createElement("summary");
      s.textContent = "Import / export";
      const box = document.createElement("textarea");
      box.className = "mft-io-box";
      box.placeholder = "Paste templates JSON here to import, or press Export to fill this box.";
      const msg = document.createElement("div");
      msg.className = "mft-io-msg";
      msg.setAttribute("role", "status");
      msg.setAttribute("aria-live", "polite");
      const row = document.createElement("div");
      row.className = "mft-save-row";
      const mkbtn = (label) => {
        const b = document.createElement("button");
        b.className = "mft-btn";
        b.textContent = label;
        return b;
      };
      const expBtn = mkbtn("Export");
      const dlBtn = mkbtn("Download");
      const impBtn = mkbtn("Import");
      impBtn.classList.add("mft-run");
      row.append(expBtn, dlBtn, impBtn);

      async function currentJson() {
        const data = await getJSON(API);
        return JSON.stringify(data.templates || [], null, 2);
      }
      expBtn.addEventListener("click", async () => {
        try {
          box.value = await currentJson();
          msg.textContent = "Exported the current set into the box — copy it anywhere.";
        } catch (e) {
          msg.textContent = "could not read templates";
        }
      });
      dlBtn.addEventListener("click", async () => {
        try {
          const json = await currentJson();
          const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
          const a = document.createElement("a");
          a.href = url;
          a.download = "mindflock-templates.json";
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e) {
          msg.textContent = "could not export";
        }
      });
      impBtn.addEventListener("click", async () => {
        let data;
        try {
          data = JSON.parse(box.value);
        } catch (e) {
          msg.textContent = "That isn't valid JSON.";
          return;
        }
        const list = Array.isArray(data) ? data : Array.isArray(data && data.templates) ? data.templates : null;
        if (!list) {
          msg.textContent = "Expected a JSON array of templates (or { templates: [...] }).";
          return;
        }
        let ok = 0;
        let skipped = 0;
        for (const t of list) {
          if (!t || !t.name) {
            skipped++;
            continue;
          }
          try {
            const r = await fetch(API, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(t),
            });
            r.ok ? ok++ : skipped++;
          } catch (e) {
            skipped++;
          }
        }
        msg.textContent = "Imported " + ok + (skipped ? ", " + skipped + " skipped" : "") + ".";
        await load();
      });

      d.append(s, box, row, msg);
      return d;
    }

    function buildModal() {
      modal = document.createElement("div");
      modal.id = "mft-modal";
      modal.className = "modal hidden";
      const panel = document.createElement("div");
      panel.className = "mft-panel";

      const head = document.createElement("div");
      head.className = "ws-head";
      const h2 = document.createElement("h2");
      h2.textContent = "Session templates";
      const refresh = document.createElement("button");
      refresh.textContent = "Refresh";
      refresh.addEventListener("click", load);
      const close = document.createElement("button");
      close.textContent = "Close";
      close.addEventListener("click", hide);
      head.append(h2, refresh, close);

      const intro = document.createElement("p");
      intro.className = "mft-intro muted";
      intro.textContent = "Save a session setup once, launch it in one click. Templates are per-user and never touch running sessions.";

      filterWrap = document.createElement("div");
      filterWrap.className = "mft-filter";
      filterInput = document.createElement("input");
      filterInput.type = "text";
      filterInput.className = "mft-filter-input";
      filterInput.placeholder = "Filter templates…";
      filterInput.setAttribute("aria-label", "Filter templates");
      filterInput.addEventListener("input", renderList);
      filterWrap.appendChild(filterInput);

      listEl = document.createElement("div");
      listEl.className = "mft-list";
      errEl = document.createElement("p");
      errEl.className = "error";

      panel.append(head, intro, filterWrap, listEl, errEl, buildForm(), buildIO());
      modal.appendChild(panel);
      modal.addEventListener("click", (e) => {
        if (e.target === modal) hide();
      });
      document.body.appendChild(modal);
    }

    function onKey(e) {
      if (e.key === "Escape") hide();
    }
    function show() {
      opener = document.activeElement;
      if (!modal) buildModal();
      modal.classList.remove("hidden");
      document.addEventListener("keydown", onKey);
      // Placeholder while the first fetch runs, so a cold open isn't a blank box.
      if (listEl && !listEl.querySelector(".mft-card")) {
        listEl.innerHTML = '<div class="mft-empty">Loading templates…</div>';
      }
      load();
      releaseA11y = activateModalA11y(modal, opener, "Session templates");
    }
    function hide() {
      if (modal) modal.classList.add("hidden");
      document.removeEventListener("keydown", onKey);
      if (releaseA11y) {
        releaseA11y();
        releaseA11y = null;
      }
    }

    // Paint the list from currentItems, applying the filter term. Starters show
    // only when there are genuinely no templates (not merely filtered to none).
    function renderList() {
      if (!listEl) return;
      listEl.innerHTML = "";
      if (!currentItems.length) {
        const empty = document.createElement("div");
        empty.className = "mft-empty";
        empty.textContent = "No templates yet. Start from one of these, or create your own below:";
        listEl.appendChild(empty);
        const row = document.createElement("div");
        row.className = "mft-starters";
        for (const s of STARTERS) {
          const b = document.createElement("button");
          b.className = "mft-btn";
          b.textContent = s.name;
          b.title = s.prompt;
          b.addEventListener("click", () => {
            if (formApi) formApi.fill({ name: s.name, prompt: s.prompt, workspace_strategy: "worktree" });
          });
          row.appendChild(b);
        }
        listEl.appendChild(row);
        return;
      }
      const term = ((filterInput && filterInput.value) || "").trim().toLowerCase();
      const shown = term
        ? currentItems.filter((t) =>
            (String(t.name || "") + " " + String(t.prompt || "") + " " + String(t.program || ""))
              .toLowerCase()
              .includes(term)
          )
        : currentItems;
      if (!shown.length) {
        const none = document.createElement("div");
        none.className = "mft-empty";
        none.textContent = "No templates match “" + term + "”.";
        listEl.appendChild(none);
        return;
      }
      for (const t of shown) listEl.appendChild(renderCard(t));
    }

    async function load() {
      if (errEl) errEl.textContent = "";
      try {
        const data = await getJSON(API);
        currentItems = data.templates || [];
        currentNames = currentItems.map((t) => String(t.name || "").toLowerCase());
        // Show the filter only once there are enough templates to warrant it
        // (or a filter is already active), mirroring the sidebar session filter.
        if (filterWrap) {
          const active = !!(filterInput && filterInput.value);
          filterWrap.style.display = currentItems.length >= FILTER_MIN || active ? "block" : "none";
        }
        renderList();
        paintBadge(currentItems.length);
      } catch (e) {
        if (errEl) errEl.textContent = "Couldn't load templates — is the server up?";
      }
    }

    // Expose an opener so the + New dialog (and anything else) can launch this
    // template manager without duplicating its DOM.
    window.mindflockAddons.templates.open = show;

    // Prime the count once without opening the modal, so the + New dialog can
    // show how many templates exist before it's first opened.
    (async function primeBadge() {
      try {
        const data = await getJSON(API);
        paintBadge((data.templates || []).length);
      } catch (e) {
        /* no signal; the dialog still lists templates when opened */
      }
    })();
  },
};
