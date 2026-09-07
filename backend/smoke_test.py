"""Standalone smoke test: exercises the full pipeline in-process
(register -> upload resume -> submit job post -> tailored output ready).

REQUIRES a real LLM provider configured in backend/.env:
  - LLM_PROVIDER=groq + LLM_API_KEY=...  (free Groq tier works)
  - LLM_PROVIDER=openai_compatible + LLM_BASE_URL + LLM_API_KEY

CRITICAL: the test ISOLATES all state in a throwaway temp directory so it
can never touch the real backend/data (database, outputs)."""
import os
import sys
import tempfile
import time

_test_dir = tempfile.mkdtemp(prefix="jobtailor_smoke_")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_test_dir, "test.db")

# Verify a real LLM provider is configured
from app.config import settings
if settings.llm_provider == "mock":
    print("SKIPPED: LLM_PROVIDER=mock is no longer supported. Configure a real provider in backend/.env")
    sys.exit(0)

from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine
import docx
import io

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

client = TestClient(app)

print("=== 1. Health check ===")
r = client.get("/health")
assert r.status_code == 200, r.text
print(r.json())

print("=== 2. Register ===")
r = client.post("/auth/register", json={"email": "test@example.com", "password": "testpass123"})
assert r.status_code == 200, r.text
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
print("token acquired")

print("=== 3. Build sample resume docx in-memory ===")
d = docx.Document()
d.add_paragraph("Jane Doe - Software Engineer")
d.add_paragraph("jane@example.com")
d.add_paragraph("Experience:")
d.add_paragraph("Backend Engineer at Acme Inc, 2021-2024")
d.add_paragraph("Built REST APIs using FastAPI and PostgreSQL")
d.add_paragraph("Skills: Python, FastAPI, SQL, Docker")
buf = io.BytesIO()
d.save(buf)
buf.seek(0)

print("=== 4. Upload resume ===")
r = client.post(
    "/resumes/upload",
    headers=headers,
    files={"file": ("sample_resume.docx", buf, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
)
assert r.status_code == 200, r.text
resume = r.json()
print(resume)
resume_id = resume["id"]

print("=== 5. Submit a job post (simulating extension/paste capture) ===")
job_text = (
    "We are hiring a Backend Engineer. Required skills: Python, FastAPI, SQL. "
    "Experience with API design and Postgres is a plus."
)
r = client.post(
    "/job-posts",
    headers=headers,
    json={"raw_text": job_text, "source_type": "paste", "resume_id": resume_id},
)
assert r.status_code == 200, r.text
output = r.json()
print(output)
output_id = output["id"]

print("=== 6. Poll until the background pipeline finishes ===")
status = output["status"]
for _ in range(120):  # up to ~60s
    r = client.get(f"/tailored-outputs/{output_id}", headers=headers)
    assert r.status_code == 200, r.text
    result = r.json()
    status = result["status"]
    if status not in ("pending", "processing"):
        break
    time.sleep(0.5)
print(result)
assert status == "ready", f"expected ready, got {status}: {result}"

print("=== 7. Download generated resume (PDF) ===")
r = client.get(f"/tailored-outputs/{output_id}/download", headers=headers)
assert r.status_code == 200, r.text
out_path = os.path.join(tempfile.gettempdir(), "downloaded_tailored_resume.pdf")
with open(out_path, "wb") as f:
    f.write(r.content)
print(f"downloaded to {out_path}, size={len(r.content)} bytes")
assert r.content[:4] == b"%PDF", "expected a PDF payload"

print("=== 8. Approve output ===")
r = client.post(f"/tailored-outputs/{output_id}/approve", headers=headers)
assert r.status_code == 200, r.text
print(r.json())

print("\nALL SMOKE TESTS PASSED")
