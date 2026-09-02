/** The body of an extension grid pane. Adopts the host-owned keep-alive
 * container (extensions/host.ts) on mount and detaches WITHOUT disposing on
 * unmount — the terminals-registry idiom — which is what lets typed SQL and
 * dirty grid cells survive grid drags and row reflows. Dispose happens only
 * when the pane is explicitly closed or its extension is deactivated. */

import { useEffect, useRef, useSyncExternalStore } from "react";
import { extPaneView, hostVersion, mountExtPane, subscribeHost } from "./host";

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
        <div className="ext-surface-status ext-surface-error">
          <p>
            {view.label} failed: {view.error || "no reason recorded"}
          </p>
          <p className="muted">See Settings → Extensions for details.</p>
        </div>
      )}
      {/* Adoption target: React renders nothing inside it, ever — the
          extension's keep-alive DOM is appended imperatively. */}
      <div className="ext-surface-mount" ref={mountRef} />
    </div>
  );
}
