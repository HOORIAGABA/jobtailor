"""
Generates templates/resume_template.docx — an ATS-safe Jinja-style
docxtpl template (see blueprint section 3.4 for the "why" behind these
layout choices: single column, plain headings, no tables/text
boxes/graphics).

Run once: `python -m app.rendering.build_template`
The output file is checked into the repo, so this only needs to be
re-run if you want to change the template's design.
"""
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
import os

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "resume_template.docx")


def build_template():
    doc = Document()

    # Page margins — reasonable, ATS-safe
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.7)
        section.right_margin = Inches(0.7)

    # Normal style
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    style.paragraph_format.space_after = Pt(4)

    # Heading styles — plain text headings, not graphics (ATS-safe)
    heading_style = doc.styles["Heading 2"]
    heading_style.font.name = "Calibri"
    heading_style.font.size = Pt(12)
    heading_style.font.bold = True
    heading_style.font.color.rgb = RGBColor(0, 0, 0)
    heading_style.paragraph_format.space_before = Pt(10)
    heading_style.paragraph_format.space_after = Pt(4)

    # Name — centered, bold
    name_p = doc.add_paragraph()
    name_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = name_p.add_run("{{ full_name }}")
    run.bold = True
    run.font.size = Pt(16)
    name_p.paragraph_format.space_after = Pt(2)

    # Contact — centered, smaller
    contact_p = doc.add_paragraph()
    contact_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_run = contact_p.add_run("{{ email }}{% if phone %} | {{ phone }}{% endif %}")
    contact_run.font.size = Pt(9.5)
    contact_p.paragraph_format.space_after = Pt(6)

    #
    # Summary
    #
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("{{ summary_heading }}")
    doc.add_paragraph("{{ summary }}")

    #
    # Skills
    #
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("Skills")
    doc.add_paragraph("{{ skills_line }}")

    #
    # Experience — one block per entry, title bold, bullets as proper list
    #
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("{{ experience_heading }}")
    exp_body = doc.add_paragraph(
        "{% for exp in experience %}"
        "{{ exp.title }}{% if exp.company %} — {{ exp.company }}{% endif %}"
        "{% if exp.dates %} ({{ exp.dates }}){% endif %}"
        "{% for b in exp.bullets %}\n{{ b }}{% endfor %}"
        "\n{% endfor %}"
    )

    #
    # Projects
    #
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("{{ projects_heading }}")
    proj_body = doc.add_paragraph(
        "{% for proj in projects %}"
        "{{ proj.title }}{% if proj.dates %} ({{ proj.dates }}){% endif %}"
        "{% for b in proj.bullets %}\n{{ b }}{% endfor %}"
        "\n{% endfor %}"
    )

    #
    # Certifications
    #
    cert_heading = doc.add_paragraph(style="Heading 2")
    cert_heading.add_run("Certifications")
    cert_heading_run = cert_heading.runs[-1]
    cert_heading_run.text = "{{ certifications_heading }}"
    cert_body = doc.add_paragraph(
        "{% for cert in certifications %}"
        "{{ cert.title }}{% if cert.company %} — {{ cert.company }}{% endif %}"
        "{% if cert.dates %} ({{ cert.dates }}){% endif %}"
        "\n{% endfor %}"
    )

    #
    # Education
    #
    p = doc.add_paragraph(style="Heading 2")
    p.add_run("{{ education_heading }}")
    edu_body = doc.add_paragraph(
        "{% for edu in education %}"
        "{{ edu.title }}{% if edu.company %} — {{ edu.company }}{% endif %}"
        "{% if edu.dates %} ({{ edu.dates }}){% endif %}"
        "\n{% endfor %}"
    )

    #
    # Leadership / community
    #
    lead_heading = doc.add_paragraph(style="Heading 2")
    lead_heading.add_run("{{ leadership_heading }}")
    lead_body = doc.add_paragraph(
        "{% for lead in leadership %}"
        "{{ lead.title }}{% if lead.company %} — {{ lead.company }}{% endif %}"
        "{% if lead.dates %} ({{ lead.dates }}){% endif %}"
        "{% for b in lead.bullets %}\n{{ b }}{% endfor %}"
        "\n{% endfor %}"
    )

    os.makedirs(os.path.dirname(TEMPLATE_PATH), exist_ok=True)
    doc.save(TEMPLATE_PATH)
    print(f"Template written to {TEMPLATE_PATH}")


if __name__ == "__main__":
    build_template()