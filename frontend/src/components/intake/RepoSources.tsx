/** The repo source list behind Intake → Pull requests and Intake → Issues.
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
 */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { toast } from "../../lib/toast";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { SourceCard, TestButton } from "./kit";
import { DEPTH_LABELS, SOURCE_DEPTHS } from "../../lib/autopilot";

export const REPO_RE = /^[^\s/]+\/[^\s/]+$/;

/** A card's own values. Absent/blank = inherit the screen-wide default. */
export interface RepoOverride {
  agent?: string;
  /** How far autopilot carries an item ingested from this repo ("" = inherit).
   * Never "merge" — a per-repo default applies to every future item with nobody
   * watching, and a merge cannot be undone. */
  depth?: string;
  base_branch?: string;
  min_age_minutes?: number | string;
  skip_authors?: string[] | string;
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
  /** "pr" additionally offers a base-branch filter; issue work always branches
   * off the repo's own default, so that field would be a decoration there. */
  surface: "pr" | "issue";
  /** The screen-wide values a blank card field falls back to, for placeholders.
   * Showing the inherited value is what makes "empty" legible as "inherit 15"
   * rather than "no grace period at all". */
  defaults: { agent: string; baseBranch: string; minAge: string; skipAuthors: string };
  /** The field label above the card list, e.g. "Repositories to review". */
  label: string;
  listId?: string;
  addId?: string;
  addLabel: string;
  emptyText: string;
  hint: React.ReactNode;
}) {
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

  /** Persist `list`, keeping only overrides whose repo is still in it. */
  const persist = (list: Card[], nextOverrides: RepoOverrides, msg: string) => {
    const slugs = named(list);
    const kept: RepoOverrides = {};
    for (const slug of slugs) if (nextOverrides[slug]) kept[slug] = nextOverrides[slug];
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
    if (card?.repo) delete moved[card.repo];
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
            const summary =
              (card.repo || "New repository") +
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
                    Required — press Enter or click away to save. This list is separate from
                    the other GitHub tab's; a repo can be on either, or both.
                  </span>
                </label>
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
