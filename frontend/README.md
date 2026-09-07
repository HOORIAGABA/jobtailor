# JobTailor Frontend (Next.js / React)

The production web frontend for the JobTailor agentic resume-tailoring system.
A Next.js 14 (App Router) + TypeScript + Tailwind app that talks to the FastAPI
backend.

## Run locally

```bash
npm install
npm run dev        # -> http://localhost:3000
```

It expects the backend at `http://127.0.0.1:8000` by default. To point it
elsewhere, create `.env.local`:

```env
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000
```

> Backend must run with CORS enabled (it is by default). See
> `../docs/SETUP_GUIDE.md` for full setup.

## Routes

- `/` — landing page
- `/login` — log in / register (plus Google OAuth)
- `/dashboard` — profile (incl. Connect Gmail), resume upload, job submission,
  polling result (download / approve / send), and history

## Layout

- `lib/api.ts` — typed client for every backend endpoint (JWT in `localStorage`).
- `lib/auth-context.tsx` — React auth provider.
- `components/dashboard/` — ProfilePanel, ResumePanel, JobPanel, ResultPanel, HistoryPanel.

## Verify

```bash
npm run lint
npm run build
```
