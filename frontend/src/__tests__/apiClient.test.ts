/** api/client.ts — api() (JSON) and apiText() (plain text) share one request
 * path: the 401 reload and the thrown ApiError. apiText exists so plain-text
 * endpoints (a pane's history capture) stop bypassing that with raw fetch(),
 * without running their body through JSON.parse. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, apiText } from "../api/client";

const g = globalThis as unknown as { fetch: unknown; location: unknown };
const realFetch = g.fetch;
const realLocation = g.location;

function serve(status: number, body: string) {
  const fetchMock = vi.fn(async () => new Response(body, { status }));
  g.fetch = fetchMock;
  return fetchMock;
}

afterEach(() => {
  g.fetch = realFetch;
  g.location = realLocation;
});

describe("apiText", () => {
  it("returns the body verbatim — an empty capture stays '', JSON-looking text stays text", async () => {
    serve(200, "");
    expect(await apiText("/h")).toBe("");
    expect(await (serve(200, ""), api("/h"))).toBeNull();
    serve(200, "123");
    expect(await apiText("/h")).toBe("123");
  });

  it("throws the server's message on non-2xx, like api()", async () => {
    serve(404, JSON.stringify({ error: "no such session" }));
    await expect(apiText("/h")).rejects.toMatchObject({ status: 404, message: "no such session" });
    serve(500, "boom");
    const err = await apiText("/h").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    // Never the request path: a toast reads this.
    expect(err.message).toBe("MindFlock hit an error (details in the log).");
    expect(err.message).not.toContain("/h");
  });

  it("prefers the route's plain `detail` sentence over the terse `error`", async () => {
    serve(500, JSON.stringify({ error: "fix session failed", detail: "Couldn't start a fix session — MindFlock hit an error (details in the log)." }));
    await expect(apiText("/api/test-plans/x/fix")).rejects.toMatchObject({
      status: 500,
      message: "Couldn't start a fix session — MindFlock hit an error (details in the log).",
    });
  });

  it("reloads on 401 (the token gate turned on mid-session)", async () => {
    const reload = vi.fn();
    g.location = { reload };
    serve(401, "");
    await expect(apiText("/h")).rejects.toMatchObject({ status: 401 });
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("api", () => {
  it("still encodes `json` as a POST body and parses the reply", async () => {
    const f = serve(200, JSON.stringify({ ok: true }));
    expect(await api("/p", { json: { a: 1 } })).toEqual({ ok: true });
    const init = (f.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"a":1}');
  });
});

describe("no raw fetch() beside api()", () => {
  const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../__tests__/**"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;
  it("only api/client.ts and the prefs keepalive beacon call window fetch", () => {
    const callers = Object.entries(SOURCES)
      // `fetch(` not preceded by an identifier character or a dot (so not
      // `.fetch(`, `refetch(`, or a parameter named `fetch` being declared).
      .filter(([, src]) => /(^|[^\w.])fetch\(\s*["'`/]/m.test(src))
      .map(([p]) => p)
      .sort();
    expect(callers).toEqual(["../lib/prefs.ts"]);
  });
});
