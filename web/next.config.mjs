/**
 * ★ One origin, because `SameSite=Lax` is not negotiable from this side.
 *
 * This file used to argue the opposite — no rewrite, the browser talks to the
 * API's own origin, "cookies are cross-origin, which the API is configured for:
 * CORS_ORIGINS names this origin and allow_credentials is on". That sentence is
 * wrong, and wrong in the way that costs a day.
 *
 * CORS and SameSite answer different questions. CORS decides whether the
 * *server's* response may be read by a page from another origin;
 * `allow_credentials` is the server saying "I will accept a cookie". SameSite
 * decides whether the *browser* attaches the cookie in the first place. The
 * session cookie is `SameSite=Lax` (app/api/auth.py), and Lax means: send this
 * on top-level navigations, never on a cross-site `fetch`.
 *
 * Deployed, the two halves land on `jobtailor.vercel.app` and
 * `jobtailor-api.vercel.app`. Those look like one site and are not: `vercel.app`
 * is on the Public Suffix List, so they are separate registrable domains and
 * every `fetch` between them is cross-site. Sign-in would have completed, the
 * cookie would have been stored, and then `GET /api/me` would have answered 401
 * with the cookie sitting in the jar unsent — the same silent-401 failure the
 * CORS wildcard produces, arriving from the other direction. Nothing local would
 * have caught it: `localhost:3000` and `localhost:8000` differ by port, and
 * SameSite ignores ports, so in development the cookie is same-site and works.
 *
 * `SameSite=None; Secure` is the usual answer and is not one here. That is a
 * third-party cookie: Safari's ITP and Firefox's Total Cookie Protection block
 * it outright, and Chrome is walking the same road. It would turn "broken" into
 * "broken in some browsers", which is worse.
 *
 * So the browser is given one origin. The UI proxies `/api/*` to the API and
 * every cookie is first-party — including the one Google's callback sets, which
 * is why `GOOGLE_REDIRECT_URI` points at the *UI* host in DEPLOY.md and not at
 * the API host: `Set-Cookie` is attributed to the origin the browser asked, not
 * the one that answered behind the proxy.
 *
 * The old objection to a rewrite was real and has expired. It was: a rewrite
 * becomes "a proxy hop through Vercel in production, on a free plan, in front of
 * a service that can take eight minutes to answer". The service that takes eight
 * minutes is the one with the model, and it never gets deployed — the public
 * instance is read-only and does two things, read a row and return bytes. The
 * hop is now Vercel to Vercel in one region, in front of requests that finish in
 * tens of milliseconds.
 *
 * `API_ORIGIN` is a server-side variable on purpose: it is read here at build
 * time to write the rewrite, and it is never shipped to the browser. Unset (the
 * default, and what a fresh `npm run dev` does) there is no rewrite at all and
 * `lib/api.ts` talks to `http://localhost:8000` exactly as before.
 */

const apiOrigin = process.env.API_ORIGIN?.trim().replace(/\/+$/, "");

const nextConfig = {
  reactStrictMode: true,

  async rewrites() {
    if (!apiOrigin) return [];
    return [
      // `/api/:path*` and not `/:path*`: the UI owns every other route, and a
      // catch-all would proxy the pages too.
      { source: "/api/:path*", destination: `${apiOrigin}/api/:path*` },
    ];
  },
};

export default nextConfig;
