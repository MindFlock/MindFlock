/** Settings → Voice → Say it as (`#voice-pronounce`, the sub-block right after
 * "Voice words", under its own `h4.set-subtitle`): the person's own pronunciations — one row per word or phrase
 * ("MindFlock" → "mind flock", "live" → "lyve"), a field pair that adds one
 * (Enter or Add), × removes a row, and Try speaks a respelling through the
 * current server voice (POST /api/voice/speak with `shape: false`, so what
 * you hear is the respelling itself) — the draft's too, before it is saved.
 *
 * Saved through the ordinary settings model as `voice.pronounce` — an
 * ordered list of `{say, as}` (at most 40 rows, 40 characters a side) — so
 * the server normalises it (core.spoken_text.normalize_pronounce) and syncs
 * it across the person's devices; every voice applies it first, before its
 * own rules (lib/spokenText shapeForSpeech), and a caption never shows it.
 * The editor refuses what the server would (pronounceError); adding a word
 * that has a row replaces that row. Inline DOM only: the desktop app has no
 * prompt() or alert(). */

import { useState } from "react";
import { apiRaw } from "../../../api/client";
import type { Json } from "../../../api/types";
import { PRONOUNCE_MAX, PRONOUNCE_MAX_CHARS, normalizePronounce, type PronounceRule } from "../../../lib/spokenText";
import { voiceAudioCtx } from "../../../lib/voiceDesktop";
import type { SettingsModel } from "../useSettings";
import type { VoiceView } from "./Voice";

/** The saved `voice.pronounce`, normalised the server's way. */
export function savedPronounce(raw: unknown): PronounceRule[] {
  return normalizePronounce(raw);
}

/** Why a draft row can't be saved, or "" — the server's limits, said first. */
export function pronounceError(say: string, as: string): string {
  const s = say.replace(/\s+/g, " ").trim();
  const a = as.replace(/\s+/g, " ").trim();
  if (!s) return "Type the word or phrase to change.";
  if (!a) return "Type how to say it.";
  if (s.length > PRONOUNCE_MAX_CHARS || a.length > PRONOUNCE_MAX_CHARS) return `At most ${PRONOUNCE_MAX_CHARS} characters a side.`;
  if (!/^[a-z0-9][a-z0-9 '.+#_-]*$/i.test(s)) return "A word or phrase: letters, digits, spaces, and ' . + # _ -";
  if (/[<>`]/.test(a)) return "The spoken form can't hold < > or `.";
  return "";
}

/** `rows` with `rule` added — or replacing the row that already says its
 * word — at most PRONOUNCE_MAX. */
export function withRule(rows: PronounceRule[], rule: PronounceRule): PronounceRule[] {
  const [clean] = normalizePronounce([rule]);
  if (!clean) return rows;
  const i = rows.findIndex((r) => r.say === clean.say);
  const next = i >= 0 ? rows.map((r, j) => (j === i ? clean : r)) : [...rows, clean];
  return next.slice(0, PRONOUNCE_MAX);
}

export function withoutRule(rows: PronounceRule[], say: string): PronounceRule[] {
  return rows.filter((r) => r.say !== say);
}

/** Speak `text` as written (no shaping) through the server voice, playing
 * the bytes through `ctx` (made inside the click; one made outside a
 * gesture stays muted). The sentence the row shows. */
export async function speakAsWritten(ctx: AudioContext | null, text: string): Promise<string> {
  if (!ctx) return "This browser can't play audio from the server.";
  try {
    const r = await apiRaw("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, shape: false }),
    });
    if (!r.ok) {
      let why = r.statusText || String(r.status);
      try {
        const body = (await r.json()) as { error?: string };
        if (body?.error) why = body.error;
      } catch {
        /* not JSON */
      }
      return "Couldn't speak: " + why.replace(/\.$/, "") + ".";
    }
    const audio = await ctx.decodeAudioData(await r.arrayBuffer());
    const src = ctx.createBufferSource();
    src.buffer = audio;
    src.connect(ctx.destination);
    src.start(0);
    return "";
  } catch (err) {
    return "Couldn't speak: " + String((err as Error)?.message || err).replace(/\.$/, "") + ".";
  }
}

export function PronounceBlock({ model, view }: { model: SettingsModel; view: VoiceView | null }) {
  const rows = savedPronounce(model.get("voice", "pronounce"));
  const locked = view?.configure_allowed === false;
  const [say, setSay] = useState("");
  const [as, setAs] = useState("");
  const [error, setError] = useState("");
  const [said, setSaid] = useState<{ key: string; text: string } | null>(null);
  const [sent, setSent] = useState<{ n: number; doc: Json } | null>(null);
  const full = rows.length >= PRONOUNCE_MAX;

  const save = async (next: PronounceRule[]) => {
    setSent({ n: next.length, doc: model.settings });
    await model.saveField("voice", "pronounce", next as unknown as Json);
  };
  const add = () => {
    const why = pronounceError(say, as);
    setError(why);
    if (why) return;
    void save(withRule(rows, { say, as }));
    setSay("");
    setAs("");
  };
  const tryIt = async (key: string, text: string) => {
    setSaid({ key, text: "Speaking…" });
    const why = await speakAsWritten(voiceAudioCtx(), text);
    setSaid({ key, text: why || "Spoke it." });
  };
  const kept = sent && sent.doc !== model.settings && sent.n !== rows.length ? rows.length : null;

  return (
    <div className="set-section" id="voice-pronounce">
      <h4 className="set-subtitle">Say it as</h4>
      <p className="set-hint" id="voice-pronounce-hint">
        Words the voice gets wrong, respelled the way they should sound — "live" as "lyve", "SQL" as "sequel".
        They apply to everything read aloud, on every device, before the built-in rules; captions keep the
        real spelling. Whole words, any capitalisation.
        {rows.length > 0 && !locked && (
          <>
            {" "}
            <button type="button" className="linklike" id="voice-pronounce-reset" onClick={() => void save([])}>
              Remove all
            </button>
          </>
        )}
      </p>
      {locked && (
        <p className="set-hint" id="voice-pronounce-locked">
          Only the owner of this computer can change them.
        </p>
      )}
      {kept !== null && (
        <p className="set-hint voice-pronounce-kept" role="status">
          The server kept {kept} of them.
        </p>
      )}
      <div className="voice-pronounce-rows" role="list" aria-label="Say it as">
        {rows.map((r) => (
          <div className="voice-pronounce-row" role="listitem" key={r.say} data-pronounce-say={r.say}>
            <span className="voice-pronounce-say">{r.say}</span>
            <span className="voice-pronounce-arrow" aria-hidden="true">
              →
            </span>
            <span className="voice-pronounce-as">{r.as}</span>
            <button
              type="button"
              className="test-btn voice-pronounce-try"
              title="Hear it through the current voice"
              onClick={() => void tryIt(r.say, r.as)}
            >
              Try
            </button>
            <button
              type="button"
              className="voice-chip-x"
              aria-label={"Remove " + r.say}
              title="Remove"
              disabled={locked}
              onClick={() => void save(withoutRule(rows, r.say))}
            >
              ×
            </button>
            {said?.key === r.say && (
              <span className="set-hint voice-pronounce-said" role="status">
                {said.text}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="voice-pronounce-row is-draft">
        <input
          id="voice-pronounce-say"
          type="text"
          autoComplete="off"
          spellCheck={false}
          maxLength={PRONOUNCE_MAX_CHARS + 20}
          disabled={locked || full}
          placeholder="Word or phrase"
          aria-label="Word or phrase"
          aria-invalid={error ? true : undefined}
          value={say}
          onChange={(e) => {
            setSay(e.target.value);
            if (error) setError("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <span className="voice-pronounce-arrow" aria-hidden="true">
          →
        </span>
        <input
          id="voice-pronounce-as"
          type="text"
          autoComplete="off"
          spellCheck={false}
          maxLength={PRONOUNCE_MAX_CHARS + 20}
          disabled={locked || full}
          placeholder="Say it as"
          aria-label="Say it as"
          aria-invalid={error ? true : undefined}
          value={as}
          onChange={(e) => {
            setAs(e.target.value);
            if (error) setError("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <button
          type="button"
          className="test-btn voice-pronounce-try"
          id="voice-pronounce-try-draft"
          title="Hear the respelling through the current voice"
          disabled={!as.trim()}
          onClick={() => void tryIt("", as.trim())}
        >
          Try
        </button>
        <button type="button" className="test-btn" id="voice-pronounce-add" disabled={locked || full} onClick={add}>
          Add
        </button>
        {said?.key === "" && (
          <span className="set-hint voice-pronounce-said" role="status">
            {said.text}
          </span>
        )}
      </div>
      {error && (
        <span className="set-field-error" role="alert">
          {error}
        </span>
      )}
      {full && (
        <span className="set-hint" id="voice-pronounce-full">
          That's the most ({PRONOUNCE_MAX}); remove one to add another.
        </span>
      )}
    </div>
  );
}
