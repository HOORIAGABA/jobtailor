/**
 * The shapes the API actually returns.
 *
 * Hand-written rather than generated, and kept deliberately loose where the
 * payload is a JSON blob the UI only renders (`brief`, `evidence`, `standing`).
 * A generated client would be stricter and would also have to be regenerated
 * every time a stage gains a field, which on this project is weekly.
 */

export type RunStatus =
  | "created"
  | "tailoring"
  | "needs_review"
  | "approved"
  | "rejected"
  | "sending"
  | "sent"
  | "failed";

export type ResumeStatus =
  | "uploaded"
  | "parsing"
  | "needs_confirm"
  | "confirmed"
  | "superseded"
  | "failed";

export interface Me {
  id: string;
  email: string;
  name: string;
  gmail_connected: boolean;
  gmail_scopes: string[];
  can_send: boolean;
}

export interface Capabilities {
  read_only: boolean;
  can_start_runs: boolean;
  why: string;
  stages_built: string[];
  stages_not_built: string[];
  mail_provider: string;
}

export interface Contact {
  full_name: string;
  email: string;
  phone: string;
  location: string;
  linkedin: string;
  github: string;
  website: string;
}

export interface RawEntry {
  title: string;
  org: string;
  dates: string;
  bullets: string[];
}

export interface RawSection {
  heading: string;
  entries: RawEntry[];
}

export interface RawResume {
  contact: Contact;
  summary: string;
  sections: RawSection[];
}

export interface ResumeSummary {
  id: string;
  filename: string;
  version: number;
  status: ResumeStatus;
  revision: number;
  error: string;
  sections: { kind: string; heading: string; items: number; bullets: number }[];
  skills: number;
  /** The exact source lines the parse lost. Not a debug field — this is the
   *  only kind of confirmation prompt anyone completes. */
  unplaced_lines: string[];
  invented_lines: string[];
  structure_warnings: string[];
  parse_is_clean: boolean;
}

export interface ResumeDetail extends ResumeSummary {
  raw: RawResume | null;
  doc: unknown;
}

export interface RunSummary {
  id: string;
  status: RunStatus;
  stage: string;
  role: string;
  company: string;
  resume_id: string;
  llm_calls: number;
  tokens: number;
  error: string;
  proof: Record<string, unknown>;
  created_at: string;
  decided_at: string;
  changes: number;
  accepted: number;
  rejected: number;
  can_download: boolean;
}

export interface RunDetail extends RunSummary {
  brief: unknown;
  standing: { demonstrated?: string[]; declared_only?: string[]; not_found?: string[] } | null;
  evidence: unknown;
  ops: unknown[];
  accepted_ops: unknown[];
  rejected_ops: unknown[];
  diff: Diff | null;
  outreach: Outreach | null;
  call_log: { stage?: string; tokens?: number; model?: string }[];
  artifacts: { stage: string; filename: string; content_type: string; bytes: number }[];
  checkpoints: { stage: string; at: string }[];
  may_send: boolean;
}

export interface Change {
  op_id?: string;
  op?: string;
  kind?: string;
  where?: string;
  before?: string;
  after?: string;
  why?: string;
  cites?: string[];
}

export interface Diff {
  changes?: Change[];
  gaps?: { requirement?: string; note?: string; why?: string }[];
  questions?: string[];
  rejections?: { op_id?: string; rule?: string; why?: string }[];
}

export interface Outreach {
  recipient?: string;
  recipient_candidates?: string[];
  subject?: string;
  body?: string;
  problems?: string[];
  cites?: string[];
}

/** ★ The gate. Everything the person is being asked to decide about, plus the
 *  token that binds their answer to exactly this text. */
export interface Preview {
  run_id: string;
  recipient: string;
  recipient_candidates: string[];
  subject: string;
  body: string;
  problems: string[];
  cites: string[];
  changes: Change[];
  gaps: { requirement?: string; note?: string; why?: string }[];
  questions: string[];
  rejections: { op_id?: string; rule?: string; why?: string }[];
  standing: { demonstrated?: string[]; declared_only?: string[]; not_found?: string[] };
  confirm_token: string;
  can_download: boolean;
  will_send: boolean;
}

export interface Decision {
  status: RunStatus;
  message_id?: string;
  may_send: boolean;
  can_download: boolean;
}

export interface SendResult {
  status: RunStatus;
  provider: string;
  provider_message_id: string;
  sent_at: string;
  attachments: string[];
  /** False on the console backend: nothing left the machine, and the API says
   *  so rather than letting the UI imply otherwise. */
  dispatched: boolean;
}
