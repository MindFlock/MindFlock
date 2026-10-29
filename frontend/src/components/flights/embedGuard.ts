/** What a surface embedded in Flights says about leaving it. A standalone
 * dialog owned its Modal's Escape and backdrop; embedded, Flights owns them,
 * so the surface registers here while it is mounted (Schedule's template
 * editor does) and Flights asks before closing or switching tabs. */
export interface EmbeddedGuard {
  /** Escape was pressed: true when the surface spent it (withdrew an editor). */
  escape(): boolean;
  /** Unsaved work that leaving would drop. */
  dirty(): boolean;
}

export const embeddedGuard: { current: EmbeddedGuard | null } = { current: null };
