from pydantic import BaseModel, EmailStr
from typing import Optional


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserUpdate(BaseModel):
    """Profile fields a user may edit. smtp_app_password is write-only:
    an empty string keeps the stored value, an explicit None clears it."""
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    smtp_username: Optional[str] = None
    smtp_app_password: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None
    smtp_username: Optional[str] = None
    smtp_configured: bool = False

    class Config:
        from_attributes = True


class ResumeSection(BaseModel):
    title: str
    company: Optional[str] = None
    dates: Optional[str] = None
    heading: Optional[str] = None
    bullets: list[str] = []


class ResumeJSON(BaseModel):
    summary_heading: Optional[str] = None
    summary: str = ""
    skills: list[str] = []
    experience: list[ResumeSection] = []
    projects: list[ResumeSection] = []
    education: list[ResumeSection] = []
    certifications: list[ResumeSection] = []
    leadership: list[ResumeSection] = []


class ResumeOut(BaseModel):
    id: str
    label: str
    version: int
    resume_json: ResumeJSON

    class Config:
        from_attributes = True


class JobPostCreate(BaseModel):
    url: Optional[str] = None
    raw_text: Optional[str] = None
    source_type: str = "paste"  # paste | url | extension
    resume_id: Optional[str] = None  # if omitted, latest active resume is used


class JobPostOut(BaseModel):
    id: str
    url: Optional[str]
    source_type: str

    class Config:
        from_attributes = True


class TailoredOutputOut(BaseModel):
    id: str
    job_post_id: str
    resume_id: str
    status: str
    docx_path: Optional[str]
    tailored_json: str
    draft_message: Optional[str] = None
    error: Optional[str] = None  # populated when status == failed

    class Config:
        from_attributes = True
