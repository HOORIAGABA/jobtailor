"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import type { GmailStatus } from "@/lib/types";

export function ProfilePanel() {
  const { user, refresh } = useAuth();
  const [form, setForm] = useState({
    full_name: "",
    phone: "",
    location: "",
    linkedin: "",
    github: "",
    smtp_username: "",
    smtp_app_password: "",
  });
  const [gmail, setGmail] = useState<GmailStatus | null>(null);
  const [status, setStatus] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (user) {
      setForm({
        full_name: user.full_name || "",
        phone: user.phone || "",
        location: user.location || "",
        linkedin: user.linkedin || "",
        github: user.github || "",
        smtp_username: user.smtp_username || "",
        smtp_app_password: "",
      });
    }
  }, [user]);

  const loadGmail = useCallback(async () => {
    try {
      setGmail(await api.gmailStatus());
    } catch {
      setGmail(null);
    }
  }, []);

  useEffect(() => {
    loadGmail();
  }, [loadGmail]);

  async function saveProfile() {
    setSaving(true);
    setStatus("");
    try {
      await api.updateMe({
        full_name: form.full_name || null,
        phone: form.phone || null,
        location: form.location || null,
        linkedin: form.linkedin || null,
        github: form.github || null,
        smtp_username: form.smtp_username || null,
        smtp_app_password: form.smtp_app_password || null,
      });
      setForm((f) => ({ ...f, smtp_app_password: "" }));
      await refresh();
      setStatus("Profile saved.");
    } catch (e) {
      setStatus(`Error: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  }

  async function connectGmail() {
    setStatus("Opening Google consent…");
    try {
      const url = await api.gmailConnectUrl();
      window.open(url, "gmail-connect", "width=520,height=700");
    } catch (e) {
      setStatus(`Connect failed: ${e instanceof Error ? e.message : e}`);
    }
  }

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.data && event.data.type === "gmail_connected") {
        setStatus("Gmail connected!");
        loadGmail();
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [loadGmail]);

  const field = (key: keyof typeof form, label: string, opts?: { type?: string; placeholder?: string }) => (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      <input
        type={opts?.type || "text"}
        placeholder={opts?.placeholder}
        value={form[key]}
        onChange={(e) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
      />
    </label>
  );

  return (
    <section className="rounded-2xl border border-slate-200 p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-base font-semibold text-slate-900">Profile</h2>
        <span className="text-xs text-slate-400">{user?.email}</span>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {field("full_name", "Full name", { placeholder: user?.full_name || "Your name" })}
        {field("phone", "Phone")}
        {field("location", "Location")}
        {field("linkedin", "LinkedIn")}
        {field("github", "GitHub")}
        {field("smtp_username", "Gmail to send from (SMTP fallback)")}
        {field("smtp_app_password", "Gmail App Password (optional)", {
          type: "password",
          placeholder: "Write-only",
        })}
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <button
          onClick={saveProfile}
          disabled={saving}
          className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save profile"}
        </button>
        <button
          onClick={connectGmail}
          className="rounded-lg bg-[#4285f4] px-4 py-2 text-sm font-semibold text-white hover:bg-[#3367d6]"
        >
          Connect Gmail (no password)
        </button>
      </div>

      <p className="mt-3 text-xs text-slate-500">
        {gmail
          ? gmail.connected
            ? `Gmail connected (${gmail.email}) — sending needs no password.`
            : gmail.configured
              ? "Gmail API is configured but not connected. Click 'Connect Gmail' to enable password-free sending."
              : "Gmail API not configured yet; SMTP + App Password will be used to send."
          : ""}
      </p>
      {status && <p className="mt-2 text-sm text-slate-600">{status}</p>}
    </section>
  );
}
