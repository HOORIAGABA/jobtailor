# -*- coding: utf-8 -*-
"""End-to-end parse of the Hooria Attas resume using the FIXED parser.

This exercises the full LLM parse + deterministic guards + contact fallback.
Check: location="Lahore, Pakistan", github populated, projects in projects
(not experience), skills flattened (no group headings).
"""
import json
import sys

sys.path.insert(0, r"E:\jobtailor\jobtailor\backend")

from app.agents.resume_parser_agent import parse_resume_text

RAW = """HOORIA ATTAS
City: Lahore | Country: Pakistan | Phone: +92 3036800002 | Email: hooria.gaba129@gmail.com | LinkedIn: www.linkedin.com/in/hooria-attas | GitHub: github.com/hooriaattas

AI/ML Engineer with hands-on experience in building and deploying production machine learning systems, multi-modal computer vision pipelines, and RAG-based applications. Skilled in end-to-end model development — from data preprocessing and training to ONNX/TensorRT deployment. Experienced in developing multi-agent systems and intelligent automations using LLMs, FastAPI, and n8n. Passionate about applying practical AI solutions to real-world problems.

SKILLS
Computer Vision & Image Processing
- YOLO (multi-modal / 4-channel)
- computer vision
- Transfert learning architectures (VGG16, VGG19, MobileNet V3, ResNet50, ...etc)
- Object detection
- Feature Extraction
- multi-modal RGB-Thermal pipelines
- Onnx
AI/ML Frameworks & Techniques
- PyTorch, TensorFlow, ONNX, TensorRT, OpenCV, Hugging Face
Backend & Production Pipelines
- FastAPI, Python, Docker, AWS
Automation & Integration
- n8n, LangChain, LangGraph, RAG pipelines

WORK EXPERIENCE
NexPred Solutions
Machine Learning Associate | [01/03/2026 – 13/08/2026]
- Designed and deployed a production multi-modal RGB-Thermal object-detection pipeline
- Optimized inference with ONNX/TensorRT
- Built FAISS-based RAG for document querying

Funsol Technologies
AI Automation engineer & Technical Project Coordinator | [01/12/2025 – 28/02/2026]
- Developed agentic automations for business workflows using LLMs, FastAPI, n8n

ITSOLERA pvt Ltd
Machine Learning Engineer | [24/06/2024 – 24/09/2024]
- Built ML models and REST APIs for document processing

PROJECTS
FBR Tax Assistant — WhatsApp RAG Chatbot for Pakistan Income Tax | [07/03/2026 – Current]
- Developed a WhatsApp chatbot using RAG to answer Pakistan income-tax queries

RGB-Thermal Aerial Object Detection Pipeline (2026) | [16/02/2026 – 25/05/2026]
- Built an aerial RGB-Thermal object detection pipeline

Agentic Resume Tailoring & Outreach System | [13/07/2026 – Current]
- Built the system you are reading about

Automated Team Productivity Tracker (n8n) | [01/12/2025 – 28/02/2026]
- n8n workflow to track team tasks

AI-Driven Malware Detection Desktop Application | [01/08/2024 – 01/07/2025]
- Built a malware-detection desktop app with CNNs

Automated Timetable Alarm System | [10/06/2025 – 19/12/2025]
- Built an automated class-schedule alarm system

EDUCATION
FAST-NUCES
BS Computer Science | [2022 – 2026]
- 3.67 GPA
"""


def _has_content(entry: dict) -> bool:
    parts = [str(entry.get(k, "")) for k in ("heading", "title", "degree", "field",
                                             "institution", "company", "dates", "location")]
    parts += [str(b) for b in (entry.get("bullets") or []) if b]
    return bool("".join(parts).strip())


def main():
    result = parse_resume_text(RAW)
    print("=== PARSED RESULT ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    print("\n=== DEFECT CHECK ===")
    projects = result.get("projects", [])
    experience = result.get("experience", [])
    project_titles = [(p.get("title") or "").strip().lower() for p in projects]
    exp_titles = [(e.get("title") or "").strip().lower() for e in experience]
    checks = {
        "location == Lahore, Pakistan": result.get("location") == "Lahore, Pakistan",
        "github populated": bool((result.get("github") or "").strip()),
        "no group-heading in skills": all(
            "&" not in s and "Frameworks" not in s and "Pipelines" not in s and "Techniques" not in s
            for s in result.get("skills", [])
        ),
        "skills extracted (>= 10)": len(result.get("skills", [])) >= 10,
        "summary captured": bool((result.get("summary") or "").strip()),
        "projects carry content (>= 4, at most one sparse entry)": (
            len(projects) >= 4
            and sum(1 for p in projects if (p.get("title") or "").strip() and (p.get("bullets") or []))
            >= len(projects) - 1
        ),
        "no project title leaked into experience": not any(
            pt and (pt in et or et and pt.split()[0] in et.split()[:3])
            for pt in project_titles for et in exp_titles
        ),
        "experience only has paid-work entries": all(e.get("company") for e in experience),
        "no fully-empty entries in any section": all(
            _has_content(x) for x in projects + experience
            + (result.get("education") or []) + (result.get("certifications") or [])
        ),
    }
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    ok = all(checks.values())
    print("\nOVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())