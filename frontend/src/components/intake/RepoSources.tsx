/** A watched GitHub repository, edited where it shows and added once.
 *
 * Intake → GitHub PRs, Intake → GitHub issues and Verify each watch a list of
 * repositories. There used to be a card list per surface, under the work, each
 * with its own + Add, Test access and Remove — and the same repository was
 * typed into each of them separately ("Separate from the other GitHub tab's
 * list", the card's own hint admitted). Now:
 *
 * - **Edit where it shows.** A watched repository's heading in a work list
 *   carries Edit; the editor (`RepoEditor`) opens attached under it. It shows
 *   that surface's fields, plus "Use it for" ticks that put the same repository
 *   on the other two lists in one save (`repoUses.ts` owns the rules: the first
 *   PR repository of a never-chosen setup is saved switched OFF, an Intake
 *   use's block goes with it, Verify's block — text the user wrote — stays).
 * - **Add once.** `AddRepoFlow` is the repository half of "+ Add source": the
 *   slug once, Test access, the uses as ticks, then each ticked use's fields.
 *
 * Storage is unchanged: `github.repos` + `github.repo_settings`,
 * `github.issue_repos` + `github.issue_repo_settings`, and
 * `repository.verify_repos` + `repository.verify_repo_settings`, maps keyed by
 * `owner/name` whose block may carry `agent`, `depth`, `base_branch` (PR),
 * `min_age_minutes`, `skip_authors`, and on Verify `live_branch`,
 * `deploy_delay_minutes`, `target`, `prompt`. An absent key or blank value
 * inherits the tab-wide default, which every placeholder names.
 */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Json } from "../../api/types";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import {
  refreshPanel,
  useAgentChoices,
  useCachedLiveBranch,
  useTicketingSources,
} from "../../state/queries";
import { useSettings } from "../settings/useSettings";
import { useAutosave } from "./autosave";
import { LANE_LABEL, SOURCE_DEPTHS } from "../../lib/autopilot";
import {
  AddFlowShell,
  ArmButton,
  GithubStatusLine,
  REPO_KIND,
  TestButton,
  type AddKind,
} from "./kit";
import {
  REPO_USES,
  USE_LABEL,
  USE_SENTENCE,
  blocksFor,
  fieldPatch,
  githubSlugOf,
  knownRepos,
  usesOf,
  usesPatch,
  type RepoUse,
} from "./repoUses";
import { sourceRepos } from "./repoList";

export const REPO_RE = /^[^\s/]+\/[^\s/]+$/;

/** Ceiling on a verify card's standing instructions, mirroring
 * `VERIFY_PROMPT_MAX` in `backend/config/settings.py`.
 *
 * Duplicated rather than fetched because it is enforced on the way IN, at the
 * one place a person can exceed it — the server truncates silently (it has to:
 * the text shares a prompt with the branch diff, and the diff is the half that
 * describes the change under test), and a textarea that keeps showing 2,600
 * characters of a paragraph only 2,000 of which were saved is a lie the user
 * cannot see. `maxLength` makes the box refuse the 2,001st character, paste
 * included, so what is on screen is always what was stored. If the two numbers
 * ever drift, the browser's is the smaller kind of wrong: text that was never
 * typed cannot be lost. */
export const VERIFY_PROMPT_MAX = 2000;

/** Ceiling on a verify card's target, mirroring `VERIFY_TARGET_MAX` in
 * `backend/config/settings.py`. Same reasoning as the cap above, one size down:
 * this is a URL and the sentence somebody needs to reach it, not a runbook. */
export const VERIFY_TARGET_MAX = 500;

/** A repository's own values on one surface. Absent/blank = inherit. */
export interface RepoOverride {
  agent?: string;
  /** How far autopilot carries an item ingested from this repo ("" = inherit).
   * Never "merge" — a per-repo default applies to every future item with nobody
   * watching, and a merge cannot be undone. */
  depth?: string;
  base_branch?: string;
  min_age_minutes?: number | string;
  skip_authors?: string[] | string;
  /** Verify only: which branch counts as SHIPPED for this repo, so a plan
   * written here goes due when its commit lands on it. Per repo because "what
   * counts as live" is a per-repo fact — `main` here, `staging` next door — and
   * a plan measured against the wrong branch either goes due when nothing
   * shipped or never goes due at all. "" = inherit the flock-wide chain. */
  live_branch?: string;
  deploy_delay_minutes?: string;
  /** Verify only: where this repo's running product is. Blank means "there is
   * no deployment to point at" — a real answer for a library or a CLI. */
  target?: string;
  /** Verify only: standing instructions folded into the prompt that writes a
   * plan for this repo. Truncated server-side; see `VERIFY_PROMPT_MAX`. */
  prompt?: string;
}

export type RepoOverrides = Record<string, RepoOverride>;

/** What a blank field on one surface inherits — each placeholder names it, so
 * an empty box reads "inherits 15", never "no grace period at all". */
export interface RepoDefaults {
  agent: string;
  baseBranch: string;
  minAge: string;
  skipAuthors: string;
  /** Verify: the flock-wide live branch a card that sets none goes due against. */
  liveBranch: string;
  /** Verify: the flock-wide deploy wait, in minutes. */
  deployDelay: string;
}

const listText = (v: unknown) => (Array.isArray(v) ? v.join(", ") : v == null ? "" : String(v));

/** The tab-wide defaults one surface's blank fields inherit. */
export function repoDefaults(
  settings: Json,
  use: RepoUse,
  agentFallback: string,
  liveBranch: string
): RepoDefaults {
  const gh = (settings.github || {}) as Json;
  const rp = (settings.repository || {}) as Json;
  const blank = { agent: "", baseBranch: "", minAge: "", skipAuthors: "", liveBranch: "", deployDelay: "" };
  if (use === "pr")
    return {
      ...blank,
      agent: String(gh.agent || agentFallback || ""),
      baseBranch: String(gh.base_branch || ""),
      minAge: gh.min_age_minutes == null ? "" : String(gh.min_age_minutes),
      skipAuthors: listText(gh.skip_authors),
    };
  if (use === "issue")
    return {
      ...blank,
      agent: String(gh.issue_agent || agentFallback || ""),
      minAge: gh.issue_min_age_minutes == null ? "" : String(gh.issue_min_age_minutes),
      skipAuthors: listText(gh.issue_skip_authors),
    };
  return {
    ...blank,
    liveBranch: liveBranch || String(rp.live_branch || ""),
    // Absent => the backend default, which is 5.
    deployDelay: String(rp.deploy_delay_minutes == null ? 5 : rp.deploy_delay_minutes),
  };
}

/** `repoDefaults` for the surface, with the agent fallback and the cached live
 * branch filled in — what every editor and the add flow need. */
function useDefaults(use: RepoUse, liveBranch?: string): RepoDefaults {
  const s = useSettings();
  const agents = useAgentChoices().data;
  const cached = useCachedLiveBranch();
  return repoDefaults(s.settings, use, agents?.fallback || "", liveBranch ?? cached);
}

/** One text field of a repository's block: uncontrolled, sent as you type
 * (autosave.ts — on a pause, blur, Enter or the dialog closing; blur alone
 * lost what was typed to Escape or Close). An unchanged value isn't sent.
 * Keyed by repo by its caller, so a different repository remounts it. */
function BlockInput({
  label,
  field,
  value,
  placeholder,
  hint,
  type = "text",
  maxLength,
  narrow,
  onSave,
}: {
  label: string;
  field: string;
  value: string;
  placeholder: string;
  hint: React.ReactNode;
  type?: "text" | "number";
  maxLength?: number;
  narrow?: boolean;
  onSave(field: string, value: string): void;
}) {
  const autosave = useAutosave<string>((v) => {
    if (v.trim() !== value) onSave(field, v);
  });
  return (
    <label className="set-row">
      <span className="set-label">{label}</span>
      <input
        type={type}
        className={narrow ? "num-sm" : undefined}
        min={type === "number" ? 0 : undefined}
        step={type === "number" ? 1 : undefined}
        autoComplete="off"
        spellCheck={false}
        maxLength={maxLength}
        data-repo-field={field}
        placeholder={placeholder}
        defaultValue={value}
        onChange={(e) => autosave.schedule(e.target.value)}
        onBlur={() => autosave.flush()}
        onKeyDown={(e) => {
          if (e.key === "Enter") autosave.flush();
        }}
      />
      <span className="set-hint">{hint}</span>
    </label>
  );
}

/** The fields one surface keeps for one repository.
 *
 * Verify's are a different set entirely — the branch that ships, the wait
 * after merge, where it runs, notes for the checklist writer — because nothing
 * an Intake surface has applies to it: a verify run has no author to skip, no
 * PR to age out, no depth to carry an item to, and it runs the agent the
 * SESSION already uses. Fields that cannot do anything are absent, not
 * disabled. */
export function RepoFields({
  use,
  repo,
  liveBranch,
}: {
  use: RepoUse;
  repo: string;
  /** Verify passes the server's resolved live branch; Intake reads the cache. */
  liveBranch?: string;
}) {
  const s = useSettings();
  const agents = useAgentChoices().data || { names: [], fallback: "" };
  const defaults = useDefaults(use, liveBranch);
  // The editor's own copy of this surface's blocks, reconciled with the store
  // by CONTENT: our own save's echo must not rebuild the fields mid-edit, and
  // two edits in quick succession must compose (the map is written wholesale).
  const saved = blocksFor(s.settings, use);
  const savedKey = JSON.stringify(saved);
  const [blocks, setBlocksState] = useState<Record<string, Json>>(saved);
  // Read by `save`, so two fields flushed together (the dialog closing with
  // both typed in) compose instead of the second writing over the first.
  const blocksRef = useRef(blocks);
  const setBlocks = (next: Record<string, Json>) => {
    blocksRef.current = next;
    setBlocksState(next);
  };
  useEffect(() => {
    if (JSON.stringify(blocksRef.current) !== savedKey) setBlocks(saved);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);
  const o = ((blocks[repo] || {}) as RepoOverride) || {};
  const save = (field: string, value: string) => {
    const before = blocksRef.current;
    const { patch, blocks: next } = fieldPatch(before, use, repo, field, value);
    setBlocks(next);
    void s.saveGroups(patch, "Saved " + repo).then((ok) => {
      // "Save failed" is toasted; put the editor back on what is stored.
      if (!ok && blocksRef.current === next) setBlocks(before);
    });
  };
  const promptSave = useAutosave<string>((v) => {
    if (v.trim() !== (o.prompt || "")) save("prompt", v);
  });
  const text = (field: keyof RepoOverride) => listText(o[field]);

  if (use === "verify") {
    const live = o.live_branch || defaults.liveBranch;
    return (
      <div className="ik-fields" data-use="verify" key={repo}>
        <div className="ik-grid ik-grid-2">
          <BlockInput
            label="Live branch"
            field="live_branch"
            value={text("live_branch")}
            // The INHERITED answer, not a format hint: an empty box has to read
            // as "inherits staging".
            placeholder={live || "main"}
            hint={
              <>
                Blank uses <code>{defaults.liveBranch || "main"}</code>.
              </>
            }
            onSave={save}
          />
          <BlockInput
            label="Deploy delay (minutes)"
            field="deploy_delay_minutes"
            type="number"
            value={text("deploy_delay_minutes")}
            placeholder={defaults.deployDelay || "5"}
            hint={
              <>
                Time to deploy; 0 = no deploy. Blank uses <code>{defaults.deployDelay || "5"}</code>.
              </>
            }
            onSave={save}
          />
        </div>
        <BlockInput
          label="Where it runs (optional)"
          field="target"
          value={text("target")}
          maxLength={VERIFY_TARGET_MAX}
          placeholder="e.g. https://app.example.com — log in as qa@example.com"
          hint="URL and test login the checks use. Blank checks a fresh checkout."
          onSave={save}
        />
        <label className="set-row">
          <span className="set-label">Notes for the checklist writer (optional)</span>
          <textarea
            className="vf-repo-prompt"
            rows={3}
            spellCheck={false}
            data-repo-field="prompt"
            // The server's cap, enforced where the text is typed — see
            // `VERIFY_PROMPT_MAX`.
            maxLength={VERIFY_PROMPT_MAX}
            placeholder={
              "e.g. The UI runs on :3000 — check there, not :8080.\n" +
              "Always confirm the migration ran.\n" +
              "Ignore anything under vendor/."
            }
            defaultValue={o.prompt || ""}
            onChange={(e) => promptSave.schedule(e.target.value)}
            onBlur={() => promptSave.flush()}
          />
          <span className="set-hint">Added to every checklist for this repo.</span>
        </label>
      </div>
    );
  }

  const agent = o.agent || "";
  // A stored "agent" rung is Off in the one ladder (see lib/autopilot's normalizeLane).
  const depth = o.depth === "agent" ? "" : o.depth || "";
  return (
    <div className="ik-fields" data-use={use} key={repo}>
      <div className="ik-grid ik-grid-3">
        <label className="set-row">
          <span className="set-label">Agent</span>
          <select data-repo-field="agent" value={agent} onChange={(e) => save("agent", e.target.value)}>
            <option value="">{defaults.agent ? `Inherit (${defaults.agent})` : "Inherit (app default)"}</option>
            {agents.names.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <span className="set-hint">Works this repository's sessions.</span>
        </label>
        <label className="set-row">
          <span className="set-label">Fast-track to</span>
          <select data-repo-field="depth" value={depth} onChange={(e) => save("depth", e.target.value)}>
            {/* One ladder: Off / Commit / Push / Open a PR. Merge is per item only. */}
            <option value="">{LANE_LABEL.leave}</option>
            {SOURCE_DEPTHS.map((d) => (
              <option key={d} value={d}>
                {LANE_LABEL[d]}
              </option>
            ))}
          </select>
          <span className="set-hint">After the agent finishes.</span>
        </label>
        <BlockInput
          label={use === "pr" ? "Min PR age (minutes)" : "Min issue age (minutes)"}
          field="min_age_minutes"
          type="number"
          value={text("min_age_minutes")}
          placeholder={defaults.minAge || "15"}
          hint={"Blank uses " + (defaults.minAge || "15") + "."}
          onSave={save}
        />
      </div>
      <div className={"ik-grid " + (use === "pr" ? "ik-grid-2" : "ik-grid-1")}>
        {use === "pr" && (
          <BlockInput
            label="Base branch"
            field="base_branch"
            value={text("base_branch")}
            placeholder={defaults.baseBranch || "the repo's default branch"}
            hint="Only PRs into this branch auto-start."
            onSave={save}
          />
        )}
        <BlockInput
          label={use === "pr" ? "Ignore comments from" : "Ignore issues from"}
          field="skip_authors"
          value={text("skip_authors")}
          placeholder={defaults.skipAuthors || "e.g. dependabot, renovate"}
          hint="GitHub logins, comma-separated."
          onSave={save}
        />
      </div>
    </div>
  );
}

/** "Use it for: PR comments · New issues · Verify" — one tick per list.
 *
 * `fixed` is the surface the editor belongs to: its own tick is shown on and
 * locked, because taking a repository off the list you are looking at is
 * Remove's job (which asks first). */
function UseTicks({
  repo,
  fixed,
  value,
  onChange,
  disabledUses = [],
}: {
  repo: string;
  fixed?: RepoUse;
  value: RepoUse[];
  onChange(next: RepoUse[]): void;
  /** Already on (the add flow shows these on and locked, "already"). */
  disabledUses?: RepoUse[];
}) {
  return (
    <div className="set-row ik-uses" role="group" aria-label={"Use " + (repo || "this repository") + " for"}>
      <span className="set-label">Use it for</span>
      <div className="ik-uses-row">
        {REPO_USES.map((u) => {
          const locked = u === fixed || disabledUses.includes(u);
          const on = value.includes(u);
          return (
            <label key={u} className={"ik-use" + (locked ? " locked" : "")} data-use={u}>
              <input
                type="checkbox"
                checked={on}
                disabled={locked}
                onChange={(e) =>
                  onChange(e.target.checked ? [...value, u] : value.filter((x) => x !== u))
                }
              />
              <span>{fixed ? USE_LABEL[u] : USE_SENTENCE[u]}</span>
              {disabledUses.includes(u) && u !== fixed ? <span className="ik-use-note">already</span> : null}
            </label>
          );
        })}
      </div>
    </div>
  );
}

const PANEL_OF: Partial<Record<RepoUse, "github-prs" | "github-issues">> = {
  pr: "github-prs",
  issue: "github-issues",
};

/** After a repository joins or leaves a list, re-sweep the panels that list
 * feeds, so the headings fill (or empty) now rather than when the cache ages. */
function resweep(uses: RepoUse[]) {
  for (const u of uses) {
    const key = PANEL_OF[u];
    if (key) void refreshPanel(key);
  }
}

/** The usage line a toast and a heading share: "PR comments and Verify". */
function usesText(uses: RepoUse[]): string {
  const names = REPO_USES.filter((u) => uses.includes(u)).map((u) => USE_LABEL[u]);
  return names.length <= 1 ? names.join("") : names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

/** Test access for one repository: does the GitHub token reach THIS repo — the
 * failure people actually hit (a typo'd slug, or a private repo the token has
 * no scope for; GitHub 404s both). Issue work has to push, so a read-only
 * token is said there. */
function TestAccess({
  repo,
  use,
  onResult,
}: {
  repo: string;
  use?: RepoUse;
  /** The add flow listens: a failed test makes Finish ask before saving (round 5). */
  onResult?(ok: boolean): void;
}) {
  return (
    <TestButton
      label="Test access"
      onTest={async () => {
        if (!REPO_RE.test(repo.trim())) throw new Error("type owner/name first");
        const r = await api<{
          ok?: boolean;
          error?: string;
          name?: string;
          private?: boolean;
          can_push?: boolean;
          default_branch?: string;
        }>("/api/settings/test/github-repo", { json: { repo: repo.trim() } });
        onResult?.(!!r?.ok);
        if (!r?.ok) {
          // The dialog's own Connect GitHub button sits right under this row:
          // point at it, not at a Settings path (r5-verify m6).
          const err = r?.error || "test failed";
          throw new Error(/no GitHub token/i.test(err) ? "Not connected — use Connect GitHub below" : err);
        }
        return (
          (r.name || repo) +
          (r.private ? " (private)" : "") +
          (r.default_branch ? " · default " + r.default_branch : "") +
          (r.can_push ? " · can push" : "") +
          (use === "issue" && !r.can_push ? " · read-only token — issue work can't push a branch" : "")
        );
      }}
    />
  );
}

/** One repository's editor, attached under its heading on one surface. */
export function RepoEditor({
  use,
  repo,
  liveBranch,
}: {
  use: RepoUse;
  repo: string;
  liveBranch?: string;
}) {
  const s = useSettings();
  const uses = usesOf(s.settings, repo);
  const setUses = (next: RepoUse[]) => {
    const patch = usesPatch(s.settings, repo, next);
    const added = next.filter((u) => !uses.includes(u));
    const dropped = uses.filter((u) => !next.includes(u));
    void s
      .saveGroups(
        patch,
        added.length
          ? repo + " — now also " + usesText(added)
          : repo + " — no longer " + usesText(dropped)
      )
      .then((ok) => ok && resweep([...added, ...dropped]));
  };
  return (
    <div className="ik-editor-body" data-repo={repo} data-use={use}>
      {/* Verify's editor is about Verify: the PR-comment / issue ticks are
          Intake's business (r5-verify m7). */}
      {use !== "verify" && <UseTicks repo={repo} fixed={use} value={uses} onChange={setUses} />}
      <RepoFields use={use} repo={repo} liveBranch={liveBranch} />
      <div className="ik-editor-foot">
        <span className="test-row">
          <TestAccess repo={repo} use={use} />
        </span>
        <ArmButton
          className="tk-remove"
          label="Remove"
          armedLabel={"Confirm — stop watching " + repo + (use === "verify" ? "" : " for " + USE_LABEL[use])}
          title={"Take " + repo + " off this list — its other uses stay"}
          onConfirm={() => {
            void s
              .saveGroups(
                usesPatch(
                  s.settings,
                  repo,
                  uses.filter((u) => u !== use)
                ),
                "Removed " + repo
              )
              .then((ok) => ok && resweep([use]));
          }}
        />
      </div>
    </div>
  );
}

/** The opt-in for a repo with no GitHub remote: a file to commit. Such a
 * checkout cannot be named on the list (there is no slug to type); its own
 * committed `.mindflock.toml` is OR'd with the list. */
const OPT_IN_SNIPPET = "[workspace]\nverify_on_push = true\n";

export function NoRemoteOptIn() {
  return (
    <details className="pr-advanced vf-optin">
      <summary>Repository without a GitHub remote?</summary>
      <div className="pr-advanced-body">
        <p className="set-hint">
          Commit this as <code>.mindflock.toml</code> at the repository's root — each new branch
          pushed there then gets a checklist.
        </p>
        <div className="vf-snippet">
          <pre>{OPT_IN_SNIPPET}</pre>
          <button
            type="button"
            className="test-btn"
            onClick={() =>
              void copyText(OPT_IN_SNIPPET).then((ok) =>
                toast(ok ? "Copied" : "Couldn't copy — select the text instead"),
              )
            }
          >
            Copy
          </button>
        </div>
      </div>
    </details>
  );
}

/** The repository half of "+ Add source".
 *
 * Step 2 — Connect: the slug (suggestions are the repositories already watched
 * elsewhere and every ticket source's GitHub repo), the GitHub line, Test
 * access, and the uses as ticks. "Add & continue" saves the ticked lists in
 * ONE request; "Finish with defaults" does the same and closes.
 * Step 3 — What it does: each newly ticked use's own fields, already saved.
 *
 * Nothing here turns a switch on: a repository on the PR list of a setup that
 * never chose is saved with PR comments OFF, and issue handling is opt-in. */
export function AddRepoFlow({
  uses: initialUses,
  trackers = [],
  onPickTracker,
  onDone,
  onCancel,
  title,
  liveBranch,
  only,
}: {
  uses: RepoUse[];
  /** The tracker tiles to offer beside Repository ([] = repositories only). */
  trackers?: AddKind[];
  onPickTracker?(provider: string): void;
  onDone(repo: string, uses: RepoUse[]): void;
  onCancel(): void;
  title?: string;
  liveBranch?: string;
  /** Verify: only the Verify use is on offer, so no ticks at all. */
  only?: RepoUse;
}) {
  const s = useSettings();
  const ticketSources = useTicketingSources(true).data || [];
  const [step, setStep] = useState<2 | 3>(2);
  const [slug, setSlugRaw] = useState("");
  const setSlug = (v: string) => {
    setSlugRaw(v);
    setAccessFailed(false);
    setSaveAnyway(false);
  };
  const [ticks, setTicks] = useState<RepoUse[]>(only ? [only] : initialUses);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [added, setAdded] = useState<{ repo: string; uses: RepoUse[] } | null>(null);
  // Round 5 (r5-verify B2): a repository whose access test FAILED is never
  // saved on the first press — the second press is an explicit "Save anyway".
  const [accessFailed, setAccessFailed] = useState(false);
  const [saveAnyway, setSaveAnyway] = useState(false);
  const repo = slug.trim();
  const already = REPO_RE.test(repo) ? usesOf(s.settings, repo) : [];
  const ticketRepos = ticketSources.flatMap((src) => sourceRepos(src).map((r) => githubSlugOf(r.url)));
  const suggestions = knownRepos(s.settings, ticketRepos).filter(
    (r) => r.toLowerCase() !== repo.toLowerCase()
  );
  const kinds: AddKind[] = trackers.length
    ? [...trackers, REPO_KIND]
    : [];

  const commit = async (andContinue: boolean) => {
    if (!REPO_RE.test(repo)) {
      setError("Type the repository as owner/name — e.g. mindflockai/MindFlock.");
      return;
    }
    const want = [...new Set([...already, ...ticks])];
    if (!ticks.length && !already.length) {
      setError("Tick at least one use.");
      return;
    }
    if (accessFailed && !saveAnyway) {
      setSaveAnyway(true);
      setError("The access test failed — fix the name or Connect GitHub, or press Save anyway.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const fresh = ticks.filter((u) => !already.includes(u));
      const patch = usesPatch(s.settings, repo, want);
      if (Object.keys(patch).length) {
        const ok = await s.saveGroups(patch, "Added " + repo + (fresh.length ? " — " + usesText(fresh) : ""));
        if (!ok) {
          // The toast said why; stay on this step with what was typed.
          setError("Couldn't add " + repo + " — try again.");
          return;
        }
        resweep(fresh);
      }
      const shown = ticks.length ? ticks : already;
      setAdded({ repo, uses: shown });
      if (andContinue) setStep(3);
      else onDone(repo, shown);
    } finally {
      setBusy(false);
    }
  };

  const footer =
    step === 2 ? (
      <>
        <span className="ik-add-error" role="alert">
          {error}
        </span>
        <button type="button" className="ik-quiet-btn" disabled={busy} onClick={() => void commit(false)}>
          {saveAnyway ? "Save anyway" : "Finish with defaults"}
        </button>
        <button
          type="button"
          className="btn-primary"
          id="ik-add-next"
          disabled={busy || !repo}
          onClick={() => void commit(true)}
        >
          {busy ? "Adding…" : "Add & continue ›"}
        </button>
      </>
    ) : (
      <>
        <span className="ik-add-error" />
        <button
          type="button"
          className="btn-primary"
          id="ik-add-finish"
          onClick={() => added && onDone(added.repo, added.uses)}
        >
          Finish
        </button>
      </>
    );

  return (
    <AddFlowShell
      title={title}
      step={step}
      kinds={kinds}
      picked="repo"
      onPick={(k) => {
        if (k !== "repo") onPickTracker?.(k);
      }}
      onCancel={onCancel}
      cancelLabel={step === 3 ? "Close" : "Cancel"}
      footer={footer}
    >
      {step === 2 ? (
        <div className="ik-add-body">
          <div className="set-row">
            <label className="set-label" htmlFor="ik-add-repo">
              Repository
            </label>
            {/* The slug and the one check that matters for it — does the token
                reach THIS repository — on one line; the answer wraps under. */}
            <div className="ik-repo-row">
              <input
                type="text"
                id="ik-add-repo"
                autoFocus
                autoComplete="off"
                spellCheck={false}
                list="ik-add-repo-known"
                placeholder="owner/name — e.g. mindflockai/MindFlock"
                value={slug}
                className={error && !REPO_RE.test(repo) ? "field-missing" : ""}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setError("");
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void commit(true);
                  }
                }}
              />
              <TestAccess
                repo={repo}
                use={ticks.includes("issue") ? "issue" : undefined}
                onResult={(ok) => {
                  setAccessFailed(!ok);
                  setSaveAnyway(false);
                }}
              />
            </div>
            <datalist id="ik-add-repo-known">
              {suggestions.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
            <span className="set-hint">
              {only === "verify" ? "Each new branch pushed in it gets a checklist. " : ""}
              {suggestions.length
                ? "Any clone counts. Known: " + suggestions.slice(0, 3).join(", ") + (suggestions.length > 3 ? "…" : "")
                : "Any clone of it counts — it is matched by its GitHub remote, not its path."}
            </span>
          </div>
          <GithubStatusLine needFor="a repository can't be read without it" />
          {!only && (
            <UseTicks
              repo={repo}
              value={[...new Set([...ticks, ...already])]}
              disabledUses={already}
              onChange={(next) => setTicks(next.filter((u) => !already.includes(u)))}
            />
          )}
          {!only ? (
            <p className="set-hint ik-uses-hint">
              Each tick adds it to that list. Nothing auto-starts until that tab's switch is on.
            </p>
          ) : null}
          {(only === "verify" || ticks.includes("verify")) && <NoRemoteOptIn />}
        </div>
      ) : added ? (
        <div className="ik-add-body">
          <p className="set-hint ik-add-done">
            <strong>{added.repo}</strong> is on the list for {usesText(added.uses)}. Blank fields
            inherit the defaults — change any now, or later with <strong>Edit</strong> on its heading.
          </p>
          {REPO_USES.filter((u) => added.uses.includes(u)).map((u) => (
            <div key={u} className="ik-add-use">
              {added.uses.length > 1 ? <h4 className="ik-editor-h">{USE_LABEL[u]}</h4> : null}
              <RepoFields use={u} repo={added.repo} liveBranch={liveBranch} />
            </div>
          ))}
        </div>
      ) : null}
    </AddFlowShell>
  );
}
