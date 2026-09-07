/**
 * Runs inside the user's own authenticated LinkedIn tab. Read-only DOM
 * access to whatever the user can already see on screen — no scraping
 * beyond the page, no login automation, no auto-posting or auto-connecting.
 * See blueprint section 3.1 (Option B) for the design rationale.
 *
 * NOTE: LinkedIn's DOM structure changes periodically; the selectors
 * below are best-effort and may need occasional updates. This is a
 * known, documented maintenance tradeoff of scraping a live third-party
 * page's DOM rather than a stable API.
 */

function extractPostText() {
  const selectors = [
    '[data-test-id="main-feed-activity-card"]',
    ".feed-shared-update-v2",
    ".scaffold-finite-scroll__content article",
  ];

  for (const selector of selectors) {
    const el = document.querySelector(selector);
    if (el && el.innerText && el.innerText.trim().length > 0) {
      return el.innerText.trim();
    }
  }

  // Fallback: if we're on a single-post permalink page, the whole
  // main content area is usually just the post.
  const main = document.querySelector("main");
  return main ? main.innerText.trim() : null;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "GET_POST_TEXT") {
    sendResponse({
      text: extractPostText(),
      url: window.location.href,
    });
  }
  return true; // keep the message channel open for async sendResponse
});
