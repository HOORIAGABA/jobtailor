export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface UserOut {
  id: string;
  email: string;
  full_name?: string | null;
  phone?: string | null;
  location?: string | null;
  linkedin?: string | null;
  github?: string | null;
  smtp_username?: string | null;
  smtp_configured: boolean;
}

export interface UserUpdate {
  full_name?: string | null;
  phone?: string | null;
  location?: string | null;
  linkedin?: string | null;
  github?: string | null;
  smtp_username?: string | null;
  smtp_app_password?: string | null;
}

export type OutputStatus =
  | "pending"
  | "processing"
  | "ready"
  | "approved"
  | "sent"
  | "failed";

export interface TailoredOutputOut {
  id: string;
  job_post_id: string;
  resume_id: string;
  status: OutputStatus;
  docx_path?: string | null;
  tailored_json: string;
  draft_message?: string | null;
  gap_analysis?: Array<{ requirement: string; note?: string }> | null;
  error?: string | null;
}

export interface ResumeSection {
  heading?: string | null;
  title: string;
  company?: string | null;
  dates?: string | null;
  bullets: string[];
}

export interface ExtraSection {
  heading: string;
  entries: ResumeSection[];
}

export interface ResumeJSON {
  full_name?: string | null;
  email?: string | null;
  phone?: string | null;
  location?: string | null;
  linkedin?: string | null;
  github?: string | null;
  summary_heading?: string | null;
  summary: string;
  skills: string[];
  experience: ResumeSection[];
  projects: ResumeSection[];
  education: ResumeSection[];
  certifications: ResumeSection[];
  leadership: ResumeSection[];
  extra_sections?: ExtraSection[];
}

export interface ResumeOut {
  id: string;
  label: string;
  version: number;
  resume_json: ResumeJSON;
}

export interface JobPostCreate {
  url?: string | null;
  raw_text?: string | null;
  source_type: "paste" | "url" | "extension";
  resume_id?: string | null;
}

export interface GmailStatus {
  configured: boolean;
  connected: boolean;
  email: string;
}
