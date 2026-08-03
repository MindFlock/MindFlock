/** Settings → Local model: run sessions against a model served on this machine.
 *
 * Removes the paid-subscription barrier and makes the privacy story absolute —
 * with this on, no prompt, diff or file leaves the box. The screen's job is to
 * make that verifiable rather than aspirational, so Test does three things at
 * once: confirms the server answers, lists the models it actually serves (so the
 * model field becomes a dropdown instead of "type the exact tag"), and names
 * which installed CLIs can be pointed at it. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

interface ProbeResult {
  ok?: boolean;
  runtime?: string;
  base_url?: string;
  models?: string[];
  error?: string;
  supported_agents?: string[];
  default_base_urls?: Record<string, string>;
}

const RUNTIMES: Array<{ id: string; label: string; hint: string }> = [
  { id: "ollama", label: "Ollama", hint: "ollama serve — the default at :11434" },
  { id: "lmstudio", label: "LM Studio", hint: "Developer → Start Server, at :1234" },
  {
    id: "custom",
    label: "Other (OpenAI-compatible)",
    hint: "llama.cpp, vLLM, a LiteLLM proxy — anything serving /v1",
  },
];

export function LocalModel(_: ScreenProps) {
  const s = useSettings();
  const group = (s.settings.local_model || {}) as Record<string, unknown>;
  const enabled = group.enabled === true;
  const runtime = String(group.runtime || "ollama");
  const baseUrl = String(group.base_url || "");
  const model = String(group.model || "");

  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [testing, setTesting] = useState(false);

  // Probe once on open when the feature is already on, so a server that died
  // since the last session shows as down without the user pressing anything.
  useEffect(() => {
    if (!enabled) return;
    (async () => {
      try {
        setProbe(await api<ProbeResult>("/api/settings/test/local-model", { json: {} }));
      } catch {
        /* the Test button reports it properly */
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runTest = async (over?: Record<string, string>) => {
    setTesting(true);
    try {
      const r = await api<ProbeResult>("/api/settings/test/local-model", {
        json: { runtime, base_url: baseUrl, model, ...(over || {}) },
      });
      setProbe(r);
      toast(r?.ok ? "Local model server is up" : "Local model server unreachable");
    } catch (err) {
      setProbe({ ok: false, error: (err as Error).message });
    } finally {
      setTesting(false);
    }
  };

  const placeholder = probe?.default_base_urls?.[runtime] || "";
  const models = probe?.models || [];
  const supported = probe?.supported_agents || [];

  return (
    <>
      <h3 className="set-section-title">Local model</h3>
      <p className="set-hint set-block-hint">
        Point your agent CLI at a model running on this machine. No subscription and no
        API key — and nothing you type, no diff and no file ever leaves the box. Works
        with <strong>codex</strong>, <strong>aider</strong> and <strong>goose</strong>,
        each of which has native local-model support; Claude Code talks only to the
        Anthropic API, so a session on it keeps using that.
      </p>

      <div className="set-row set-switch-row" id="lm-enabled-row">
        <span className="set-label">Use a local model</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="lm-enabled"
            checked={enabled}
            onChange={(e) => s.saveField("local_model", "enabled", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>

      <label className="set-row">
        <span className="set-label">Server</span>
        <select
          id="lm-runtime"
          value={runtime}
          onChange={(e) => s.saveField("local_model", "runtime", e.target.value)}
        >
          {RUNTIMES.map((r) => (
            <option key={r.id} value={r.id}>
              {r.label}
            </option>
          ))}
        </select>
        <span className="set-hint">
          {RUNTIMES.find((r) => r.id === runtime)?.hint || ""}
        </span>
      </label>

      <label className="set-row">
        <span className="set-label">Base URL</span>
        <input
          type="text"
          id="lm-base-url"
          autoComplete="off"
          placeholder={placeholder || "leave blank for this server's default"}
          defaultValue={baseUrl}
          key={runtime /* re-seed the field when the runtime's default changes */}
          onBlur={(e) => {
            if (e.target.value !== baseUrl)
              s.saveField("local_model", "base_url", e.target.value);
          }}
        />
        <span className="set-hint">
          Blank uses the server's documented default. Point it at another machine on
          your LAN to share one GPU box across a flock.
        </span>
      </label>

      <label className="set-row">
        <span className="set-label">Model</span>
        {models.length ? (
          <select
            id="lm-model"
            value={models.includes(model) ? model : ""}
            onChange={(e) => s.saveField("local_model", "model", e.target.value)}
          >
            <option value="">Pick a model…</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        ) : (
          <input
            type="text"
            id="lm-model"
            autoComplete="off"
            placeholder="qwen2.5-coder:7b"
            defaultValue={model}
            onBlur={(e) => {
              if (e.target.value !== model)
                s.saveField("local_model", "model", e.target.value);
            }}
          />
        )}
        <span className="set-hint">
          {models.length
            ? "Served by your local server right now."
            : "Exactly as your server names it — press Test to list what it has."}
        </span>
      </label>

      <div className="set-row">
        <span className="test-row">
          <button
            type="button"
            id="lm-test"
            className="test-btn"
            disabled={testing}
            onClick={() => runTest()}
          >
            Test connection
          </button>
          <span
            className={"test-result" + (probe ? (probe.ok ? " ok" : " bad") : "")}
            id="lm-test-result"
          >
            {testing
              ? "testing…"
              : probe
                ? probe.ok
                  ? `✓ up at ${probe.base_url} · ${models.length} model(s)`
                  : "✗ " + (probe.error || "unreachable")
                : ""}
          </span>
        </span>
        <span className="set-hint">
          {supported.length
            ? `Installed CLIs that can use it: ${supported.join(", ")}. ` +
              "Set a session's agent (or a ticketing source's) to one of those."
            : "Test also reports which of your installed CLIs can be pointed at it."}
        </span>
      </div>
    </>
  );
}
