# Loop back to the agent, and easier loop wires (the Sky)

The owner's feedback: *"i was not able to loop well"*. Two causes:

1. A Loop could only go back to a **stop**. The chain the owner wanted,
   **Claude → Wait 5 h → ↺**, which runs Claude again after every wait, could
   not be drawn.
2. Drawing the loop wire was fiddly: the drop didn't connect, and nothing
   said where a wire could go.

This is the frontend half. The backend half (built in parallel,
`backend/web/core/flight_kinds/loop.py`) implements the contract below.

## The contract

- A Loop with config **`back_to: "agent"`** (the literal) goes back to the
  session's agent. The agent is re-prompted and its turn runs; then the chain
  runs again from its first stop. This works only on chains that have a
  session.
- Optional Loop config **`prompt`** (string) is what the agent is sent on each
  repeat. Empty means: re-send the prompt that started the agent's turn.

The frontend names the literal `LOOP_AGENT` (lib/flightDiagram), and
`loopsToAgent(step)` tests for it.

## How to draw a loop

There are three ways. All of them make the same Loop, right after the stop
you start from (after its whole parallel group if it is in one):

- **Drag a wire.** Press a stop's out port (the dot on its right; the grab
  area is bigger than the dot) and drag. While you drag:
  - every tile a drop means something on is lit, with a generous invisible
    pad round it;
  - the tile under the pointer glows;
  - the agent tile wears a **↺** badge;
  - a hint by the pointer says where to go ("Drop on an earlier stop or the
    agent to loop"), then what letting go does ("↺ Loop back to the agent",
    "↺ Loop back to “Fix”").

  Drop on the **agent tile** for a Loop back to the agent, or on an earlier
  stop (or the same one) for a Loop back to that stop.
- **Use the menu.** Right-click a stop (or press the context-menu key) and
  pick **Loop back to…**. A list opens in the menu's place: **The agent (run
  it again)** first, then every stop at or before this one, then ← Back.
  Picking one makes exactly the Loop the wire would. This works on a stop the
  chain is at, too. For example, on a running Wait: Claude → Wait → ⋯ → Loop
  back to → The agent.
- **Re-aim a loop.** Drag a Loop's pill onto the agent tile, or onto another
  earlier stop.

To change a Loop, click its pill. In the Loop's dialog, a Loop aimed at the
agent shows **Back to: the agent** and a **Prompt** field ("Empty = send the
same prompt again"). It also has a **Go back to a stop instead** button. On a
session's chain, a Loop aimed at a stop offers **↺ Loop back to the agent
instead**.

## How it draws

`loopEdges` turns a Loop aimed at the agent into a `loop` edge. The edge runs
from the last stop before the Loop to the session box. The Loop itself
rides the edge as `via` and draws no tile. With nothing before the Loop, the
edge runs from the session box round to itself. On a chain with no session,
the Loop draws no wire and stays a box. In the Sky's graph (`chainGraph`) the
session box is the head, so the wire ends at the agent tile's left side and
carries the usual pill ("↻ ×3", "↻ until …", "2 / 3").

**Never through a tile.** `loopWirePathG` still runs a loop under the line
first: under every tile it passes, and under the agent's extras when one end
is the agent. Before it settles on that route, `loopHits` checks the route's
three legs (the hook beside each port and the run between them) against every
other tile. If something is in the way, the wire goes **over** the tiles
instead, with the same hooks and pill. Sub-sessions hung under the agent are
the usual case. If both routes cross something (a tile dragged into every
lane), the wire takes the route that crosses fewer tiles. Parallel groups
work as before: the wire leaves the group's last (lowest) member.

## Where it lives

| What | Where |
|---|---|
| `LOOP_AGENT`, `loopsToAgent`, `loopStepTo(def, ids, LOOP_AGENT)`, `loopBackTo`, `loopChoices`, `loopFromOf`, the agent edge in `loopEdges` | `frontend/src/lib/flightDiagram.ts` |
| `wireIntent` (own head → `{op: "loop", agent: true}`), `loopWirePathG` over/under, `loopHits` | `frontend/src/lib/skyGraph.ts` |
| `runWireIntent` for an agent Loop, the drag hint (`dragHint`, `intentHint`, `WireBand.hint`) | `components/flights/useSkyWiring.ts` |
| The hint drawn by the pointer (`.sky-band-hint`) | `components/flights/SkyWires.tsx` |
| `.is-wiring` / `.is-agent-target`, the bigger out port, the drop pads | `components/flights/SkyChain.tsx` and `SkyChain.css` (its own commented block at the end) |
| ⋯ **Loop back to…** (`withLoopItem`, `loopMenuItems`, `stepLoopChoices`), the pill re-aim onto the agent (`pillAimer`) | `components/flights/useChainMenu.tsx` |
| The session line's `onLoopFrom` / `onLoopTo` for the session box, `loopChoicesOf` on `graphCanvas` | `components/flights/useSessionLine.tsx` |
| "Back to: the agent", the prompt field (`loopFormDef`, `loopAgent`) | `components/flights/StopDialog.tsx` |

Tests: `frontend/src/__tests__/loopAgentSky.test.ts`. Two pins in
`skyGraph.test.ts` moved, because a stop's wire dropped on its own agent tile
is now a Loop (it was `none` before), and that tile is lit while a wire is
dragged.

## Limits

- A stops-only chain (no session) has no agent, so its menu lists only its
  earlier stops. It builds that list from the flight's steps, and only for a
  stop that can still change.
- A Loop's own ⋯ menu has no "Loop back to…". Re-aim a Loop by dragging its
  pill, or change it in its dialog.
- A head-round loop (a Loop first on the chain) has no tile before it, so
  its pill can't be dragged to re-aim it. Its dialog still edits it.
