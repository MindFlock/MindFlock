/** Settings → Extensions (Addon API v3): one card per extension — built-in or
 * discovered under ~/.mindflock/extensions — with what it contributes (bar,
 * commands), an enable/disable switch, and the last activation error the
 * runtime host recorded for it.
 *
 * The switch writes `extensions.disabled` in settings.json and then refetches
 * the manifests; it does NOT talk to the host directly. App.tsx watches the
 * manifest query and feeds the host the enabled set, which is the one path a
 * disable takes whether it was made here, in another tab, or by hand in the
 * settings file — so there is exactly one place that can tear an extension
 * down, and it cannot disagree with what the server says is enabled. */

import { useSyncExternalStore } from "react";
import { extActivationError, hostVersion, subscribeHost } from "../../../extensions/host";
import type { ExtensionInfo } from "../../../extensions/types";
import { refreshExtensions, useExtensions } from "../../../state/queries";
import { useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

/** The stored disabled list, or undefined when the settings document has no
 * such group yet (an older server, or the doc still loading) — the caller then
 * falls back to the manifest's own `enabled` flag. */
function readDisabled(raw: unknown): string[] | undefined {
  return Array.isArray(raw) ? raw.map((x) => String(x)) : undefined;
}

export function Extensions(_: ScreenProps) {
  const s = useSettings();
  const { data: extensions, isLoading, isError } = useExtensions();
  // Activation errors land after the fact (a module loads on first click), so
  // the rows re-render on host changes rather than reading a stale snapshot.
  useSyncExternalStore(subscribeHost, hostVersion);
  const disabled = readDisabled(s.get("extensions", "disabled"));

  const setEnabled = async (ext: ExtensionInfo, on: boolean) => {
    const cur = disabled ?? (extensions || []).filter((e) => !e.enabled).map((e) => e.id);
    const next = on ? cur.filter((id) => id !== ext.id) : cur.includes(ext.id) ? cur : [...cur, ext.id];
    await s.saveGroup(
      "extensions",
      { disabled: next },
      (on ? "Enabled " : "Disabled ") + ext.label
    );
    // The manifest's `enabled` flips server-side; refetching is what makes the
    // sidebar bar, the palette and the host (via App.tsx) see it.
    void refreshExtensions();
  };

  return (
    <>
      <h3 className="set-section-title">Extensions</h3>
      <p className="set-hint">
        Each extension adds one sidebar bar whose buttons run its commands, and opens its own
        dialogs and grid windows. Its bar drags and hides like the built-in ones (footer
        Customize); its commands are in the command palette.
      </p>

      {isLoading && <p className="set-hint">Loading…</p>}
      {isError && <p className="set-hint">Could not read the extension list from the server.</p>}
      {extensions && !extensions.length && <p className="set-hint">No extensions installed.</p>}

      {extensions && extensions.length > 0 && (
        <div className="prov-conn-list" id="ext-list">
          {extensions.map((ext) => {
            const on = disabled ? !disabled.includes(ext.id) : ext.enabled;
            const err = extActivationError(ext.id);
            const spec = ext.extension;
            const n = spec.commands.length;
            return (
              <div className="prov-conn" key={ext.id} data-extension={ext.id}>
                <div className="prov-conn-head ext-row-head">
                  <span className="prov-name">{ext.label}</span>
                  <span className="prov-badge">
                    {ext.origin === "user" ? "~/.mindflock/extensions" : "built-in"}
                  </span>
                  {!on && <span className="prov-badge">disabled</span>}
                  {/* label wraps only the switch, so clicking the row text no longer flips it */}
                  <label className="ca-switch" title={on ? "Disable this extension" : "Enable this extension"}>
                    <input type="checkbox" checked={on} onChange={(e) => void setEnabled(ext, e.target.checked)} />
                    <span className="ca-slider" />
                  </label>
                </div>
                <p className="set-hint">
                  <code>{ext.id}</code> · bar "{spec.bar_label || ext.label}" · {n}{" "}
                  {n === 1 ? "command" : "commands"}
                </p>
                {err && (
                  <p className="set-hint ext-row-error">
                    Failed to activate: {err}. Turn it off and on again to retry after fixing
                    the module.
                  </p>
                )}
                {ext.origin === "user" && (
                  <p className="set-hint">
                    Disabling removes its bar, commands and windows here; its backend (routes,
                    event subscriptions) stays loaded until MindFlock restarts.
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}

      <h3 className="set-section-title">Create an extension</h3>
      <p className="set-hint">
        Make a folder <code>~/.mindflock/extensions/&lt;id&gt;/</code> containing an{" "}
        <code>extension.py</code> that exposes <code>build(ctx)</code> and returns an Addon whose{" "}
        <code>extension()</code> declares the bar, commands and surfaces. Put the ES module and
        its <code>style.css</code> in an optional <code>frontend/</code> folder next to it (served
        at <code>/extensions/&lt;id&gt;/</code>). Extensions are discovered once at startup:
        restart MindFlock to load a new one. The manifest and API reference is in{" "}
        <code>docs/extensions.md</code>.
      </p>
    </>
  );
}
