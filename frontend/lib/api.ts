import type {
  GmailStatus,
  JobPostCreate,
  ResumeOut,
  TailoredOutputOut,
  TokenResponse,
  UserOut,
  UserUpdate,
} from "./types";

const TOKEN_KEY = "jobtailor_token";

function getDefaultBaseUrl(): string {
  if (typeof window === "undefined") return "http://127.0.0.1:8000";
  const proto = window.location.protocol === "https:" ? "https:" : "http:";
  return `${proto}//${window.location.hostname}:8000`;
}

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL || getDefaultBaseUrl();

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  isForm = false,
): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!isForm && options.body && typeof options.body === "string") {
    headers["Content-Type"] = "application/json";
  }

  const resp = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try {
      const err = await resp.json();
      detail = err.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, resp.status);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  // ---- Auth ----
  register: (email: string, password: string) =>
    request<TokenResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => request<UserOut>("/auth/me"),
  updateMe: (payload: UserUpdate) =>
    request<UserOut>("/auth/me", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  logout: () => request<{ message: string }>("/auth/logout", { method: "POST" }),

  googleLoginUrl: async (): Promise<string> => {
    const redirectUri = `${API_BASE_URL}/auth/google/callback`;
    const resp = await fetch(
      `${API_BASE_URL}/auth/google/login?state=frontend&redirect_uri=${encodeURIComponent(
        redirectUri,
      )}`,
    );
    if (!resp.ok) throw new ApiError("Failed to start Google login", resp.status);
    const data = (await resp.json()) as { auth_url: string };
    return data.auth_url;
  },

  // ---- Gmail (password-free sending) ----
  gmailStatus: () => request<GmailStatus>("/auth/gmail/status"),
  gmailConnectUrl: async (): Promise<string> => {
    const data = await request<{ auth_url: string }>("/auth/gmail/connect");
    const sep = data.auth_url.includes("?") ? "&" : "?";
    return `${data.auth_url}${sep}state=frontend-gmail`;
  },

  // ---- Resumes ----
  uploadResume: (file: File, label = "Default") => {
    const form = new FormData();
    form.append("file", file);
    form.append("label", label);
    return request<ResumeOut>("/resumes/upload", { method: "POST", body: form }, true);
  },
  listResumes: () => request<ResumeOut[]>("/resumes"),

  // ---- Job posts / pipeline ----
  submitJobPost: (payload: JobPostCreate) =>
    request<TailoredOutputOut>("/job-posts", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getOutput: (id: string) =>
    request<TailoredOutputOut>(`/tailored-outputs/${id}`),
  listOutputs: () => request<TailoredOutputOut[]>("/tailored-outputs"),
  approveOutput: (id: string) =>
    request<{ status: string }>(`/tailored-outputs/${id}/approve`, { method: "POST" }),
  sendOutput: (id: string) =>
    request<{ status: string; sent_at?: string }>(`/tailored-outputs/${id}/send`, {
      method: "POST",
    }),

  downloadPdf: async (id: string): Promise<void> => {
    const token = getToken();
    const resp = await fetch(`${API_BASE_URL}/tailored-outputs/${id}/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!resp.ok) {
      let detail = `Download failed (${resp.status})`;
      try {
        const err = await resp.json();
        detail = err.detail || detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(detail, resp.status);
    }
    const blob = await resp.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "tailored_resume.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  },
};
