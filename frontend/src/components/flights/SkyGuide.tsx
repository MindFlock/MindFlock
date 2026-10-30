/** The Sky guide (round 12): the long-form tutorial behind the Sky's "?" —
 * the coach tour (lib/tours/sky.ts) points at things; this explains them. A
 * wide `Modal` (`#sky-guide`): a list of sections on the left, the chosen
 * section on the right, each with a small inline sketch drawn in
 * `currentColor` and the theme tokens (SkyGuide.css), so both themes work.
 *
 * Its open flag lives in a tiny store of its own (`useSkyGuide`), not the app
 * store, so anything can open it — the sky's ? (onboarding/TourButton), the
 * palette's "Learn the Sky" — and the host only mounts `<SkyGuide />` (in
 * SkyView). Given `open` it is controlled instead (the tests, a future host).
 * No window.prompt/alert/confirm: the desktop app has none. */

import { useState, type KeyboardEvent, type ReactNode } from "react";
import { create } from "zustand";
import { replayTour } from "../onboarding/replayTour";
import { Modal } from "../Modal";
import "./SkyGuide.css";

export const SKY_GUIDE_SECTIONS = [
  { id: "what", title: "What the sky is" },
  { id: "lines", title: "Lines and boxes" },
  { id: "stops", title: "Stops and saved stops" },
  { id: "bird", title: "The bird" },
  { id: "halt", title: "Halt points and Remove" },
  { id: "grounded", title: "Grounded stops" },
  { id: "select", title: "Selection" },
  { id: "keys", title: "Tools and the keyboard" },
  { id: "intake", title: "The intake lane" },
  { id: "saved", title: "Where things are saved" },
] as const;

export type SkyGuideSection = (typeof SKY_GUIDE_SECTIONS)[number]["id"];

function sectionOf(id: string | undefined): SkyGuideSection {
  return SKY_GUIDE_SECTIONS.find((s) => s.id === id)?.id ?? SKY_GUIDE_SECTIONS[0].id;
}

interface SkyGuideState {
  /** The section showing, or null while the guide is shut. */
  section: SkyGuideSection | null;
  open(section?: string): void;
  close(): void;
}

export const useSkyGuide = create<SkyGuideState>((set) => ({
  section: null,
  open: (section) => set({ section: sectionOf(section) }),
  close: () => set({ section: null }),
}));

export const openSkyGuide = (section?: string) => useSkyGuide.getState().open(section);
export const closeSkyGuide = () => useSkyGuide.getState().close();

/** The keyboard table (Tools and the keyboard), exported for the test. */
export const SKY_GUIDE_KEYS: readonly (readonly [string, string])[] = [
  ["V", "Drag tool: lines move by their bars, a drag on empty sky selects"],
  ["H", "Click tool: any drag pans the sky, a click selects"],
  ["Space (held)", "Pan with the mouse, in either tool"],
  ["0", "Fit every line, titles still readable"],
  ["1", "Fit the focused line to the window"],
  ["+ / −", "Zoom in / out (Ctrl+wheel too)"],
  ["T", "Tidy: every line back in its place"],
  ["Arrows", "Pan (Shift: further); on a selected stop ← → move it"],
  ["Delete", "Remove the selected box"],
  ["Esc", "Clear the selection, fold an open session, cancel a pick"],
  ["Ctrl+A", "Select every line"],
  ["Enter", "Open the focused line's session; on a stop, its dialog"],
  ["Alt+Y", "Switch between the grid and the sky"],
  ["Ctrl/Alt+N", "A new-session block (double-click empty sky does too)"],
];

// --- The sketches ----------------------------------------------------------
// Small parts so each sketch stays short: a box, a line's bar, the bird.

function Box({ x, y, w = 44, h = 26, cls = "" }: { x: number; y: number; w?: number; h?: number; cls?: string }) {
  return <rect className={"sg-box " + cls} x={x} y={y} width={w} height={h} rx={5} />;
}

function Bar({ x, y, w }: { x: number; y: number; w: number }) {
  return <rect className="sg-bar" x={x} y={y} width={w} height={8} rx={3} />;
}

function BirdMark({ x, y }: { x: number; y: number }) {
  return <path className="sg-bird" d={`M${x - 8} ${y} Q${x - 4} ${y - 6} ${x} ${y} Q${x + 4} ${y - 6} ${x + 8} ${y}`} />;
}

/** A line: its bar, then a row of boxes from `x`; the first is the session. */
function Line({ x, y, n = 4, cls = [] as string[] }: { x: number; y: number; n?: number; cls?: string[] }) {
  return (
    <g>
      <Bar x={x} y={y} w={n * 54 - 10} />
      {Array.from({ length: n }, (_, k) => (
        <g key={k}>
          {k > 0 && <line className="sg-link" x1={x + k * 54 - 10} y1={y + 27} x2={x + k * 54} y2={y + 27} />}
          <Box x={x + k * 54} y={y + 14} cls={k === 0 ? "sg-session" : cls[k] || ""} />
        </g>
      ))}
    </g>
  );
}

function Sketch({ label, children, h = 110 }: { label: string; children: ReactNode; h?: number }) {
  return (
    <svg className="sg-sketch" viewBox={`0 0 320 ${h}`} role="img" aria-label={label}>
      {children}
    </svg>
  );
}

const SKETCHES: Record<SkyGuideSection, ReactNode> = {
  what: (
    <Sketch label="Panes on the left, the same sessions as lines on the right">
      <rect className="sg-pane" x={8} y={14} width={60} height={38} rx={4} />
      <rect className="sg-pane" x={74} y={14} width={60} height={38} rx={4} />
      <rect className="sg-pane" x={8} y={58} width={126} height={38} rx={4} />
      <path className="sg-arrow" d="M144 55 H170 M164 49 L170 55 L164 61" />
      <Line x={180} y={12} n={2} />
      <Line x={180} y={58} n={2} />
    </Sketch>
  ),
  lines: (
    <Sketch label="A line: its bar on top, the session box, then its stops, and a + to tack on">
      <Line x={12} y={30} n={4} />
      <text className="sg-text" x={12} y={22}>bar · drag me · ⋯</text>
      <text className="sg-text" x={12} y={92}>session</text>
      <text className="sg-text" x={70} y={92}>stops →</text>
      <rect className="sg-box sg-dash" x={228} y={44} width={26} height={26} rx={5} />
      <text className="sg-text" x={236} y={62}>+</text>
    </Sketch>
  ),
  stops: (
    <Sketch label="A saved stop: one box holding three sub-steps, and its chip on the shelf with a dot per step">
      <Box x={12} y={24} w={150} h={56} cls="sg-group" />
      <text className="sg-text" x={20} y={38}>Ticket to merge · 2 of 3</text>
      <Box x={20} y={46} w={40} h={24} cls="sg-done" />
      <Box x={66} y={46} w={40} h={24} cls="sg-here" />
      <Box x={112} y={46} w={40} h={24} />
      <rect className="sg-chip" x={196} y={40} width={112} height={26} rx={13} />
      <text className="sg-text" x={208} y={57}>Ticket to merge</text>
      <circle className="sg-dot" cx={290} cy={53} r={2} />
      <circle className="sg-dot" cx={296} cy={53} r={2} />
      <circle className="sg-dot" cx={302} cy={53} r={2} />
    </Sketch>
  ),
  bird: (
    <Sketch label="A line of boxes with the bird on the third; a dashed arrow drags it back to the second">
      <Line x={20} y={40} n={5} cls={["", "sg-done", "sg-here"]} />
      <BirdMark x={150} y={46} />
      <path className="sg-arrow sg-dash" d="M146 36 Q120 14 96 36 M98 28 L96 36 L104 36" />
      <text className="sg-text" x={20} y={100}>click: halt / carry on · drag: jump</text>
    </Sketch>
  ),
  halt: (
    <Sketch label="The bird on a stop, and a halt point two stops on wearing a pause badge">
      <Line x={20} y={34} n={5} cls={["", "sg-done", "sg-here"]} />
      <BirdMark x={150} y={40} />
      <rect className="sg-halt" x={236} y={44} width={14} height={14} rx={3} />
      <path className="sg-halt-ink" d="M241 48 V54 M245 48 V54" />
      <text className="sg-text" x={20} y={96}>⏹ halt now — here · ⏸ halt here — when it arrives</text>
    </Sketch>
  ),
  grounded: (
    <Sketch label="A grounded line: a dashed No session yet head, its stops, and the Session chip dropped on it">
      <Bar x={20} y={30} w={206} />
      <rect className="sg-box sg-dash" x={20} y={44} width={64} height={26} rx={5} />
      <text className="sg-text" x={26} y={61}>no session</text>
      <Box x={94} y={44} />
      <Box x={148} y={44} />
      <rect className="sg-chip" x={60} y={84} width={70} height={20} rx={10} />
      <text className="sg-text" x={70} y={98}>Session</text>
      <path className="sg-arrow" d="M70 84 L56 74 M56 80 L56 74 L62 74" />
    </Sketch>
  ),
  select: (
    <Sketch label="A dashed marquee drawn over the boxes of two lines">
      <Line x={20} y={10} n={4} />
      <Line x={20} y={58} n={4} />
      <rect className="sg-marquee" x={64} y={18} width={118} height={84} rx={2} />
      <path className="sg-arrow" d="M182 102 l10 6 M182 102 l3 -10" />
    </Sketch>
  ),
  keys: (
    <Sketch label="The tool toggle: Drag (V) and Click (H)" h={80}>
      <rect className="sg-toggle" x={60} y={20} width={200} height={34} rx={17} />
      <rect className="sg-toggle-on" x={63} y={23} width={97} height={28} rx={14} />
      <text className="sg-text sg-strong" x={84} y={42}>✥ Drag · V</text>
      <text className="sg-text" x={180} y={42}>☝ Click · H</text>
      <text className="sg-text" x={98} y={74}>hold Space: pan in either</text>
    </Sketch>
  ),
  intake: (
    <Sketch label="The intake lane folded to a row of source blocks, and expanded to show each waiting item with Fly">
      <Bar x={12} y={12} w={120} />
      <Box x={12} y={26} w={56} cls="sg-group" />
      <Box x={76} y={26} w={56} cls="sg-group" />
      <text className="sg-text" x={12} y={72}>folded</text>
      <Bar x={168} y={12} w={140} />
      <Box x={168} y={26} w={140} h={70} cls="sg-group" />
      <rect className="sg-row" x={176} y={36} width={124} height={14} rx={3} />
      <rect className="sg-row" x={176} y={56} width={124} height={14} rx={3} />
      <rect className="sg-fly" x={276} y={38} width={20} height={10} rx={3} />
      <rect className="sg-fly" x={276} y={58} width={20} height={10} rx={3} />
      <text className="sg-text" x={168} y={108}>expanded ▾</text>
    </Sketch>
  ),
  saved: (
    <Sketch label="Three places: this browser for the layout, every device for saved stops, this machine for flights">
      <rect className="sg-pane" x={16} y={24} width={70} height={46} rx={4} />
      <text className="sg-text" x={22} y={88}>layout: here</text>
      <path className="sg-cloud" d="M136 64 a14 14 0 0 1 4 -27 a18 18 0 0 1 34 -2 a13 13 0 0 1 6 29 Z" />
      <text className="sg-text" x={118} y={88}>saved stops: synced</text>
      <rect className="sg-pane" x={238} y={30} width={60} height={34} rx={4} />
      <line className="sg-link" x1={238} y1={52} x2={298} y2={52} />
      <text className="sg-text" x={226} y={88}>flights: this machine</text>
    </Sketch>
  ),
};

// --- The words -------------------------------------------------------------

function Keys() {
  return (
    <table className="sky-guide-keys">
      <thead>
        <tr>
          <th scope="col">Key</th>
          <th scope="col">What it does</th>
        </tr>
      </thead>
      <tbody>
        {SKY_GUIDE_KEYS.map(([k, what]) => (
          <tr key={k}>
            <td>
              <kbd>{k}</kbd>
            </td>
            <td>{what}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const WORDS: Record<SkyGuideSection, ReactNode> = {
  what: (
    <>
      <p>The sky is the main area's second view. Every session is a line — the same sessions as the grid, with the same selection and the same open tabs; only the drawing changes.</p>
      <p>Switch with <b>Grid · Sky</b> on the top bar, or <kbd>Alt+Y</kbd>. The chips on the sky's toolbar choose which lines show, and <b>Find a line…</b> jumps to one by name.</p>
    </>
  ),
  lines: (
    <>
      <p>A line is one session. Its <b>bar</b> carries the name and a <b>⋯</b> menu: open the session here, open it in the grid, show only this line, tack on a stop, close the session.</p>
      <p>Drag a line by its bar to put it anywhere — it stays there. <b>Tidy</b> (<kbd>T</kbd>) puts every line back in its place; the ⋯'s <b>Tidy this line</b> does one.</p>
      <p>The boxes run left to right: the session's own box (it opens the real terminal in a card on the line), its workers, then its flight's stops in order. The <b>+</b> at the end tacks on a stop, the + between two boxes puts one in there, and a box's corner handle resizes every box on the line.</p>
    </>
  ),
  stops: (
    <>
      <p>A <b>stop</b> is one block of work: <b>Agent</b> gives the session's agent a prompt and waits for its turn to settle, <b>Perch</b> waits until you press Done, <b>Ship</b> commits, pushes, opens or merges the PR, <b>Verify</b> works through the branch's checklist, <b>Wait</b> waits some minutes or until a time. <b>All blocks…</b> on the shelf lists the rest, each with what it does.</p>
      <p>A <b>saved stop</b> is a named series of stops — <i>Ticket to merge</i>, say — that you drag on as one box. It flies its sub-steps one by one and reads <i>2 of 3</i> while it does. Double-click it to see and change each sub-step.</p>
      <p>To change a saved stop, drag it onto the sky, edit its boxes there, then <b>Save to the shelf</b>. Right-click its chip on the shelf to rename, reset or delete it.</p>
    </>
  ),
  bird: (
    <>
      <p>The bird stands on the stop the flight is at: bobbing while the session works, gold when it is perched on you.</p>
      <p><b>Click</b> the bird to halt the flight where it is; click it again on a paused flight to carry on.</p>
      <p><b>Drag</b> the bird onto another stop of the same flight to jump there — the stops you can land on light up, and dropping it anywhere else puts it back. Jumping back re-arms the halt points after the stop you land on.</p>
    </>
  ),
  halt: (
    <>
      <p><b>⏹ Halt now</b> (on the current stop, or a click on the bird) pauses the flight where it is. <b>⏸ Halt here</b> on a stop ahead makes it a halt point: the flight pauses when it reaches that stop, before doing it, and the stop wears a pause badge. <b>▶ Carry on</b> goes on.</p>
      <p><b>Remove</b> (🗑, the menu, or <kbd>Delete</kbd>) takes a stop off at once, with <b>Undo</b> in the toast. Removing the stop that is flying stops its work and pauses the flight right before the next stop; Carry on goes into it.</p>
    </>
  ),
  grounded: (
    <>
      <p>Drop a block or a saved stop on empty sky and it waits there as a <b>grounded</b> line: a dashed <i>No session yet</i> head, then its stops. Nothing runs until it has a session.</p>
      <p>Give it one by dropping the shelf's <b>Session</b> chip on it (a new session is made and the stops take off), or with its ⋯ → <b>Fly on a session…</b> and a line that has no flight. The same ⋯ offers <b>Save to the shelf</b> and <b>Remove</b>.</p>
    </>
  ),
  select: (
    <>
      <p>Drag across empty sky to draw a box — everything it touches is selected, lines and stop boxes both (in the Click tool, Shift+drag). <kbd>Ctrl+A</kbd> selects every line; <kbd>Esc</kbd> clears.</p>
      <p>The selection bar then offers: drag any selected line's bar and they all move; <b>Remove…</b> (it asks first: a session line closes its session, a grounded line goes); and for stops of one line, <b>Group into a stop</b> and <b>Save to the shelf</b>.</p>
    </>
  ),
  keys: (
    <>
      <p>The toolbar's toggle picks how a drag behaves. <b>✥ Drag</b> (<kbd>V</kbd>): lines move by their bars and a drag on empty sky selects. <b>☝ Click</b> (<kbd>H</kbd>): nothing moves by dragging — any drag pans, a click focuses a line or selects a box. Hold <kbd>Space</kbd> to pan in either; the wheel and the middle button pan too.</p>
      <Keys />
    </>
  ),
  intake: (
    <>
      <p>The intake lane, top left, holds what is waiting to fly: one block per source — tickets, pull requests, issues — with the items in it.</p>
      <p>Folded it is a row of blocks; expand it (▸ on its head, or double-click the head) to see every item with its <b>Fly</b> button. Fly starts a session on that item, and its line appears in the sky.</p>
      <p>Drag the lane by its head like a line; Tidy puts it back.</p>
    </>
  ),
  saved: (
    <>
      <p><b>The layout</b> — where each line sits, its box sizes, the intake lane's spot, the tool — is kept per device, in this browser.</p>
      <p><b>Saved stops</b> live on the server and are synced across your devices, like templates.</p>
      <p><b>Flights</b> — the stops a session is flying and where the bird is — stay on the machine that runs them, never synced.</p>
    </>
  ),
};

export interface SkyGuideProps {
  open: boolean;
  onClose(): void;
  section?: string;
  /** Default true; the tests render it in place. */
  portal?: boolean;
}

/** The guide. With no props it renders itself from `useSkyGuide`. */
export function SkyGuide(p: Partial<SkyGuideProps>) {
  const stored = useSkyGuide((s) => s.section);
  const controlled = p.open !== undefined;
  const open = controlled ? !!p.open : stored !== null;
  const start = controlled ? p.section : (stored ?? undefined);
  const onClose = controlled ? (p.onClose ?? closeSkyGuide) : closeSkyGuide;
  if (!open) return null;
  // Keyed by the asked-for section: asking again while open follows it.
  return <SkyGuideBody key={start ?? ""} start={sectionOf(start)} onClose={onClose} portal={p.portal !== false} />;
}

function SkyGuideBody({ start, onClose, portal }: { start: SkyGuideSection; onClose(): void; portal: boolean }) {
  const [at, setAt] = useState<SkyGuideSection>(start);
  const title = SKY_GUIDE_SECTIONS.find((s) => s.id === at)!.title;
  const replay = () => {
    onClose();
    replayTour("sky");
  };
  const k = SKY_GUIDE_SECTIONS.findIndex((s) => s.id === at);
  const onNavKey = (e: KeyboardEvent<HTMLElement>) => {
    const d = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = SKY_GUIDE_SECTIONS[(k + d + SKY_GUIDE_SECTIONS.length) % SKY_GUIDE_SECTIONS.length];
    setAt(next.id);
    (e.currentTarget.querySelector(`[data-section="${next.id}"]`) as HTMLElement | null)?.focus();
  };
  return (
    <Modal id="sky-guide" className="sky-guide-modal" onClose={onClose} portal={portal} role="dialog" aria-modal="true" aria-labelledby="sky-guide-title">
      <div className="sky-guide">
        <header className="sky-guide-head">
          <h2 id="sky-guide-title">Learn the Sky</h2>
          <button type="button" className="test-btn" onClick={replay}>
            Replay the tour
          </button>
          <button type="button" className="fl-icon-btn sky-guide-x" aria-label="Close the guide" title="Close (Esc)" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="sky-guide-body">
          <nav className="sky-guide-nav" aria-label="Sections" onKeyDown={onNavKey}>
            {SKY_GUIDE_SECTIONS.map((s, n) => (
              <button
                key={s.id}
                type="button"
                data-section={s.id}
                className={"fl-stopmenu-item sky-guide-nav-item" + (s.id === at ? " on" : "")}
                aria-current={s.id === at ? "true" : undefined}
                onClick={() => setAt(s.id)}
              >
                <span className="sky-guide-n">{n + 1}</span>
                {s.title}
              </button>
            ))}
          </nav>
          <section className="sky-guide-page" aria-labelledby="sky-guide-page-title" data-section={at}>
            <h3 id="sky-guide-page-title">{title}</h3>
            <div className="sky-guide-sketch">{SKETCHES[at]}</div>
            <div className="sky-guide-words">{WORDS[at]}</div>
            <div className="sky-guide-foot">
              <button type="button" className="test-btn" disabled={k === 0} onClick={() => setAt(SKY_GUIDE_SECTIONS[k - 1].id)}>
                Back
              </button>
              <span className="muted">
                {k + 1} of {SKY_GUIDE_SECTIONS.length}
              </span>
              {k < SKY_GUIDE_SECTIONS.length - 1 ? (
                <button type="button" className="btn-primary" onClick={() => setAt(SKY_GUIDE_SECTIONS[k + 1].id)}>
                  Next: {SKY_GUIDE_SECTIONS[k + 1].title}
                </button>
              ) : (
                <button type="button" className="btn-primary" onClick={onClose}>
                  Done
                </button>
              )}
            </div>
          </section>
        </div>
      </div>
    </Modal>
  );
}
