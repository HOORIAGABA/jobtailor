"""Module 3.1 (Fetcher Agent) — fetches and extracts main content from a
job posting URL. For LinkedIn/auth-walled URLs, this will typically fail
or return junk; the caller should fall back to user-pasted text or the
Chrome extension capture path (see blueprint section 3.5)."""
import httpx
import trafilatura


def fetch_and_extract(url: str, timeout: float = 15.0) -> str | None:
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception:
        return None

    extracted = trafilatura.extract(resp.text)
    return extracted
