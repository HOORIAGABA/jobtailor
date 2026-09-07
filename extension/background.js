/**
 * Service worker: owns the auth token and all backend API calls.
 * The content script never talks to the backend directly — it only
 * reads the page and hands text back to here.
 */

const API_BASE_URL = "http://127.0.0.1:8000"; // change to your deployed backend URL

async function getStoredToken() {
  const { authToken } = await chrome.storage.local.get("authToken");
  return authToken || null;
}

async function setStoredToken(token) {
  await chrome.storage.local.set({ authToken: token });
}

async function login(email, password) {
  const resp = await fetch(`${API_BASE_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) throw new Error(`Login failed (${resp.status})`);
  const data = await resp.json();
  await setStoredToken(data.access_token);
  return data.access_token;
}

async function loginWithGoogle() {
  return new Promise(async (resolve, reject) => {
    try {
      const redirectUri = chrome.identity.getRedirectURL("google-oauth");
      
      // Step 1: Get the authorization URL from the backend
      const authResp = await fetch(`${API_BASE_URL}/auth/google/login?redirect_uri=${encodeURIComponent(redirectUri)}`);
      if (!authResp.ok) throw new Error("Failed to get Google auth URL");
      const authData = await authResp.json();
      
      const authUrl = authData.auth_url;
      
      // Step 2: Launch the OAuth flow
      chrome.identity.launchWebAuthFlow(
        {
          url: authUrl,
          interactive: true,
        },
        async (redirectUrl) => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
            return;
          }
          
          try {
            // Step 3: Extract authorization code from redirect URL
            // redirectUrl will be like: https://<ext-id>.chromiumapp.org/google-oauth?code=...
            const url = new URL(redirectUrl);
            const code = url.searchParams.get("code");
            
            if (!code) {
              reject(new Error("No authorization code found in redirect URL"));
              return;
            }
            
            // Step 4: Exchange code for JWT token
            const tokenResp = await fetch(`${API_BASE_URL}/auth/google/token`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                code: code,
                redirect_uri: redirectUri,
              }),
            });
            
            if (!tokenResp.ok) {
              const err = await tokenResp.json();
              throw new Error(err.detail || "Failed to exchange code for token");
            }
            
            const tokenData = await tokenResp.json();
            const token = tokenData.access_token;
            
            // Step 5: Store the token
            await setStoredToken(token);
            resolve(token);
          } catch (err) {
            reject(err);
          }
        }
      );
    } catch (err) {
      reject(err);
    }
  });
}

async function submitJobPost(rawText, url) {
  const token = await getStoredToken();
  if (!token) throw new Error("Not logged in. Open the popup and log in first.");

  const resp = await fetch(`${API_BASE_URL}/job-posts`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ raw_text: rawText, url, source_type: "extension" }),
  });

  if (resp.status === 401) {
    await setStoredToken(null);
    throw new Error("Session expired. Please log in again.");
  }
  if (!resp.ok) {
    const errBody = await resp.json().catch(() => ({}));
    throw new Error(errBody.detail || `Request failed (${resp.status})`);
  }
  return resp.json();
}

async function getUserInfo() {
  const token = await getStoredToken();
  if (!token) return null;
  const resp = await fetch(`${API_BASE_URL}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return null;
  return resp.json();
}

async function updateProfile(fields) {
  const token = await getStoredToken();
  if (!token) throw new Error("Not logged in. Open the popup and log in first.");
  const resp = await fetch(`${API_BASE_URL}/auth/me`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(fields),
  });
  if (resp.status === 401) {
    await setStoredToken(null);
    throw new Error("Session expired. Please log in again.");
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Profile update failed (${resp.status})`);
  }
  return resp.json();
}

async function checkResumeExists() {
  const token = await getStoredToken();
  if (!token) return false;
  const resp = await fetch(`${API_BASE_URL}/resumes`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return false;
  const resumes = await resp.json();
  return resumes.length > 0;
}

async function getTailoredOutput(outputId) {
  const token = await getStoredToken();
  const resp = await fetch(`${API_BASE_URL}/tailored-outputs/${outputId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`Status check failed (${resp.status})`);
  return resp.json();
}

async function listTailoredOutputs() {
  const token = await getStoredToken();
  if (!token) return [];
  const resp = await fetch(`${API_BASE_URL}/tailored-outputs`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return [];
  return resp.json();
}

async function approveOutput(outputId) {
  const token = await getStoredToken();
  const resp = await fetch(`${API_BASE_URL}/tailored-outputs/${outputId}/approve`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Approve failed (${resp.status})`);
  }
  return resp.json();
}

async function sendOutput(outputId) {
  const token = await getStoredToken();
  const resp = await fetch(`${API_BASE_URL}/tailored-outputs/${outputId}/send`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Send failed (${resp.status})`);
  }
  return resp.json();
}

async function getGmailStatus() {
  const token = await getStoredToken();
  if (!token) return { configured: false, connected: false };
  const resp = await fetch(`${API_BASE_URL}/auth/gmail/status`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) return { configured: false, connected: false };
  return resp.json();
}

async function connectGmail() {
  // Open the backend's consent URL in a new tab (the extension can't pop a
  // consent modal as easily as a web page). The user authorizes once and we
  // store the token server-side on their account.
  const token = await getStoredToken();
  if (!token) throw new Error("Not logged in.");
  const resp = await fetch(`${API_BASE_URL}/auth/gmail/connect`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || "Could not start Gmail connection");
  }
  const data = await resp.json();
  if (data.auth_url) {
    await chrome.tabs.create({ url: data.auth_url });
    return { ok: true };
  }
  throw new Error("No Gmail auth URL returned");
}

async function downloadOutput(outputId) {
  const token = await getStoredToken();
  const resp = await fetch(`${API_BASE_URL}/tailored-outputs/${outputId}/download`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`Download failed (${resp.status})`);
  const blob = await resp.blob();
  const reader = new FileReader();
  return new Promise((resolve, reject) => {
    reader.onloadend = () => {
      const dataUrl = reader.result;
      chrome.downloads.download({
        url: dataUrl,
        filename: "tailored_resume.pdf",
        saveAs: true,
      }, (downloadId) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(downloadId);
        }
      });
    };
    reader.onerror = () => reject(new Error("Failed to read file"));
    reader.readAsDataURL(blob);
  });
}

async function extractPostTextFromTab(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { action: "GET_POST_TEXT" });
    if (response && response.text) {
      return response;
    }
  } catch (err) {
    // Fall through to a direct injection fallback below.
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const selectors = [
        '[data-test-id="main-feed-activity-card"]',
        '.feed-shared-update-v2',
        '.scaffold-finite-scroll__content article',
      ];

      for (const selector of selectors) {
        const el = document.querySelector(selector);
        if (el && el.innerText && el.innerText.trim().length > 0) {
          return { text: el.innerText.trim(), url: window.location.href };
        }
      }

      const main = document.querySelector('main');
      return { text: main ? main.innerText.trim() : null, url: window.location.href };
    },
  });

  const fallback = results && results[0] && results[0].result;
  return fallback || { text: null, url: null };
}

// Central message router: popup.js sends these actions
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.action === "LOGIN") {
        await login(msg.email, msg.password);
        sendResponse({ ok: true });
      } else if (msg.action === "LOGIN_WITH_GOOGLE") {
        const token = await loginWithGoogle();
        sendResponse({ ok: true, token });
      } else if (msg.action === "CHECK_AUTH") {
        const token = await getStoredToken();
        if (!token) {
          sendResponse({ ok: true, loggedIn: false });
          return;
        }
        const user = await getUserInfo();
        if (!user) {
          await setStoredToken(null);
          sendResponse({ ok: true, loggedIn: false });
          return;
        }
        sendResponse({ ok: true, loggedIn: true, email: user.email, user });
      } else if (msg.action === "GET_USER_INFO") {
        const user = await getUserInfo();
        sendResponse({ ok: true, user });
      } else if (msg.action === "UPDATE_PROFILE") {
        const user = await updateProfile(msg.profile);
        sendResponse({ ok: true, user });
      } else if (msg.action === "CHECK_RESUME") {
        const exists = await checkResumeExists();
        sendResponse({ ok: true, exists });
      } else if (msg.action === "LOGOUT") {
        await setStoredToken(null);
        sendResponse({ ok: true });
      } else if (msg.action === "TAILOR_CURRENT_POST") {
        // Ask the content script (running in the active LinkedIn tab)
        // to read the post off the page.
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab || !tab.id) {
          sendResponse({ ok: false, error: "No active tab found." });
          return;
        }

        const pageResponse = await extractPostTextFromTab(tab.id);

        if (!pageResponse || !pageResponse.text) {
          sendResponse({ ok: false, error: "Could not find LinkedIn post text on this page. Open the LinkedIn post tab and try again." });
          return;
        }

        const result = await submitJobPost(pageResponse.text, pageResponse.url);
        sendResponse({ ok: true, result });
      } else if (msg.action === "GET_STATUS") {
        const result = await getTailoredOutput(msg.outputId);
        sendResponse({ ok: true, result });
      } else if (msg.action === "GET_GMAIL_STATUS") {
        const st = await getGmailStatus();
        sendResponse({ ok: true, ...st });
      } else if (msg.action === "CONNECT_GMAIL") {
        const r = await connectGmail();
        sendResponse({ ok: true, ...r });
      } else if (msg.action === "LIST_OUTPUTS") {
        const outputs = await listTailoredOutputs();
        sendResponse({ ok: true, outputs });
      } else if (msg.action === "APPROVE_OUTPUT") {
        const result = await approveOutput(msg.outputId);
        sendResponse({ ok: true, result });
      } else if (msg.action === "SEND_OUTPUT") {
        const result = await sendOutput(msg.outputId);
        sendResponse({ ok: true, result });
      } else if (msg.action === "DOWNLOAD_OUTPUT") {
        await downloadOutput(msg.outputId);
        sendResponse({ ok: true });
      }
    } catch (err) {
      sendResponse({ ok: false, error: err.message });
    }
  })();
  return true; // async response
});
