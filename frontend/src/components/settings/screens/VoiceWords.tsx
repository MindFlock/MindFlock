/** Settings → Voice → Voice words (`#voice-words`, after "This computer's
 * mic"): the words the mic listens for, one row per kind — Send, End (send
 * and stop), Start over, Stop listening, Stop talking — each a small chips
 * editor (type a phrase, Enter adds it, × removes it), and the wake word, one
 * text field. A kind with no phrases of its own uses the defaults, which its
 * field shows as the placeholder; phrases of its own REPLACE them. "Reset to
 * defaults" clears them all.
 *
 * Saved through the ordinary settings model as `voice.words` — a dict with
 * only the kinds that differ (`{}` = every default) — so the server checks
 * and normalises it, and syncs it across the person's devices; GET
 * /api/voice is re-read after the save (VoiceSection), and the mic, the
 * hints and the tour use the new words at once (lib/voiceWords). The editor
 * refuses what the server would (lib/voiceWords phraseError / wakeError: at
 * most 6 phrases per kind, 1–40 characters, the wake word one or two words);
 * a save the server kept differently shows what it kept. */

import { useState } from "react";
import type { Json } from "../../../api/types";
import {
  BUILTIN_VOICE_WORDS,
  PHRASE_KINDS,
  normPhrase,
  phraseError,
  wakeError,
  wakeName,
  wordsFrom,
  type PhraseKind,
  type VoiceWords,
} from "../../../lib/voiceWords";
import type { SettingsModel } from "../useSettings";
import type { VoiceView } from "./Voice";

/** The rows, in the order a dictation meets them. */
export const WORD_ROWS: ReadonlyArray<{ kind: PhraseKind; label: string; hint: string }> = [
  { kind: "send", label: "Send", hint: "Sends the message; listening stays on." },
  { kind: "end", label: "End (send and stop)", hint: "Sends what is left and stops listening." },
  { kind: "clear", label: "Start over", hint: "Takes back the message you are saying." },
  {
    kind: "stop",
    label: "Stop listening",
    hint: "Stops, the words left unsent. A one-word phrase counts only said on its own.",
  },
  { kind: "hush", label: "Stop talking", hint: "The app stops reading aloud; listening goes on." },
];

/** The saved `voice.words`: only what the person set (normalised), never
 * the defaults. */
export function savedWords(raw: unknown): Partial<VoiceWords> {
  const r = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const out: Partial<VoiceWords> = {};
  for (const k of PHRASE_KINDS) {
    const v = r[k];
    if (Array.isArray(v) && v.length) out[k] = v.map((p) => normPhrase(String(p))).filter(Boolean);
  }
  if (typeof r.wake === "string" && normPhrase(r.wake)) out.wake = normPhrase(r.wake);
  return out;
}

/** `saved` with kind `k` set to `v` — dropped when empty or the default, so
 * `{}` means every default. */
export function withWord(
  saved: Partial<VoiceWords>,
  k: PhraseKind | "wake",
  v: string[] | string,
  defaults: VoiceWords
): Partial<VoiceWords> {
  const next: Partial<VoiceWords> = { ...saved };
  const empty = Array.isArray(v) ? !v.length : !v;
  const same = JSON.stringify(v) === JSON.stringify(defaults[k]);
  if (empty || same) delete next[k];
  else (next as Record<string, unknown>)[k] = v;
  return next;
}

const sameJson = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

export function VoiceWordsBlock({ model, view }: { model: SettingsModel; view: VoiceView | null }) {
  const saved = savedWords(model.get("voice", "words"));
  const defaults = wordsFrom(view?.words_default, BUILTIN_VOICE_WORDS);
  const locked = view?.configure_allowed === false;
  /** What the last save sent (and the document it was sent over): once the
   * server's echo is in and differs, the row says what it kept. */
  const [sent, setSent] = useState<{ k: PhraseKind | "wake"; v: unknown; doc: Json } | null>(null);
  const save = async (k: PhraseKind | "wake", v: string[] | string) => {
    const next = withWord(saved, k, v, defaults);
    setSent({ k, v: next[k], doc: model.settings });
    await model.saveField("voice", "words", next as unknown as Json);
  };
  const kept = (k: PhraseKind | "wake") =>
    sent?.k === k && sent.doc !== model.settings && !sameJson(sent.v, saved[k]) ? saved[k] ?? (k === "wake" ? "" : []) : undefined;
  const custom = PHRASE_KINDS.some((k) => saved[k]) || !!saved.wake;
  return (
    <div className="set-section" id="voice-words">
      <h3 className="set-section-title">Voice words</h3>
      <p className="set-hint" id="voice-words-hint">
        The words the mic listens for — they work everywhere: this computer's mic, the phone page and the
        wake word. Phrases you add to a row replace its defaults (shown faded).
        {custom && !locked && (
          <>
            {" "}
            <button
              type="button"
              className="linklike"
              id="voice-words-reset"
              onClick={() => {
                setSent(null);
                void model.saveField("voice", "words", {});
              }}
            >
              Reset to defaults
            </button>
          </>
        )}
      </p>
      {locked && (
        <p className="set-hint" id="voice-words-locked">
          Only the owner of this computer can change them.
        </p>
      )}
      {WORD_ROWS.map((r) => (
        <PhraseRow
          key={r.kind}
          kind={r.kind}
          label={r.label}
          hint={r.hint}
          list={saved[r.kind] || []}
          defaults={defaults[r.kind]}
          kept={kept(r.kind) as string[] | undefined}
          disabled={locked}
          onChange={(v) => void save(r.kind, v)}
        />
      ))}
      <WakeRow
        value={saved.wake || ""}
        fallback={defaults.wake}
        kept={kept("wake") as string | undefined}
        disabled={locked}
        onChange={(v) => void save("wake", v)}
      />
    </div>
  );
}

/** One kind: its own phrases as chips, a field that adds one on Enter. */
function PhraseRow(p: {
  kind: PhraseKind;
  label: string;
  hint: string;
  list: string[];
  defaults: string[];
  kept?: string[];
  disabled: boolean;
  onChange(v: string[]): void;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const id = "voice-words-" + p.kind;
  const add = () => {
    if (!draft.trim()) return;
    const why = phraseError(draft, p.list);
    setError(why);
    if (why) return;
    p.onChange([...p.list, normPhrase(draft)]);
    setDraft("");
  };
  return (
    <div className="set-row voice-words-row" data-words-kind={p.kind}>
      <label className="set-label" htmlFor={id}>
        {p.label}
      </label>
      <div className="voice-chips" role="list" aria-label={p.label}>
        {p.list.map((w) => (
          <span className="voice-chip" role="listitem" key={w}>
            {w}
            <button
              type="button"
              className="voice-chip-x"
              aria-label={"Remove " + w}
              title="Remove"
              disabled={p.disabled}
              onClick={() => p.onChange(p.list.filter((x) => x !== w))}
            >
              ×
            </button>
          </span>
        ))}
        {!p.list.length &&
          p.defaults.map((w) => (
            <span className="voice-chip is-default" role="listitem" key={w} title="A default — add your own to replace it">
              {w}
            </span>
          ))}
        <input
          id={id}
          type="text"
          className="voice-chip-input"
          autoComplete="off"
          spellCheck={false}
          maxLength={60}
          disabled={p.disabled}
          placeholder={p.list.length ? "Add a phrase…" : "Add your own…"}
          aria-invalid={error ? true : undefined}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            } else if (e.key === "Backspace" && !draft && p.list.length) {
              p.onChange(p.list.slice(0, -1));
            }
          }}
        />
      </div>
      {error && (
        <span className="set-field-error" role="alert">
          {error}
        </span>
      )}
      {p.kept && (
        <span className="set-hint voice-words-kept">
          Saved as: {p.kept.length ? p.kept.join(", ") : "the defaults"}
        </span>
      )}
      <span className="set-hint">{p.hint}</span>
    </div>
  );
}

/** The wake word: one text field, the default as its placeholder. */
function WakeRow(p: {
  value: string;
  fallback: string;
  kept?: string;
  disabled: boolean;
  onChange(v: string): void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState("");
  const shown = draft ?? p.value;
  const commit = () => {
    if (draft === null) return;
    const why = wakeError(draft);
    setError(why);
    if (why) return;
    setDraft(null);
    if (normPhrase(draft) !== p.value) p.onChange(normPhrase(draft));
  };
  return (
    <div className="set-row voice-words-row" data-words-kind="wake">
      <label className="set-label" htmlFor="voice-words-wake">
        Wake word
      </label>
      <input
        id="voice-words-wake"
        type="text"
        autoComplete="off"
        spellCheck={false}
        maxLength={60}
        disabled={p.disabled}
        placeholder={wakeName({ ...BUILTIN_VOICE_WORDS, wake: p.fallback })}
        aria-invalid={error ? true : undefined}
        value={shown}
        onChange={(e) => {
          setDraft(e.target.value);
          if (error) setError("");
        }}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      {error && (
        <span className="set-field-error" role="alert">
          {error}
        </span>
      )}
      {p.kept !== undefined && (
        <span className="set-hint voice-words-kept">Saved as: {p.kept || "the default"}</span>
      )}
      <span className="set-hint">
        Said on its own, it opens the mic from standby (Listen for it, above). One or two words; empty is the default.
      </span>
    </div>
  );
}
