/** The repo source list behind Intake → Pull requests, Intake → Issues and
 * Verify → Repositories.
 *
 * One collapsible card per watched repository, matching a ticketing source card
 * exactly: the thing being watched, which agent CLI its sessions run, the
 * filters that decide what gets picked up, a Test button and a Remove.
 *
 * This replaced a flat chip list of `owner/name` strings plus one screen-wide
 * set of options. The chip list was smaller but it could not express the thing
 * people actually want from a multi-repo setup — "watch this one too, but give
 * it half an hour of grace and run it on codex" — because there was nowhere to
 * hang a per-repo value. Now there is: the screen-wide fields (in Advanced)
 * are the DEFAULT each card inherits, and a card's own field overrides it.
 * `github.repo_settings` / `github.issue_repo_settings` hold the overrides;
 * see `GithubSettings` for the storage shape and `GithubConfig.min_age_for`
 * and friends for how the monitors resolve them.
 *
 * Verify is the third surface, and it is here rather than in its own component
 * on purpose. What a person does on it is the same act — "track this GitHub
 * repo, and here is what is special about it" — down to typing `owner/name`
 * into a card, and a second list that looked almost but not quite like this one
 * would read as a bolted-on feature. It stores its pair on the `repository`
 * group instead (`verify_repos` / `verify_repo_settings`) and shows a different
 * two fields; `surface` is what decides that, and it was already the thing
 * deciding which fields a card shows.
 */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { toast } from "../../lib/toast";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { SourceCard, TestButton } from "./kit";
import { DEPTH_LABELS, SOURCE_DEPTHS } from "../../lib/autopilot";

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

/** A card's own values. Absent/blank = inherit the screen-wide default.
 *
 * One interface for all three surfaces, not a union: every field is already
 * optional, and the surfaces are stored in separate maps, so a `pr` card can no
 * more read a `live_branch` than it can invent one. Splitting it would buy a
 * compile-time distinction the storage does not make and cost the `patch`
 * helper below its single `keyof` signature. */
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
  /** Verify only: where this repo's running product is. See the field's own
   * hint — blank means "there is no deployment to point at", which is a real
   * answer for a library or a CLI and not a gap to be filled with a guess. */
  target?: string;
  /** Verify only: standing instructions folded into the prompt that writes a
   * plan for this repo. The useful thing to say is usually repo-shaped and
   * repetitive ("the UI runs on :3000"), and retyping it per plan is how it
   * stops being said. Truncated server-side; see `VERIFY_PROMPT_MAX`. */
  prompt?: string;
}

export type RepoOverrides = Record<string, RepoOverride>;

/** A card in the list. `key` is local and stable so a not-yet-named card keeps
 * its identity (and its typing) across re-renders — the repo slug can't do that
 * job while it is still blank. */
interface Card {
  key: number;
  repo: string;
}

let nextKey = 1;

const named = (list: Card[]) => list.map((c) => c.repo.trim()).filter(Boolean);

/** A card's identity for per-card UI state. Its slug once it has one; until
 * then a per-draft token, so two unnamed cards are two cards. */
const cardKey = (c: Card) => c.repo || "new-" + c.key;

function overrideText(o: RepoOverride | undefined, field: "base_branch" | "skip_authors"): string {
  if (!o) return "";
  const v = o[field];
  if (Array.isArray(v)) return v.join(", ");
  return v == null ? "" : String(v);
}

export function RepoSourceList({
  repos,
  overrides,
  onSave,
  surface,
  defaults,
  label,
  listId,
  addId,
  addLabel,
  emptyText,
  hint,
}: {
  repos: string[];
  overrides: RepoOverrides;
  /** Persist both halves at once — they are one edit as far as the user is
   * concerned, and two saves would let a rename land without its settings. */
  onSave(repos: string[], overrides: RepoOverrides, msg: string): void;
  /** Which fields a card carries. "pr" additionally offers a base-branch
   * filter; issue work always branches off the repo's own default, so that
   * field would be a decoration there. "verify" is a different card entirely:
   * it shows only the live branch and the standing instructions, because
   * nothing else on this list applies to it — a verify run has no author to
   * skip, no PR to age out, no depth to carry an item to, and it runs the
   * agent the SESSION already uses rather than one picked per repo. Fields
   * that cannot do anything are not disabled here, they are absent. */
  surface: "pr" | "issue" | "verify";
  /** The screen-wide values a blank card field falls back to, for placeholders.
   * Showing the inherited value is what makes "empty" legible as "inherit 15"
   * rather than "no grace period at all". Surfaces pass "" for the ones they do
   * not render (Issues already does that for `baseBranch`), so this stays one
   * bag rather than three. */
  defaults: {
    agent: string;
    baseBranch: string;
    minAge: string;
    skipAuthors: string;
    /** Verify only: the flock-wide live branch, i.e. what a card that sets none
     * of its own actually goes due against. */
    liveBranch: string;
    /** The flock-wide deploy wait, shown as this field's placeholder. */
    deployDelay?: string;
  };
  /** The field label above the card list, e.g. "Repositories to review". */
  label: string;
  listId?: string;
  addId?: string;
  addLabel: string;
  emptyText: string;
  hint: React.ReactNode;
}) {
  // Unconditional, because it is a hook: the verify surface never renders the
  // picker this feeds and pays one local GET /api/providers for it. Splitting
  // the component in two to save that request would be two components to keep
  // in step for the rest of the app's life, which is the more expensive of the
  // two costs by some distance.
  const agents = useAgentChoices();
  const [cards, setCards] = useState<Card[]>(() =>
    repos.map((r) => ({ key: nextKey++, repo: r }))
  );
  // The set of EXPANDED cards, not collapsed ones — so anything not in it is a
  // one-line summary, whenever it shows up. Seeding a collapsed-set instead had
  // two ways to fail: the keys were the numeric card ids while `nextKey` is
  // module-level and monotonic (only the page's first mount lined up), and the
  // settings model loads async, so a tab opened directly — the sidebar bars
  // deep-link straight to one — mounted with `repos` still empty and every card
  // arrived unlisted, i.e. expanded. Keyed by `cardKey` so a draft is distinct
  // from every other draft and survives being named.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // The card list and the override table are both held locally and reconciled
  // against the store, rather than read straight from the props. Two reasons,
  // and they are the same reason: our own save's echo must not rebuild the cards
  // mid-edit (it would drop a blank draft and steal focus), and two field edits
  // in quick succession must COMPOSE — `onSave` replaces `repo_settings`
  // wholesale, so a second patch built from the last echo would silently drop
  // the first. Content comparison is what tells our echo from a real change.
  const [ov, setOv] = useState<RepoOverrides>(() => overrides);
  const saved = repos.join("\n");
  const savedOv = JSON.stringify(overrides);
  useEffect(() => {
    setCards((prev) => {
      if (named(prev).join("\n") === saved) return prev; // our own echo
      // A not-yet-named card has nothing in the store to come back as, so it is
      // carried across the rebuild rather than discarded.
      const drafts = prev.filter((c) => !c.repo.trim());
      return [...repos.map((r) => ({ key: nextKey++, repo: r })), ...drafts];
    });
    // `repos` is `saved`'s content, so keying on the string alone is exact.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);
  useEffect(() => {
    setOv((prev) => (JSON.stringify(prev) === savedOv ? prev : overrides));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedOv]);

  /** Whether a card's settings outlive the card being removed.
   *
   * They do NOT on the Intake surfaces: what a pr/issue block holds is an agent
   * name, a depth, an age — values picked from a menu in two seconds, and a map
   * that only ever grows would keep handing an old agent choice back to a repo
   * somebody re-added months later, having changed CLIs in between.
   *
   * They DO on Verify, because a verify block holds text the user WROTE: a live
   * branch and up to `VERIFY_PROMPT_MAX` characters of standing instructions
   * that took a minute to phrase and cannot be reconstructed from anything on
   * screen. Remove is a single unguarded click on a card list, and losing that
   * paragraph to a misclick with no confirm and no undo is not a trade worth
   * making to keep settings.json tidy. The block is inert while the repo is off
   * the list — the backend decides tracking from `verify_repos` alone and reads
   * an orphan block as nothing at all (see `_verify_lookup`) — so keeping it
   * cannot re-track anything; it only means a re-add a minute later comes back
   * with what you typed, which is what `settings._verify_repo_settings` and
   * `test_plans._verify_lookup` both say happens. */
  const keepsRemovedBlocks = surface === "verify";

  /** Persist `list` and the override table that belongs with it.
   *
   * Blocks whose repo has left the list are dropped, or kept when the surface
   * says they are the user's own words — see `keepsRemovedBlocks`. The pruning
   * is not just tidiness on the Intake surfaces: `onSave` replaces the stored
   * map wholesale, so a block left behind there is a value that silently
   * reappears. */
  const persist = (list: Card[], nextOverrides: RepoOverrides, msg: string) => {
    const slugs = named(list);
    let kept: RepoOverrides;
    if (keepsRemovedBlocks) kept = { ...nextOverrides };
    else {
      kept = {};
      for (const slug of slugs) if (nextOverrides[slug]) kept[slug] = nextOverrides[slug];
    }
    setOv(kept);
    onSave(slugs, kept, msg);
  };

  /** Commit a slug edit. Returns the value to leave in the input — the same
   * string on success, the card's existing slug when the edit is refused. */
  const rename = (key: number, raw: string): string => {
    const val = raw.trim();
    const card = cards.find((c) => c.key === key);
    if (!card) return raw;
    if (val === card.repo) return val;
    if (val && !REPO_RE.test(val)) {
      toast("Use owner/name, e.g. MindFlock/MindFlock");
      return card.repo;
    }
    if (val && cards.some((c) => c.key !== key && c.repo.toLowerCase() === val.toLowerCase())) {
      toast(val + " is already in the list");
      return card.repo;
    }
    if (!val && card.repo) {
      // Blanking the field used to drop the repo AND its settings while the
      // card stayed on screen still showing them — so retyping the same slug
      // came back with everything silently reset. Stopping watching a repo is
      // what Remove is for, and it says so.
      toast("Repository can't be blank — use Remove to stop watching " + card.repo);
      return card.repo;
    }
    const next = cards.map((c) => (c.key === key ? { ...c, repo: val } : c));
    setCards(next);
    // Carry this card's settings across the rename — they belong to the card,
    // not to the string that happened to name it.
    const moved: RepoOverrides = { ...ov };
    if (card.repo && moved[card.repo]) {
      moved[val] = moved[card.repo];
      delete moved[card.repo];
    }
    // Carry the open/closed flag across the rename too — naming a new card is
    // the middle of configuring it, not the end.
    setExpanded((prev) => {
      const was = cardKey(card);
      if (!prev.has(was)) return prev;
      const nextSet = new Set(prev);
      nextSet.delete(was);
      nextSet.add(val);
      return nextSet;
    });
    persist(next, moved, card.repo ? "Renamed to " + val : "Added " + val);
    return val;
  };

  const patch = (repo: string, field: keyof RepoOverride, value: string) => {
    const next: RepoOverrides = { ...ov };
    const block: RepoOverride = { ...(next[repo] || {}) };
    const v = value.trim();
    if (!v) delete block[field];
    else if (field === "skip_authors")
      block.skip_authors = v.split(",").map((x) => x.trim()).filter(Boolean);
    else if (field === "min_age_minutes") block.min_age_minutes = v;
    else block[field] = v;
    if (Object.keys(block).length) next[repo] = block;
    else delete next[repo];
    persist(cards, next, "Saved " + repo);
  };

  const remove = (key: number) => {
    const card = cards.find((c) => c.key === key);
    const next = cards.filter((c) => c.key !== key);
    setCards(next);
    const moved: RepoOverrides = { ...ov };
    // Left in place where the block is text the user wrote (see
    // `keepsRemovedBlocks`); dropped where it is a couple of menu picks.
    if (card?.repo && !keepsRemovedBlocks) delete moved[card.repo];
    persist(next, moved, card?.repo ? "Removed " + card.repo : "Removed the empty card");
  };

  const add = () => {
    // A new card starts expanded: an unnamed one has nothing to summarize, and
    // typing the slug is the whole reason you clicked.
    const key = nextKey++;
    setCards((prev) => [...prev, { key, repo: "" }]);
    setExpanded((prev) => new Set(prev).add("new-" + key));
  };

  return (
    <div className="set-row">
      <span className="set-label">{label}</span>
      <div id={listId} className="ik-cards">
        {!cards.length ? (
          <div className="repo-empty">{emptyText}</div>
        ) : (
          cards.map((card) => {
            const o = ov[card.repo];
            const agent = o?.agent || "";
            const depth = o?.depth || "";
            // What this repo measures "shipped" against, override first. Only
            // read on the verify surface, where it is both the summary line and
            // the live-branch placeholder — one expression so the two can never
            // disagree about what a blank field inherits.
            const live = o?.live_branch || defaults.liveBranch;
            const summary =
              surface === "verify"
                ? // The agent is not a verify card's business (see `surface`),
                  // so the summary carries the fact that IS: which branch a plan
                  // written here waits for. Dropped entirely when the server has
                  // not answered yet rather than guessed at — naming the wrong
                  // live branch is the one lie this surface cannot afford.
                  (card.repo || "New repository") + (live ? " · live: " + live : "")
                : (card.repo || "New repository") +
                  " · " +
                  (agent || (defaults.agent ? defaults.agent + " (default)" : "app default"));
            return (
              <SourceCard
                key={card.key}
                sourceId={card.repo || "new-" + card.key}
                summary={summary}
                collapsed={!expanded.has(cardKey(card))}
                onToggle={() =>
                  setExpanded((prev) => {
                    const next = new Set(prev);
                    const k = cardKey(card);
                    if (next.has(k)) next.delete(k);
                    else next.add(k);
                    return next;
                  })
                }
                footer={
                  <>
                    <TestButton
                      label="Test access"
                      onTest={async () => {
                        const r = await api<{
                          ok?: boolean;
                          error?: string;
                          name?: string;
                          private?: boolean;
                          can_push?: boolean;
                          default_branch?: string;
                        }>("/api/settings/test/github-repo", { json: { repo: card.repo } });
                        if (!r?.ok) throw new Error(r?.error || "test failed");
                        return (
                          (r.name || card.repo) +
                          (r.private ? " (private)" : "") +
                          (r.default_branch ? " · default " + r.default_branch : "") +
                          (surface === "issue" && !r.can_push
                            ? " · read-only token — issue work can't push a branch"
                            : "")
                        );
                      }}
                    />
                    <button
                      type="button"
                      className="test-btn tk-remove"
                      onClick={() => remove(card.key)}
                    >
                      Remove
                    </button>
                  </>
                }
              >
                <label className="set-row">
                  <span className="set-label">Repository</span>
                  <input
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    required
                    data-repo-field="repo"
                    className={card.repo ? "" : "field-missing"}
                    placeholder="owner/name — e.g. mindflockai/MindFlock"
                    defaultValue={card.repo}
                    onBlur={(e) => {
                      // Uncontrolled, so a refused edit has to be put back by
                      // hand — otherwise the field keeps showing a slug that
                      // was never saved.
                      e.target.value = rename(card.key, e.target.value);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                    }}
                  />
                  <span className="set-hint">
                    Required — press Enter or click away to save.{" "}
                    {surface === "verify"
                      ? // Verify's list is not one of the Intake tabs' and does not
                        // read like one, so it gets the sentence that actually
                        // matters here: how a checkout on this machine is matched
                        // to the name you typed. It is the remote, not the path —
                        // which is the whole reason this list is slugs.
                        "A checkout counts as this repo when its origin points here, so it works" +
                        " wherever you cloned it and on every machine in the flock."
                      : "This list is separate from the other GitHub tab's; a repo can be on either, or both."}
                  </span>
                </label>
                {surface === "verify" && (
                  <>
                    <label className="set-row">
                      <span className="set-label">Live branch</span>
                      <input
                        // Keyed by the slug, not just by the card, so naming a
                        // card REMOUNTS this box. These fields are uncontrolled
                        // (`defaultValue` is read once, at mount), and a verify
                        // block outlives its repo leaving the list — so without
                        // the remount, re-adding `owner/name` would show an
                        // empty box over a stored branch that is genuinely
                        // still in effect, which reads as "it was lost".
                        key={"lb-" + card.repo}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        data-repo-field="live_branch"
                        disabled={!card.repo}
                        // The placeholder is the INHERITED answer, not a hint at
                        // the format: an empty box has to read as "inherits
                        // staging", and grey text saying "staging" is the only way
                        // to say that without typing it into the field and storing
                        // it as an override that was never asked for.
                        placeholder={live || "main"}
                        defaultValue={o?.live_branch || ""}
                        onBlur={(e) => {
                          if (e.target.value.trim() !== (o?.live_branch || ""))
                            patch(card.repo, "live_branch", e.target.value);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        }}
                      />
                      <span className="set-hint">
                        Which branch counts as shipped <em>for this repo</em>: a plan
                        written here goes due when its commit lands on it. Blank inherits{" "}
                        <code>{defaults.liveBranch || "the flock-wide setting"}</code> —
                        Settings → Workspace, falling through the PR base and the base
                        branch to <code>main</code>.
                      </span>
                    </label>
                    <label className="set-row">
                      <span className="set-label">Deploy takes (minutes)</span>
                      <input
                        // Remounted on rename for the same reason as the branch
                        // box above.
                        key={"dd-" + card.repo}
                        type="number"
                        min={0}
                        step={1}
                        autoComplete="off"
                        data-repo-field="deploy_delay_minutes"
                        disabled={!card.repo}
                        placeholder={defaults.deployDelay || "5"}
                        defaultValue={o?.deploy_delay_minutes || ""}
                        onBlur={(e) => {
                          if (
                            e.target.value.trim() !==
                            (o?.deploy_delay_minutes || "")
                          )
                            patch(card.repo, "deploy_delay_minutes", e.target.value);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        }}
                      />
                      <span className="set-hint">
                        How long after merging before the change is actually running.
                        A checklist waits that long before it turns up to be checked —
                        merged is not deployed, and checking too early records a
                        failure against code that is fine. <code>0</code> if merging is
                        shipping here. Blank inherits{" "}
                        <code>{defaults.deployDelay || "5"}</code>.
                      </span>
                    </label>
                    <label className="set-row">
                      <span className="set-label">Where it runs (optional)</span>
                      <input
                        // Remounted on rename for the same reason as the branch
                        // box above.
                        key={"tg-" + card.repo}
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        data-repo-field="target"
                        maxLength={VERIFY_TARGET_MAX}
                        disabled={!card.repo}
                        placeholder="e.g. https://app.example.com — log in as qa@example.com"
                        defaultValue={o?.target || ""}
                        onBlur={(e) => {
                          if (e.target.value.trim() !== (o?.target || ""))
                            patch(card.repo, "target", e.target.value);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        }}
                      />
                      <span className="set-hint">
                        The deployment this repo's users actually reach. THE knob that
                        decides what "it works" is checked against: with it set, both the
                        checklist and the agent that works it are aimed at{" "}
                        <em>that running system</em>. Blank is a real answer, not a missing
                        one — a library, a CLI or anything with no environment to point at
                        is checked against a fresh checkout of the live branch on this
                        machine, and a checklist for one says so rather than pretending
                        otherwise.
                      </span>
                    </label>
                    <label className="set-row">
                      <span className="set-label">Extra instructions (optional)</span>
                      <textarea
                        // Remounted on rename for the same reason as the branch
                        // box above.
                        key={"pr-" + card.repo}
                        // The one class this list borrows from VerifyDialog.css,
                        // because this is the one field that only exists there.
                        className="vf-repo-prompt"
                        rows={3}
                        spellCheck={false}
                        data-repo-field="prompt"
                        // The server's cap, enforced where the text is typed:
                        // it truncates silently on save, so without this the box
                        // would go on showing a paragraph half of which was
                        // never stored — and every later blur would re-post and
                        // re-toast "Saved", because the DOM and the stored value
                        // can no longer agree. See `VERIFY_PROMPT_MAX`.
                        maxLength={VERIFY_PROMPT_MAX}
                        disabled={!card.repo}
                        placeholder={
                          "e.g. The UI runs on :3000 — check there, not :8080.\n" +
                          "Always confirm the migration ran.\n" +
                          "Ignore anything under vendor/."
                        }
                        defaultValue={o?.prompt || ""}
                        onBlur={(e) => {
                          if (e.target.value.trim() !== (o?.prompt || ""))
                            patch(card.repo, "prompt", e.target.value);
                        }}
                      />
                      <span className="set-hint">
                        Folded into the prompt that writes a plan <em>for this repo</em>, so
                        the things that are true of every change here — where the app runs,
                        what to always check, what to ignore — do not have to be retyped into
                        each plan. It steers <em>what</em> gets tested; it cannot change the
                        format the generator must answer in, so a note here can never break
                        plan writing. Click away to save. Up to{" "}
                        {VERIFY_PROMPT_MAX.toLocaleString()} characters — the box stops there
                        rather than letting you paste a page that would be trimmed on save.
                      </span>
                    </label>
                  </>
                )}
                {/* Everything an INTAKE card carries. Absent rather than
                    disabled on the verify surface: a greyed-out "Skip authors"
                    on a repo whose plans have no authors would invite the reader
                    to work out what would un-grey it, and the answer is
                    "nothing". One guard around the four rather than four guards
                    so the reading is "these belong to the other two surfaces",
                    which is the actual rule. */}
                {surface !== "verify" && (
                  <>
                    <label className="set-row">
                      <span className="set-label">Agent CLI</span>
                      <select
                        data-repo-field="agent"
                        value={agent}
                        disabled={!card.repo}
                        onChange={(e) => patch(card.repo, "agent", e.target.value)}
                      >
                        <option value="">
                          {defaults.agent ? `Inherit (${defaults.agent})` : "Inherit (app default)"}
                        </option>
                        {agents.names.map((n) => (
                          <option key={n} value={n}>
                            {n}
                          </option>
                        ))}
                      </select>
                      <span className="set-hint">
                        Which coding CLI this repository's sessions run. Route one repo to a
                        cloud CLI and another to a local model — pick a provider whose
                        Connections row is green, or inherit the tab's default.
                      </span>
                    </label>
                    <label className="set-row">
                      <span className="set-label">Take them as far as</span>
                      <select
                        data-repo-field="depth"
                        value={depth}
                        disabled={!card.repo}
                        onChange={(e) => patch(card.repo, "depth", e.target.value)}
                      >
                        <option value="">Off — stop after the agent works</option>
                        {SOURCE_DEPTHS.map((d) => (
                          <option key={d} value={d}>
                            {DEPTH_LABELS[d]}
                          </option>
                        ))}
                      </select>
                      <span className="set-hint">
                        How far each item from this repo carries itself once the agent
                        finishes: commit, push, open a PR. Merge is not offered for a whole
                        repo — pick it on an individual row instead.
                      </span>
                    </label>
                    {surface === "pr" && (
                      <label className="set-row">
                        <span className="set-label">Base branch</span>
                        <input
                          type="text"
                          autoComplete="off"
                          spellCheck={false}
                          data-repo-field="base_branch"
                          disabled={!card.repo}
                          placeholder={defaults.baseBranch || "inherit the tab default"}
                          defaultValue={overrideText(o, "base_branch")}
                          onBlur={(e) => {
                            if (e.target.value.trim() !== overrideText(o, "base_branch"))
                              patch(card.repo, "base_branch", e.target.value);
                          }}
                        />
                        <span className="set-hint">
                          Only PRs targeting this branch are auto-reviewed here. Blank inherits
                          the tab default — which is <em>not</em> "every branch": it falls
                          through to your repository's configured base branch. A PR into any
                          other branch is still listed below, with a chip saying so, and{" "}
                          <strong>Begin review</strong> works on it.
                        </span>
                      </label>
                    )}
                    <label className="set-row">
                      <span className="set-label">
                        {surface === "pr" ? "Min PR age (minutes)" : "Min issue age (minutes)"}
                      </span>
                      <input
                        type="number"
                        className="num-sm"
                        autoComplete="off"
                        data-repo-field="min_age_minutes"
                        disabled={!card.repo}
                        placeholder={defaults.minAge || "15"}
                        defaultValue={o?.min_age_minutes == null ? "" : String(o.min_age_minutes)}
                        onBlur={(e) => {
                          const cur = o?.min_age_minutes == null ? "" : String(o.min_age_minutes);
                          if (e.target.value.trim() !== cur)
                            patch(card.repo, "min_age_minutes", e.target.value);
                        }}
                      />
                      <span className="set-hint">
                        Grace period after it opens before work starts, so you can finish
                        pushing or writing. Blank inherits the tab default.
                      </span>
                    </label>
                    <label className="set-row">
                      <span className="set-label">Skip authors</span>
                      <input
                        type="text"
                        autoComplete="off"
                        spellCheck={false}
                        data-repo-field="skip_authors"
                        disabled={!card.repo}
                        placeholder={defaults.skipAuthors || "inherit — none"}
                        defaultValue={overrideText(o, "skip_authors")}
                        onBlur={(e) => {
                          if (e.target.value.trim() !== overrideText(o, "skip_authors"))
                            patch(card.repo, "skip_authors", e.target.value);
                        }}
                      />
                      <span className="set-hint">
                        Comma-separated GitHub logins.{" "}
                        {surface === "pr"
                          ? "Review only ever takes your own PRs, so this drops their review comments instead — the bots whose feedback you don't want acted on."
                          : "Issues opened by these accounts are ignored."}{" "}
                        Blank inherits the tab default.
                      </span>
                    </label>
                  </>
                )}
              </SourceCard>
            );
          })
        )}
      </div>
      <div className="set-row">
        <button type="button" id={addId} className="btn-primary" onClick={add}>
          {addLabel}
        </button>
      </div>
      <span className="set-hint">{hint}</span>
    </div>
  );
}
