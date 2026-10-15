/** Fetch wrapper — port of app.js api(). JSON in/out; non-2xx throws with the
 * server's error message when it sent one. A 401 means the access-token gate
 * turned on mid-session: reload so the auth screen takes over. */

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** The shared request: JSON body encoding, the 401 reload, and a thrown
 * ApiError (the server's message when it sent one) on non-2xx. Resolves to the
 * raw response text. */
async function request(path: string, opts?: RequestInit & { json?: unknown }): Promise<string> {
  const init: RequestInit = { ...opts };
  if (opts && "json" in opts && opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (r.status === 401) {
    location.reload();
    throw new ApiError(401, "unauthorized", null);
  }
  const text = await r.text();
  if (!r.ok) {
    const body = parse(text);
    // Words, never the request: "/api/test-plans/x/fix -> 500" in a toast is
    // the server's address, not a reason. `detail` is the plain sentence a
    // route writes for people; `error` is the older terse field.
    let msg = statusSentence(r.status);
    if (body && typeof body === "object") {
      const b = body as Json;
      if (typeof b.detail === "string" && b.detail) msg = b.detail;
      else if (typeof b.error === "string" && b.error) msg = b.error;
    }
    throw new ApiError(r.status, msg, body);
  }
  return text;
}

/** What a failed request says when the server sent no reason of its own. */
export function statusSentence(status: number): string {
  if (status === 404) return "MindFlock couldn't find that — it may have just been removed.";
  if (status >= 500) return "MindFlock hit an error (details in the log).";
  return "MindFlock couldn't do that (error " + status + ").";
}

function parse(text: string): unknown {
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return text;
  }
}

export async function api<T = unknown>(
  path: string,
  opts?: RequestInit & { json?: unknown }
): Promise<T> {
  return parse(await request(path, opts)) as T;
}

/** api() for a plain-text endpoint (a pane's history capture): the same 401
 * reload and error handling, but the body comes back verbatim — never run
 * through JSON.parse, which would turn an empty capture into null. */
export function apiText(path: string, opts?: RequestInit & { json?: unknown }): Promise<string> {
  return request(path, opts);
}

/** Instance-scoped fetch: `api('/api/instances/' + encodeURIComponent(title) +
 * suffix, opts)`. Centralizes the encode-and-concat so no call site can forget
 * encodeURIComponent. `suffix` starts the path segment(s) after the title,
 * e.g. `'/close'`, `'/queue'`, `'?item=' + …`, or `''` for the bare instance. */
export function instApi<T = unknown>(
  title: string,
  suffix: string,
  opts?: RequestInit & { json?: unknown }
): Promise<T> {
  return api<T>("/api/instances/" + encodeURIComponent(title) + suffix, opts);
}

type Json = Record<string, unknown>;
