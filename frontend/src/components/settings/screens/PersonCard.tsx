/** "You": the person all of your devices share (GET/POST /api/person,
 * backend/web/core/person.py, docs/people.md). Meant for the top of Settings →
 * Your devices.
 *
 * The name and the git identity are editable and sync to every device. The
 * git identity is what MindFlock's OWN commits are authored as (fast-track,
 * team-run merges, peer exports); left blank, the repo's git config decides.
 * The id is shown small with Copy, never edited: the devices agree on it when
 * they pair. It identifies you; it never signs anything in. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import type { Person, PersonPatch } from "../../../api/types";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";

export type PersonDraft = Pick<Person, "name" | "git_name" | "git_email">;

const FIELDS: (keyof PersonDraft)[] = ["name", "git_name", "git_email"];

export function draftOf(p: Person | null): PersonDraft {
  return { name: p?.name || "", git_name: p?.git_name || "", git_email: p?.git_email || "" };
}

/** The fields the draft changed, trimmed (`""` clears) — what POST takes. */
export function patchOf(saved: Person | null, draft: PersonDraft): PersonPatch {
  const base = draftOf(saved);
  const out: PersonPatch = {};
  for (const k of FIELDS) {
    const v = draft[k].trim();
    if (v !== base[k]) out[k] = v;
  }
  return out;
}

/** Why the draft can't be saved, or "" (the server checks the same). */
export function draftProblem(d: PersonDraft): string {
  const email = d.git_email.trim();
  if (email && !/^[^\s<>@]+@[^\s<>@]+$/.test(email)) return "That git email doesn't look like one.";
  if (!d.git_name.trim() !== !email)
    return "Set both the git name and email (or neither) — MindFlock uses them together.";
  return "";
}

/** The card's markup — no effects, so it renders in tests through
 * react-dom/server. */
export function PersonCardView({
  person,
  draft,
  busy,
  error,
  onChange,
  onSave,
  onCopyId,
}: {
  person: Person | null;
  draft: PersonDraft;
  busy?: boolean;
  error?: string;
  onChange?: (d: PersonDraft) => void;
  onSave?: () => void;
  onCopyId?: () => void;
}) {
  if (!person) {
    return (
      <section className="person-card" id="person-card">
        <h3 className="set-section-title">You</h3>
        <p className="set-hint">{error || "Loading…"}</p>
      </section>
    );
  }
  const problem = draftProblem(draft);
  const dirty = Object.keys(patchOf(person, draft)).length > 0;
  const set = (k: keyof PersonDraft) => (e: { target: { value: string } }) =>
    onChange?.({ ...draft, [k]: e.target.value });
  return (
    <section className="person-card" id="person-card" data-person={person.id}>
      <h3 className="set-section-title">You</h3>
      <p className="set-hint">
        One person across all of your devices — sessions, claims and MindFlock's own commits are
        stamped with it. It identifies you; it never signs anything in.
      </p>
      <label className="set-row">
        <span className="set-label">Name</span>
        <input type="text" value={draft.name} placeholder="Your name" onChange={set("name")} />
      </label>
      <label className="set-row">
        <span className="set-label">Git author name</span>
        <input type="text" value={draft.git_name} placeholder="Ada Lovelace" onChange={set("git_name")} />
      </label>
      <label className="set-row">
        <span className="set-label">Git author email</span>
        <input
          type="text"
          value={draft.git_email}
          placeholder="ada@example.com"
          onChange={set("git_email")}
        />
        <span className="set-hint">
          What MindFlock's own commits (fast-track, team-run merges, peer exports) are authored as.
          Blank: the repo's git config decides. Your agents' commits are never changed.
        </span>
      </label>
      {(problem || error) && (
        <p className="set-hint person-error" role="alert">
          {problem || error}
        </p>
      )}
      <div className="set-row person-actions">
        <button
          type="button"
          className="test-btn"
          id="person-save"
          disabled={busy || !dirty || !!problem}
          onClick={onSave}
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
      <p className="set-hint person-id">
        Person id <code>{person.id || "not made yet"}</code>{" "}
        {person.id && (
          <button type="button" className="test-btn" id="person-copy-id" onClick={onCopyId}>
            Copy
          </button>
        )}
      </p>
    </section>
  );
}

export default function PersonCard() {
  const [person, setPerson] = useState<Person | null>(null);
  const [draft, setDraft] = useState<PersonDraft>(draftOf(null));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api<Person>("/api/person")
      .then((p) => {
        if (!live) return;
        setPerson(p);
        setDraft(draftOf(p));
      })
      .catch((e: Error) => live && setError(e.message || "Couldn't load"));
    return () => {
      live = false;
    };
  }, []);

  const save = useCallback(async () => {
    const patch = patchOf(person, draft);
    if (!Object.keys(patch).length) return;
    setBusy(true);
    setError("");
    try {
      const p = await api<Person>("/api/person", { json: patch });
      setPerson(p);
      setDraft(draftOf(p));
      toast("Saved — your other devices pick it up shortly");
    } catch (e) {
      setError((e as Error).message || "Couldn't save");
    } finally {
      setBusy(false);
    }
  }, [person, draft]);

  const copyId = useCallback(() => {
    if (!person?.id) return;
    copyText(person.id).then((ok) => toast(ok ? "Person id copied" : "Copy failed"));
  }, [person]);

  return (
    <PersonCardView
      person={person}
      draft={draft}
      busy={busy}
      error={error}
      onChange={setDraft}
      onSave={save}
      onCopyId={copyId}
    />
  );
}
