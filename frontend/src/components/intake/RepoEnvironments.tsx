/** A Verify repo card's ENVIRONMENTS: the places a Verify stop can check
 * this repo's behaviors on ("Where" on the stop names one). Each row is a
 * name, how to reach it, the branch it deploys from and its deploy time.
 * The list is ONE settings field (`verify_repo_settings[slug].environments`),
 * saved whole as you type once every name is valid (lib/verifyStop.ts holds
 * the server's rules); the repo's live fields above are the implicit "live". */

import { useEffect, useRef, useState } from "react";
import { ENV_NAME_MAX, ENV_REACH_MAX, envRowErrors, envsToSave, type VerifyEnv } from "../../lib/verifyStop";
import { useAutosave } from "./autosave";

const blank = (): VerifyEnv => ({ name: "", reach: "", branch: "", deploy_minutes: 0 });

export function RepoEnvironments({ value, onSave }: { value: readonly VerifyEnv[]; onSave(list: VerifyEnv[]): void }) {
  const [rows, setRows] = useState<VerifyEnv[]>(() => value.map((e) => ({ ...e })));
  const savedKey = JSON.stringify(value);
  const sent = useRef(savedKey);
  const autosave = useAutosave<VerifyEnv[]>((list) => {
    const clean = envsToSave(list);
    if (!clean) return;
    const key = JSON.stringify(clean);
    if (key === sent.current) return;
    sent.current = key;
    onSave(clean);
  });
  // A store change that isn't our own echo (another window, a failed save
  // put back) re-seeds the rows — never over typing that hasn't been sent.
  useEffect(() => {
    if (savedKey !== sent.current && !autosave.isPending()) {
      sent.current = savedKey;
      setRows(value.map((e) => ({ ...e })));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savedKey]);
  const errors = envRowErrors(rows);
  const change = (next: VerifyEnv[]) => {
    setRows(next);
    autosave.schedule(next);
  };
  const edit = (i: number, patch: Partial<VerifyEnv>) => change(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <section className="vf-envs" aria-label="Environments">
      <div className="vf-envs-head">
        <span className="set-label">Environments</span>
        <span className="set-hint">More places a Verify stop can check on — its “Where” lists them by name.</span>
      </div>
      {rows.map((r, i) => (
        <div key={i} className="vf-env" data-env={r.name || undefined}>
          <div className="ik-grid ik-grid-3">
            <label className="set-row">
              <span className="set-label">Name</span>
              <input
                type="text"
                autoComplete="off"
                spellCheck={false}
                maxLength={ENV_NAME_MAX}
                data-env-field="name"
                placeholder="e.g. sandbox"
                value={r.name}
                aria-invalid={errors[i] ? true : undefined}
                onChange={(e) => edit(i, { name: e.target.value.toLowerCase() })}
                onBlur={() => autosave.flush()}
              />
              {errors[i] ? <span className="set-hint vf-env-error">{errors[i]}</span> : null}
            </label>
            <label className="set-row">
              <span className="set-label">Deploys from branch</span>
              <input
                type="text"
                autoComplete="off"
                spellCheck={false}
                data-env-field="branch"
                placeholder="blank = none"
                value={r.branch}
                onChange={(e) => edit(i, { branch: e.target.value })}
                onBlur={() => autosave.flush()}
              />
            </label>
            <label className="set-row">
              <span className="set-label">Deploy time (minutes)</span>
              <input
                type="number"
                className="num-sm"
                min={0}
                max={1440}
                step={1}
                data-env-field="deploy_minutes"
                value={String(r.deploy_minutes)}
                onChange={(e) => edit(i, { deploy_minutes: Math.min(1440, Math.max(0, Math.trunc(Number(e.target.value) || 0))) })}
                onBlur={() => autosave.flush()}
              />
            </label>
          </div>
          <label className="set-row">
            <span className="set-label">How to reach it</span>
            <textarea
              rows={2}
              spellCheck={false}
              maxLength={ENV_REACH_MAX}
              data-env-field="reach"
              placeholder="URL, Slack channel, log search… or 'use what you know about this repo'"
              value={r.reach}
              onChange={(e) => edit(i, { reach: e.target.value })}
              onBlur={() => autosave.flush()}
            />
          </label>
          <div className="vf-env-acts">
            <button
              type="button"
              className="test-btn"
              aria-label={"Remove the environment " + (r.name || "without a name")}
              onClick={() => {
                const next = rows.filter((_, j) => j !== i);
                change(next);
                autosave.flush();
              }}
            >
              Remove
            </button>
          </div>
        </div>
      ))}
      <div>
        <button type="button" className="test-btn" onClick={() => setRows([...rows, blank()])}>
          + Add an environment
        </button>
      </div>
    </section>
  );
}
