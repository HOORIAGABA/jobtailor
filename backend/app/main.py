import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.database import Base, engine
from app.models import User, Resume, JobPost, TailoredOutput, Message
from app.routers import auth, resumes, job_posts, tailored_outputs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# CORS origins: restrict in production, allow all in development
CORS_ORIGINS = (
    os.getenv("CORS_ORIGINS", "").split(",")
    if os.getenv("CORS_ORIGINS")
    else ["*"]
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)

    logging.warning(
        "Resume parser config: provider=%s base_url=%s model=%s",
        settings.resume_parser_provider,
        settings.resume_parser_base_url,
        settings.resume_parser_model,
    )

    # Tiny SQLite migrations so existing local DBs pick up new columns.
    if str(engine.url).startswith("sqlite"):
        with engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info(users)" )).fetchall()
            col_names = {row[1] for row in cols}
            new_user_cols = {
                "oauth_tokens": "TEXT",
                "full_name": "VARCHAR",
                "phone": "VARCHAR",
                "location": "VARCHAR",
                "linkedin": "VARCHAR",
                "github": "VARCHAR",
                "smtp_username": "VARCHAR",
                "smtp_app_password": "VARCHAR",
            }
            for name, sql_type in new_user_cols.items():
                if name not in col_names:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {sql_type}"))

            out_cols = conn.execute(text("PRAGMA table_info(tailored_outputs)")).fetchall()
            out_col_names = {row[1] for row in out_cols}
            if "pdf_path" not in out_col_names:
                conn.execute(text("ALTER TABLE tailored_outputs ADD COLUMN pdf_path VARCHAR"))
            if "gap_analysis" not in out_col_names:
                conn.execute(text("ALTER TABLE tailored_outputs ADD COLUMN gap_analysis TEXT"))

            res_cols = conn.execute(text("PRAGMA table_info(resumes)")).fetchall()
            res_col_names = {row[1] for row in res_cols}
            if "raw_text" not in res_col_names:
                conn.execute(text("ALTER TABLE resumes ADD COLUMN raw_text TEXT"))

    # The tailoring pipeline now runs in a background thread. If the process
    # restarts mid-pipeline (edit with --reload, crash, etc.) those outputs
    # are orphaned as pending/processing — flag them so the UI stops polling
    # and shows a clear reason instead of hanging forever.
    from app.database import SessionLocal
    with SessionLocal() as db:
        orphaned = db.query(TailoredOutput).filter(
            TailoredOutput.status.in_(["pending", "processing"])
        ).all()
        for out in orphaned:
            try:
                tj = json.loads(out.tailored_json or "{}")
            except Exception:
                tj = {}
            if not isinstance(tj, dict):
                tj = {}
            tj["error"] = (
                "Pipeline was interrupted (server restarted). "
                "Submit the job post again to re-run it."
            )
            out.tailored_json = json.dumps(tj)
            out.status = "failed"
        if orphaned:
            db.commit()
            logging.warning("Marked %d orphaned pipeline output(s) as failed.", len(orphaned))

    yield


app = FastAPI(
    title="Agentic Resume Tailoring + Outreach System API",
    version="1.0.0",
    lifespan=lifespan,
)

# Chrome extensions and the local frontend both need CORS enabled.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(resumes.router)
app.include_router(job_posts.router)
app.include_router(tailored_outputs.router)


@app.get("/health")
def health():
    return {"status": "ok"}
