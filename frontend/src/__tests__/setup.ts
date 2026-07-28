// The xterm UMD bundles (pulled in transitively through lib/terminals, which
// several logic modules import for their action callbacks) reference the
// browser global `self` at module-load time. These unit tests exercise only
// pure logic and never open a terminal, so defining `self` is enough to let
// those modules import under the lightweight node test environment — no DOM
// is otherwise touched.
const g = globalThis as Record<string, unknown>;
if (typeof g.self === "undefined") g.self = globalThis;
