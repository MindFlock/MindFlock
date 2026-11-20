/** The body of an extension grid pane. Adopts the host-owned keep-alive
 * container (extensions/host.ts) on mount and detaches WITHOUT disposing on
 * unmount — the terminals-registry idiom — which is what lets typed SQL and
 * dirty grid cells survive grid drags and row reflows. Dispose happens only
 * when the pane is explicitly closed or its extension is deactivated. */

import { useEffect, useRef, useSyncExternalStore } from "react";
import { openSettings } from "../lib/openSettings";
import { extPaneView, hostVersion, mountExtPane, retryExtSurface, subscribeHost } from "./host";

/** A failed extension window, pane or dialog: why, a Try again (the host
 * loads it again in place — a module that failed during a server restart
 * no longer needs a page reload), and a door to Settings → Extensions. */
export function ExtSurfaceError({
  label,
  error,
  onRetry,
}: {
  label: string;
  error?: string;
  onRetry(): void;
}) {
  return (
    <div className="ext-surface-status ext-surface-error">
      <p>
        {label} failed: {error || "no reason recorded"}
      </p>
      <p>
        <button type="button" className="test-btn" onClick={onRetry}>
          Try again
        </button>
      </p>
      <p className="muted">
        See{" "}
        <button type="button" className="linklike" onClick={() => openSettings("extensions")}>
          Settings → Extensions
        </button>{" "}
        for details.
      </p>
    </div>
  );
}

export function ExtPaneBody({ extKey }: { extKey: string }) {
  // Re-render on host changes (activation finishing, renderer errors).
  useSyncExternalStore(subscribeHost, hostVersion);
  const mountRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = mountRef.current;
    if (!el) return;
    return mountExtPane(extKey, el);
  }, [extKey]);

  const view = extPaneView(extKey);
  return (
    <div className="ext-pane-body">
      {view.status === "loading" && (
        <p className="ext-surface-status muted">Loading {view.label}…</p>
      )}
      {view.status === "error" && (
        <ExtSurfaceError label={view.label} error={view.error} onRetry={() => retryExtSurface(extKey, "pane")} />
      )}
      {/* Adoption target: React renders nothing inside it, ever — the
          extension's keep-alive DOM is appended imperatively. */}
      <div className="ext-surface-mount" ref={mountRef} />
    </div>
  );
}
