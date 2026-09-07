const loginView = document.getElementById("loginView");
const mainView = document.getElementById("mainView");
const tailorView = document.getElementById("tailorView");
const resultView = document.getElementById("resultView");
const historyView = document.getElementById("historyView");
const profileSection = document.getElementById("profileSection");
const statusEl = document.getElementById("status");

let currentOutputId = null;
let polling = null;

function setStatus(text) {
  statusEl.textContent = text;
}

const PROCESSING = new Set(["pending", "processing"]);

function sendMessage(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(payload, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(response);
    });
  });
}

function showView(view) {
  [tailorView, resultView, historyView].forEach(v => v.classList.add("hidden"));
  view.classList.remove("hidden");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

function statusBadge(status) {
  const safeStatus = escapeHtml(status);
  const cls = { ready: "status-ready", approved: "status-approved", sent: "status-sent", failed: "status-failed" };
  return `<span class="status-badge ${cls[safeStatus] || "status-pending"}">${safeStatus}</span>`;
}

function formatPreview(tailoredJson) {
  try {
    const data = typeof tailoredJson === "string" ? JSON.parse(tailoredJson) : tailoredJson;
    const parts = [];
    if (data.summary) parts.push(`Summary: ${data.summary.substring(0, 150)}...`);
    if (data.skills && data.skills.length) parts.push(`Skills: ${data.skills.join(", ")}`);
    if (data.experience && data.experience.length) {
      parts.push(`Experience: ${data.experience.map(e => `${e.title || e.heading || ""} at ${e.company || ""}`).join("; ")}`);
    }
    return parts.join("\n") || "(no preview available)";
  } catch {
    return "(preview unavailable)";
  }
}

async function refreshView() {
  const res = await sendMessage({ action: "CHECK_AUTH" });
  if (res.ok && res.loggedIn) {
    loginView.classList.add("hidden");
    mainView.classList.remove("hidden");
    const user = res.user || {};
    const dispName = user.full_name || user.email || "";
    document.getElementById("userEmail").textContent = dispName;
    fillProfile(user);
    const resumeRes = await sendMessage({ action: "CHECK_RESUME" });
    const warning = document.getElementById("resumeWarning");
    if (resumeRes.ok && !resumeRes.exists) {
      warning.classList.remove("hidden");
    } else {
      warning.classList.add("hidden");
    }
    showView(tailorView);
  } else {
    loginView.classList.remove("hidden");
    mainView.classList.add("hidden");
  }
}

function fillProfile(user) {
  if (!user) return;
  const set = (id, val) => { document.getElementById(id).value = val || ""; };
  set("pfFullName", user.full_name);
  set("pfPhone", user.phone);
  set("pfLocation", user.location);
  set("pfLinkedin", user.linkedin);
  set("pfGithub", user.github);
  set("pfSmtpUser", user.smtp_username);
  document.getElementById("pfSmtpPass").value = "";
}

function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}

async function showResult(output) {
  currentOutputId = output.id;
  stopPolling();
  const statusEl = document.getElementById("resultStatus");
  statusEl.innerHTML = `Status: ${statusBadge(output.status)}`;
  const msgEl = document.getElementById("resultMessage");
  if (output.status === "failed") {
    msgEl.textContent = `Tailoring failed: ${output.error || "unknown error"}. Check the server logs and try again.`;
  } else if (PROCESSING.has(output.status)) {
    msgEl.textContent = output.draft_message || "The tailoring pipeline is still running — you'll be notified here when it's ready.";
  } else {
    msgEl.textContent = output.draft_message || "(no draft message)";
  }
  const downloadBtn = document.getElementById("downloadBtn");
  const approveBtn = document.getElementById("approveBtn");
  const sendBtn = document.getElementById("sendBtn");
  const busy = PROCESSING.has(output.status);
  downloadBtn.disabled = busy || !output.id || output.status === "failed";
  approveBtn.disabled = busy || output.status === "approved" || output.status === "sent" || output.status === "failed";
  sendBtn.disabled = busy || output.status !== "approved";
  showView(resultView);

  if (busy) {
    setStatus("Tailoring… you'll be notified when it's ready.");
    polling = setInterval(async () => {
      try {
        const r = await sendMessage({ action: "GET_STATUS", outputId: currentOutputId });
        if (r.ok && !PROCESSING.has(r.result.status)) {
          setStatus(r.result.status === "ready" ? "Ready!" : `Done (${r.result.status}).`);
          await showResult(r.result);
        }
      } catch (e) { /* transient — keep polling */ }
    }, 10000);
  } else {
    setStatus(output.status === "failed" ? "Failed." : "");
  }
}

async function showHistory() {
  const listEl = document.getElementById("historyList");
  listEl.innerHTML = "<p style='font-size:12px;color:#888;'>Loading...</p>";
  showView(historyView);
  const res = await sendMessage({ action: "LIST_OUTPUTS" });
  if (!res.ok || !res.outputs.length) {
    listEl.innerHTML = "<p style='font-size:12px;color:#888;'>No past results.</p>";
    return;
  }
  listEl.innerHTML = res.outputs.map(o => {
    let preview = "";
    try {
      const data = typeof o.tailored_json === "string" ? JSON.parse(o.tailored_json) : o.tailored_json;
      if (data.skills) preview = data.skills.slice(0, 3).join(", ");
    } catch {}
    return `<div class="history-item" data-id="${escapeHtml(o.id)}">
      <div>${statusBadge(o.status)} <span style="color:#888;font-size:11px;">${escapeHtml(o.job_post_id.substring(0, 8))}</span></div>
      ${preview ? `<div style="color:#666;margin-top:4px;">${escapeHtml(preview)}</div>` : ""}
    </div>`;
  }).join("");

  listEl.querySelectorAll(".history-item").forEach(el => {
    el.addEventListener("click", async () => {
      const id = el.dataset.id;
      const r = await sendMessage({ action: "GET_STATUS", outputId: id });
      if (r.ok) showResult(r.result);
    });
  });
}

async function loginWithPassword(email, password) {
  setStatus("Logging in...");
  const res = await sendMessage({ action: "LOGIN", email, password });
  if (res.ok) {
    setStatus("Logged in.");
    await refreshView();
  } else {
    setStatus(`Login failed: ${res.error}`);
  }
}

async function loginWithGoogle() {
  setStatus("Logging in with Google...");
  const res = await sendMessage({ action: "LOGIN_WITH_GOOGLE" });
  if (res.ok) {
    setStatus("Logged in with Google.");
    await refreshView();
  } else {
    setStatus(`Login failed: ${res.error}`);
  }
}

document.getElementById("loginBtn").addEventListener("click", async () => {
  const email = document.getElementById("email").value;
  const password = document.getElementById("password").value;
  await loginWithPassword(email, password);
});

document.getElementById("googleLoginBtn").addEventListener("click", async () => {
  await loginWithGoogle();
});

document.getElementById("tailorBtn").addEventListener("click", async () => {
  const resumeRes = await sendMessage({ action: "CHECK_RESUME" });
  if (resumeRes.ok && !resumeRes.exists) {
    setStatus("No resume on file. Upload one at the web app first.");
    return;
  }
  setStatus("Submitting post to the tailoring pipeline…");
  const res = await sendMessage({ action: "TAILOR_CURRENT_POST" });
  if (!res.ok) {
    setStatus(`Error: ${res.error}`);
    return;
  }
  setStatus("");
  await showResult(res.result);
});

document.getElementById("historyBtn").addEventListener("click", async () => {
  stopPolling();
  await showHistory();
});

document.getElementById("profileBtn").addEventListener("click", () => {
  profileSection.classList.toggle("hidden");
  if (!profileSection.classList.contains("hidden")) refreshGmailStatus();
});

document.getElementById("profileSaveBtn").addEventListener("click", async () => {
  const profile = {
    full_name: document.getElementById("pfFullName").value,
    phone: document.getElementById("pfPhone").value,
    location: document.getElementById("pfLocation").value,
    linkedin: document.getElementById("pfLinkedin").value,
    github: document.getElementById("pfGithub").value,
    smtp_username: document.getElementById("pfSmtpUser").value,
    smtp_app_password: document.getElementById("pfSmtpPass").value,
  };
  setStatus("Saving profile...");
  const res = await sendMessage({ action: "UPDATE_PROFILE", profile });
  if (res.ok) {
    document.getElementById("pfSmtpPass").value = "";
    setStatus("Profile saved. It will appear on future resumes.");
    profileSection.classList.add("hidden");
  } else {
    setStatus(`Save failed: ${res.error}`);
  }
});

async function refreshGmailStatus() {
  const st = await sendMessage({ action: "GET_GMAIL_STATUS" });
  const el = document.getElementById("gmailStatus");
  if (!el) return;
  el.textContent = st.ok && st.connected
    ? `Gmail connected (${st.email}) — no password needed.`
    : "Gmail not connected. Click 'Connect Gmail' to enable password-free sending.";
}

document.getElementById("connectGmailBtn").addEventListener("click", async () => {
  setStatus("Opening Google consent in a new tab…");
  const res = await sendMessage({ action: "CONNECT_GMAIL" });
  if (res.ok) {
    setStatus("Authorize in the opened tab, then come back and click 'Check connection'.");
    setTimeout(refreshGmailStatus, 3000);
  } else {
    setStatus(`Connect failed: ${res.error}`);
  }
});

document.getElementById("downloadBtn").addEventListener("click", async () => {
  if (!currentOutputId) return;
  setStatus("Downloading...");
  const res = await sendMessage({ action: "DOWNLOAD_OUTPUT", outputId: currentOutputId });
  setStatus(res.ok ? "Downloaded." : `Download failed: ${res.error}`);
});

document.getElementById("approveBtn").addEventListener("click", async () => {
  if (!currentOutputId) return;
  setStatus("Approving...");
  const res = await sendMessage({ action: "APPROVE_OUTPUT", outputId: currentOutputId });
  if (res.ok) {
    setStatus("Approved.");
    await showResult({ ...res.result, id: currentOutputId, status: "approved" });
  } else {
    setStatus(`Approve failed: ${res.error}`);
  }
});

document.getElementById("sendBtn").addEventListener("click", async () => {
  if (!currentOutputId) return;
  setStatus("Sending...");
  const res = await sendMessage({ action: "SEND_OUTPUT", outputId: currentOutputId });
  if (res.ok) {
    setStatus("Sent!");
    await showResult({ ...res.result, id: currentOutputId, status: "sent" });
  } else {
    setStatus(`Send failed: ${res.error}`);
  }
});

document.getElementById("backBtn").addEventListener("click", () => {
  stopPolling();
  showView(tailorView);
  setStatus("");
});

document.getElementById("historyBackBtn").addEventListener("click", () => {
  showView(tailorView);
  setStatus("");
});

document.getElementById("logoutBtn").addEventListener("click", async () => {
  await sendMessage({ action: "LOGOUT" });
  setStatus("Logged out.");
  await refreshView();
});

refreshView();
