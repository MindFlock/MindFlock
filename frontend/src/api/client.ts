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

export async function api<T = unknown>(
  path: string,
  opts?: RequestInit & { json?: unknown }
): Promise<T> {
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
  let body: unknown = null;
  const text = await r.text();
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!r.ok) {
    let msg = `${path} -> ${r.status}`;
    if (body && typeof body === "object") {
      const b = body as Json;
      if (typeof b.error === "string" && b.error) msg = b.error;
      else if (typeof b.detail === "string" && b.detail) msg = b.detail;
    }
    throw new ApiError(r.status, msg, body);
  }
  return body as T;
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
