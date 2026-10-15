/** The New form's Launch flags fold: extra CLI flags for this session. */

import { useRef, type Dispatch, type SetStateAction } from "react";
import { FlagChips, tokenize } from "../FlagChips";
import { SKIP_PERMS_LABEL, SKIP_PERMS_TITLE } from "../../../lib/vocab";

/** The flags that turn the agent's permission prompts off, per CLI. */
const SKIP_PERMS_FLAGS = [
  "--dangerously-skip-permissions",
  "--dangerously-bypass-approvals-and-sandbox",
  "--yes-always",
  "--auto",
];

export function skipsPermissions(args: string): boolean {
  return tokenize(args).some((t) => SKIP_PERMS_FLAGS.includes(t));
}

export function LaunchFold({
  launchOpen,
  setLaunchOpen,
  launchArgs,
  setLaunchArgs,
  program,
}: {
  launchOpen: boolean;
  setLaunchOpen: Dispatch<SetStateAction<boolean>>;
  launchArgs: string;
  setLaunchArgs: Dispatch<SetStateAction<string>>;
  program: string;
}) {
  const launchRef = useRef<HTMLDetailsElement | null>(null);
  return (
    <details
      id="new-launch-advanced"
      className="nf-advanced"
      ref={launchRef}
      open={launchOpen}
      onToggle={(e) => {
        const open = (e.target as HTMLDetailsElement).open;
        setLaunchOpen(open);
        // The fold is the last thing in the scroll region, so its revealed
        // fields open below the fold line — scroll them into view so it's
        // obvious the click did something and where to look. Only on a
        // real click: open-by-default fires no toggle, and scrolling the
        // body on arrival would bury the Name field.
        if (open)
          launchRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
      }}
    >
      <summary>
        Launch flags <span className="muted">— extra CLI flags for this session</span>
      </summary>
      <div className="nf-advanced-body">
        <label>
          <span>
            Flags{" "}
            <span className="muted">
              — appended after the agent's saved defaults
            </span>
          </span>
          <input
            type="text"
            id="new-launch-args"
            autoComplete="off"
            placeholder="e.g. --verbose"
            value={launchArgs}
            onChange={(e) => setLaunchArgs(e.target.value)}
          />
        </label>
        <FlagChips provider={program} value={launchArgs} onChange={setLaunchArgs} />
        {skipsPermissions(launchArgs) && (
          <p className="nf-git-nudge nf-skip-perms" id="new-skip-perms-warn" role="status">
            {SKIP_PERMS_LABEL} — {SKIP_PERMS_TITLE.charAt(0).toLowerCase() + SKIP_PERMS_TITLE.slice(1)}.
          </p>
        )}
      </div>
    </details>
  );
}
