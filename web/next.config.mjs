/**
 * No API rewrite proxy.
 *
 * The obvious convenience here is rewriting /api/* to the FastAPI origin so the
 * browser sees one origin and cookies become trivial. It is not done, because
 * the deployment this is aimed at splits the two: the UI on Vercel (static,
 * free, instant) and the API on Render or a laptop with Ollama (the model cannot
 * be reached from a serverless function at all). A rewrite would work in
 * development and quietly become a proxy hop through Vercel in production, on a
 * free plan, in front of a service that can take eight minutes to answer.
 *
 * So the UI talks to NEXT_PUBLIC_API_URL directly and cookies are cross-origin,
 * which the API is configured for: CORS_ORIGINS names this origin and
 * allow_credentials is on.
 */
const nextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
