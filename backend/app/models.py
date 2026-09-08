import uuid
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    oauth_tokens: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON string of provider tokens

    # User profile / resume header fields
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    linkedin: Mapped[str | None] = mapped_column(String, nullable=True)
    github: Mapped[str | None] = mapped_column(String, nullable=True)

    # Per-user SMTP so each user sends from their OWN Gmail (no hardcoded
    # shared sender in .env). App password is write-only: never returned.
    smtp_username: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_app_password: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    resumes: Mapped[list["Resume"]] = relationship(back_populates="user")
    job_posts: Mapped[list["JobPost"]] = relationship(back_populates="user")


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, default="Default")
    resume_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-serialized structured resume
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # extracted source text (debug/re-parse)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="resumes")


class JobPost(Base):
    __tablename__ = "job_posts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String, default="paste")  # paste | url | extension
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="job_posts")


class TailoredOutput(Base):
    __tablename__ = "tailored_outputs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    job_post_id: Mapped[str] = mapped_column(String, ForeignKey("job_posts.id"), nullable=False)
    resume_id: Mapped[str] = mapped_column(String, ForeignKey("resumes.id"), nullable=False)
    tailored_json: Mapped[str] = mapped_column(Text, nullable=False)
    docx_path: Mapped[str] = mapped_column(String, nullable=True)
    pdf_path: Mapped[str] = mapped_column(String, nullable=True)
    gap_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of missing requirements
    status: Mapped[str] = mapped_column(String, default="pending")  # pending|ready|approved|sent|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_uuid)
    tailored_output_id: Mapped[str] = mapped_column(String, ForeignKey("tailored_outputs.id"), nullable=False)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|approved|sent
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
