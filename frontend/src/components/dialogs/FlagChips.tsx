/** Launch-flag preset chips (port of initFlagChips, section 25). Toggling a
 * chip adds/removes its flag token sequence in the bound field; presets vary
 * per provider. Shared by the New-session dialog and Settings → Coding CLI. */

interface FlagPreset {
  label: string;
  flag: string;
  group?: string;
}

// Flags verified against installed CLIs (codex 0.142.x, aider 0.86.x, agy
// 1.0.x, claude code) — keep in step with providers/config.py's
// skip_perms_flag. A provider absent here simply shows no chips.
const LAUNCH_FLAG_PRESETS: Record<string, FlagPreset[]> = {
  claude: [
    { label: "Skip permissions", flag: "--dangerously-skip-permissions" },
    { label: "Plan mode", flag: "--permission-mode plan" },
    { label: "Verbose", flag: "--verbose" },
  ],
  codex: [
    { label: "Bypass approvals", flag: "--dangerously-bypass-approvals-and-sandbox" },
    { label: "Web search", flag: "--search" },
  ],
  antigravity: [
    { label: "Skip permissions", flag: "--dangerously-skip-permissions" },
    { label: "Sandbox", flag: "--sandbox" },
  ],
  aider: [
    { label: "Yes to all", flag: "--yes-always" },
    { label: "No auto-commit", flag: "--no-auto-commits" },
  ],
  opencode: [{ label: "Auto-approve", flag: "--auto" }],
};

export function tokenize(s: string): string[] {
  if (!s) return [];
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = re.exec(s)) !== null) out.push(m[1] || m[2] || m[3] || "");
  return out.filter(Boolean);
}

const quote = (t: string) => (/\s/.test(t) ? '"' + t + '"' : t);

function seqIndex(tokens: string[], seq: string[]): number {
  if (!seq.length) return -1;
  for (let i = 0; i + seq.length <= tokens.length; i++) {
    let ok = true;
    for (let j = 0; j < seq.length; j++)
      if (tokens[i + j] !== seq[j]) {
        ok = false;
        break;
      }
    if (ok) return i;
  }
  return -1;
}

export function FlagChips({
  provider,
  value,
  onChange,
}: {
  provider: string;
  value: string;
  onChange(args: string): void;
}) {
  const presets = LAUNCH_FLAG_PRESETS[(provider || "").trim().toLowerCase()] || [];
  if (!presets.length) return <div className="flag-chips" />;
  const tokens = tokenize(value);

  const toggle = (p: FlagPreset) => {
    let next = tokenize(value);
    const seq = tokenize(p.flag);
    const at = seqIndex(next, seq);
    if (at >= 0) {
      next.splice(at, seq.length); // present -> remove (toggle off)
    } else {
      if (p.group) {
        // Mutually exclusive: drop any sibling group flag before adding this.
        for (const other of presets) {
          if (other === p || other.group !== p.group) continue;
          const osq = tokenize(other.flag);
          let oi;
          while ((oi = seqIndex(next, osq)) >= 0) next.splice(oi, osq.length);
        }
      }
      next = next.concat(seq);
    }
    onChange(next.map(quote).join(" "));
  };

  return (
    <div className="flag-chips">
      {presets.map((p) => {
        const on = seqIndex(tokens, tokenize(p.flag)) >= 0;
        return (
          <button
            key={p.flag}
            type="button"
            className={"flag-chip" + (on ? " active" : "")}
            data-flag={p.flag}
            aria-pressed={on}
            onClick={(e) => {
              e.preventDefault();
              toggle(p);
            }}
          >
            {p.label}
          </button>
        );
      })}
    </div>
  );
}
