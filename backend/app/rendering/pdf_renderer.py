"""A4 PDF resume renderer (fpdf2) mirroring the DOCX ATS layout:
centered header (name, title, contact), then Summary -> Skills ->
Experience -> Projects -> Certifications -> Education -> Leadership,
using bold capitals for section headings and '-' bullet lines."""
import os
from pathlib import Path
from fpdf import FPDF

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "outputs")

ACCENT = (0x1F, 0x3A, 0x5F)
MUTED = (0x55, 0x55, 0x55)
INK = (0x1A, 0x1A, 0x1A)

_PUNCT = {
    "\u2014": "-", "\u2013": "-", "\u2022": "-", "\u00b7": "-", "\u2026": "...",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u20ac": "EUR", "\u00a3": "GBP", "\u2122": "(TM)",
    "\u00ae": "(R)", "\u00b0": " degree", "\u00b1": "+/-", "\u00d7": "x",
}


def _clean(text) -> str:
    if text is None:
        return ""
    out = []
    for ch in str(text):
        if ch in _PUNCT:
            out.append(_PUNCT[ch])
        elif ord(ch) < 32:
            continue
        elif ord(ch) < 0xFF:
            out.append(ch)
        else:
            out.append(" ")
    return " ".join("".join(out).split()).strip()


def _strip_company_dup(title: str, company: str | None) -> str:
    """Remove a trailing '- Company' / 'Company Company' duplication artifact."""
    t = _clean(title)
    c = _clean(company) if company else ""
    if c and t:
        if t.lower().endswith(c.lower()) and len(t) > len(c):
            t = t[: -len(c)].strip().rstrip(" -\u2013\u2014|").strip()
            t = " ".join(t.split())
    return t


class ResumePDF(FPDF):
    NAME = 18
    TITLE = 12
    SECTION = 12
    BODY = 10.5
    META = 9.5
    LINE_H = 5

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=12)
        self.set_margins(15, 12, 15)
        self._content_w = self.w - self.l_margin - self.r_margin
        self._font = "helvetica"
        self._setup_fonts()

    def _setup_fonts(self):
        calibri = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "calibri.ttf"
        if calibri.exists():
            try:
                self.add_font("Calibri", "", str(calibri))
                for style, name in (("B", "calibrib.ttf"), ("I", "calibrii.ttf"), ("BI", "calibriz.ttf")):
                    p = calibri.parent / name
                    if p.exists():
                        self.add_font("Calibri", style, str(p))
                self._font = "Calibri"
            except Exception:
                self._font = "helvetica"

    # ---- header ---------------------------------------------------------
    def header_line(self, text: str, size: float, bold: bool, color, h: float):
        if text.strip():
            self.set_font(self._font, "B" if bold else "", size)
            self.set_text_color(*color)
            self.cell(w=0, h=h, text=_clean(text), align="C", new_x="LMARGIN", new_y="NEXT")

    def render_header(self, contact_info: dict | None, resume_json: dict | None = None):
        self.add_page()
        contact_info = contact_info or {}
        resume_json = resume_json or {}
        full_name = contact_info.get("full_name") or resume_json.get("full_name") or "Your Name"
        self.header_line(full_name, self.NAME, True, INK, 9)
        if contact_info.get("title"):
            self.header_line(contact_info["title"], self.TITLE, False, MUTED, 6)
        parts = []
        for key in ("location", "phone", "email", "linkedin", "github"):
            val = (contact_info.get(key) or "").strip()
            if val:
                parts.append(val)
        if parts:
            self.header_line(" | ".join(parts), self.META, False, MUTED, 5)
        self.ln(3)

    # ---- sections -------------------------------------------------------
    def section(self, text: str):
        self.ln(2)
        self.set_font(self._font, "B", self.SECTION)
        self.set_text_color(*ACCENT)
        self.cell(w=0, h=6, text=_clean(text).upper(), new_x="LMARGIN", new_y="NEXT")
        y = self.get_y() + 1
        self.set_draw_color(*ACCENT)
        self.set_line_width(0.5)
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(2)

    def paragraph(self, text: str):
        self.set_font(self._font, "", self.BODY)
        self.set_text_color(*INK)
        self.multi_cell(w=0, h=self.LINE_H, text=_clean(text), new_x="LMARGIN", new_y="NEXT")

    def entry_line(self, title: str, company: str | None = None, dates: str | None = None):
        self.ln(2)
        title_c = _strip_company_dup(title, company)
        if title_c:
            self.set_font(self._font, "B", self.BODY)
            self.set_text_color(*INK)
            self.write(self.LINE_H, title_c)
        if company:
            self.set_font(self._font, "", self.BODY)
            self.write(self.LINE_H, "   " + _clean(company))
        if dates:
            self.set_font(self._font, "I", self.BODY)
            self.set_text_color(*MUTED)
            self.write(self.LINE_H, "   (" + _clean(dates) + ")")
        self.ln(self.LINE_H)

    def bullets(self, items):
        for b in items or []:
            b_c = _clean(b).strip()
            if not b_c:
                continue
            indent = self.l_margin + 3
            self.set_x(indent)
            self.set_font(self._font, "", self.BODY)
            self.set_text_color(*INK)
            self.multi_cell(w=self._content_w - 3, h=self.LINE_H, text="-  " + b_c,
                            new_x="LMARGIN", new_y="NEXT")

    def render_sections(self, tailored_json: dict):
        summary = (tailored_json.get("summary") or "").strip()
        if summary:
            self.section(tailored_json.get("summary_heading") or "Professional Summary")
            self.paragraph(summary)

        skills = tailored_json.get("skills") or []
        if skills:
            self.section("Skills")
            self.paragraph(", ".join(skills))

        experience = tailored_json.get("experience") or []
        if experience:
            self.section("Work Experience")
            for exp in experience:
                self.entry_line(exp.get("title", ""), exp.get("company"), exp.get("dates"))
                self.bullets(exp.get("bullets", []))

        projects = tailored_json.get("projects") or []
        if projects:
            self.section("Projects")
            for proj in projects:
                self.entry_line(proj.get("title", ""), proj.get("company"), proj.get("dates"))
                self.bullets(proj.get("bullets", []))

        certifications = tailored_json.get("certifications") or []
        if certifications:
            self.section("Certifications")
            for cert in certifications:
                self.entry_line(cert.get("title", ""), cert.get("company"), cert.get("dates"))
                self.bullets(cert.get("bullets", []))

        education = tailored_json.get("education") or []
        if education:
            self.section("Education")
            for edu in education:
                self.entry_line(edu.get("title", ""), edu.get("company"), edu.get("dates"))
                self.bullets(edu.get("bullets", []))

        leadership = tailored_json.get("leadership") or []
        if leadership:
            self.section("Leadership & Community")
            for lead in leadership:
                self.entry_line(lead.get("title", ""), lead.get("company"), lead.get("dates"))
                self.bullets(lead.get("bullets", []))

        extra_sections = tailored_json.get("extra_sections") or []
        for section in extra_sections:
            if not isinstance(section, dict):
                continue
            heading = section.get("heading", "")
            entries = section.get("entries", [])
            if heading and entries:
                self.section(heading)
                for entry in entries:
                    self.entry_line(entry.get("title", ""), entry.get("company"), entry.get("dates"))
                    self.bullets(entry.get("bullets", []))


def render_resume_pdf(tailored_json: dict, output_filename: str, contact_info: dict | None = None) -> str:
    """Render a tailored resume as an A4 PDF. Returns the absolute file path."""
    pdf = ResumePDF()
    pdf.render_header(contact_info, tailored_json)
    pdf.render_sections(tailored_json)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, output_filename)
    pdf.output(output_path)
    return output_path