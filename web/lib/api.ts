/**
 * The one place that talks to the API.
 *
 * Two things here are load-bearing.
 *
 * `credentials: "include"` on every request. Without it the browser holds a
 * perfectly good cookie and sends it nowhere, and every screen reports "not
 * signed in" while the network tab shows a 401 with no explanation. It is
 * necessary but not sufficient: it says this page is willing to send the cookie,
 * and `SameSite` on the cookie says whether the browser agrees. Both have to
 * line up, which is what the one-origin arrangement in `next.config.mjs` is for.
 *
 * Status codes are translated into intentions, not messages. The API is careful
 * about which code it returns — 401 sign in, 428 connect Gmail, 409 the state
 * moved under you, 503 this instance is not configured — and each one sends the
 * UI to a different place. A single `catch (e) { setError(String(e)) }` would
 * throw that distinction away and show "Error: 428" to a person whose actual
 * problem is that they have not connected Gmail yet.
 */

/**
 * Where the API is, from the browser's point of view.
 *
 * Three cases, and the middle one is the deployed one:
 *
 *   unset            → `http://localhost:8000`. A fresh `npm run dev` with
 *                      uvicorn on the side, unchanged.
 *   `/`              → `""`, so every path below is requested relative and the
 *                      browser only ever sees this origin. `next.config.mjs`
 *                      rewrites `/api/*` to the real API. This is what the
 *                      deployment sets, and the reason is the whole comment at
 *                      the top of that file: a cross-site `fetch` does not carry
 *                      a `SameSite=Lax` cookie, so an API on its own hostname is
 *                      an API nobody can be signed in to.
 *   an absolute URL  → used as given. Still correct for a same-site pair such as
 *                      two ports on localhost, and the escape hatch if the proxy
 *                      ever has to come out.
 *
 * `??` and an explicit empty check rather than `||`: with `||`, setting this to
 * `/` normalises to `""`, which is falsy, and the fallback would quietly send a
 * production build at localhost.
 */
const configured = process.env.NEXT_PUBLIC_API_URL?.trim();
export const API =
  configured === undefined || configured === ""
    ? "http://localhost:8000"
    : configured.replace(/\/+$/, "");

/** Why a request failed, in terms the UI can branch on. */
export type Reason =
  | "signin"      // 401 — nobody is signed in
  | "connect"     // 428 — signed in, but Gmail is not connected
  | "stale"       // 409 — the run moved; reload and look again
  | "refused"     // 400 — the request was not acceptable
  | "missing"     // 404 — not found, or not yours (the API does not distinguish)
  | "setup"       // 503 — this instance is missing configuration
  | "provider"    // 502 — an upstream service failed
  | "server"      // 500 or anything else
  | "offline";    // the API could not be reached at all

export class ApiError extends Error {
  constructor(
    readonly reason: Reason,
    message: string,
    readonly status = 0,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const REASONS: Record<number, Reason> = {
  400: "refused",
  401: "signin",
  404: "missing",
  409: "stale",
  428: "connect",
  502: "provider",
  503: "setup",
};

async function detailOf(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) {
      // FastAPI validation errors arrive as a list of objects. The useful part
      // is the message, not the JSON.
      return body.detail.map((d: { msg?: string }) => d.msg ?? "").join("; ");
    }
    return JSON.stringify(body);
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, {
      ...init,
      credentials: "include",
      headers:
        init.body instanceof FormData
          ? init.headers
          : { "Content-Type": "application/json", ...(init.headers ?? {}) },
    });
  } catch {
    throw new ApiError(
      "offline",
      API
        ? `Could not reach the API at ${API}. Is it running? ` +
            `(uvicorn app.api.main:app --reload)`
        : `Could not reach the API at this origin. ${path} is proxied to ` +
            `API_ORIGIN — check that it is set on this deployment.`,
    );
  }

  if (!response.ok) {
    throw new ApiError(
      REASONS[response.status] ?? "server",
      await detailOf(response),
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T>(path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<T>(path, { method: "POST", body: form });
  },
};

/** Where the browser goes to sign in. A navigation, not a fetch — the OAuth
 *  flow is a redirect chain and `fetch` cannot follow it into Google. */
export const signInUrl = `${API}/api/auth/google/start`;
export const connectGmailUrl = `${API}/api/auth/google/start?connect=true`;

/** A file the API serves. Used as an href, so the cookie rides along. */
export const fileUrl = (runId: string, stage: string) =>
  `${API}/api/runs/${runId}/file/${stage}`;
export const emlUrl = (runId: string) => `${API}/api/runs/${runId}/eml`;
