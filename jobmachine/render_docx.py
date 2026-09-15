#!/usr/bin/env python3
"""Render a structured resume to an ATS-parseable .docx.

Ported from BerniceMata/simply-apply (AGPL-3.0) services/render_docx.py.

Every choice here exists because of how resume parsers actually fail, not
because of how the document looks:

  single column, no tables, no text boxes
      Multi-column layouts and tables are the number one cause of scrambled
      parses. A two-column resume frequently reads as one interleaved run of
      words, which destroys every field the parser was looking for.

  no headers or footers
      Many parsers never read them, so contact details placed there vanish.
      The contact line belongs in the body, at the top.

  literal section headings
      SUMMARY / EXPERIENCE / EDUCATION / SKILLS. Parsers key off these exact
      words. "What I Bring To The Table" is invisible to a machine.

  real List Bullet style, not typed bullet characters
      A typed "- " or U+2022 is text and can end up concatenated into the
      previous line. A styled list paragraph carries structure the parser reads.

  contact line joined with "  ·  ", never "|"
      Some parsers treat the pipe as a column delimiter and split the line into
      phantom fields.

Usage:
    python3 render_docx.py tailored.json out.docx
"""

from __future__ import annotations

import json
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

FONT = "Calibri"
BODY_PT = 10
NAME_PT = 16
HEADING_PT = 11
MARGIN_IN = 0.6
SEPARATOR = "  ·  "  # middle dot; never a pipe


def _configure(document):
    style = document.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(BODY_PT)
    style.paragraph_format.space_after = Pt(0)
    style.paragraph_format.space_before = Pt(0)
    for section in document.sections:
        section.top_margin = section.bottom_margin = Pt(MARGIN_IN * 72)
        section.left_margin = section.right_margin = Pt(MARGIN_IN * 72)


def _para(document, text="", size=BODY_PT, bold=False, space_before=0, space_after=0,
          align=None, caps=False):
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(space_before)
    paragraph.paragraph_format.space_after = Pt(space_after)
    if align is not None:
        paragraph.alignment = align
    if text:
        run = paragraph.add_run(text.upper() if caps else text)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = FONT
    return paragraph


def _heading(document, text):
    paragraph = _para(document, text, size=HEADING_PT, bold=True,
                      space_before=10, space_after=2, caps=True)
    for run in paragraph.runs:
        run.font.color.rgb = RGBColor(0, 0, 0)
    return paragraph


def _bullets(document, items):
    for item in items:
        if not item:
            continue
        paragraph = document.add_paragraph(item, style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(1)
        for run in paragraph.runs:
            run.font.size = Pt(BODY_PT)
            run.font.name = FONT


def _contact_line(basics):
    location = basics.get("location") or {}
    city = ", ".join(p for p in (location.get("city"), location.get("region")) if p)
    fields = [city, basics.get("phone"), basics.get("email"), basics.get("url")]
    return SEPARATOR.join(f for f in fields if f)


def _date_range(entry):
    start, end = entry.get("startDate") or "", entry.get("endDate") or ""
    if start and end:
        return "%s - %s" % (start, end)
    return start or end


def render(resume, out_path):
    document = Document()
    _configure(document)
    basics = resume.get("basics") or {}

    _para(document, basics.get("name", ""), size=NAME_PT, bold=True,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)
    contact = _contact_line(basics)
    if contact:
        _para(document, contact, align=WD_ALIGN_PARAGRAPH.CENTER, space_after=2)

    if basics.get("summary"):
        _heading(document, "Summary")
        _para(document, basics["summary"])

    work = resume.get("work") or []
    if work:
        _heading(document, "Experience")
        for job in work:
            # Employer and title on one line, dates on the same line at the end.
            # Kept as a single paragraph so the parser binds them to each other.
            header_bits = [b for b in (job.get("name"), job.get("position")) if b]
            header = SEPARATOR.join(header_bits)
            dates = _date_range(job)
            location = job.get("location") or ""
            tail = SEPARATOR.join(b for b in (location, dates) if b)
            paragraph = _para(document, space_before=6, space_after=1)
            run = paragraph.add_run(header)
            run.font.bold = True
            run.font.size = Pt(BODY_PT)
            run.font.name = FONT
            if tail:
                run2 = paragraph.add_run("  " + SEPARATOR.strip() + "  " + tail)
                run2.font.size = Pt(BODY_PT)
                run2.font.name = FONT
            if job.get("summary"):
                italic = _para(document, job["summary"], space_after=1)
                for run3 in italic.runs:
                    run3.font.italic = True
            _bullets(document, job.get("highlights") or [])

    education = resume.get("education") or []
    if education:
        _heading(document, "Education")
        for edu in education:
            # studyType and area spelled out in full: a bare abbreviation fails
            # keyword screens (guardrail.py enforces this too).
            degree_bits = [b for b in (edu.get("studyType"), edu.get("area")) if b]
            line_bits = [", ".join(degree_bits), edu.get("institution") or ""]
            dates = _date_range(edu)
            if dates:
                line_bits.append(dates)
            _para(document, SEPARATOR.join(b for b in line_bits if b), space_after=1)
            courses = edu.get("courses") or []
            if courses:
                _para(document, "Coursework: " + ", ".join(courses), space_after=1)

    skills = resume.get("skills") or []
    if skills:
        _heading(document, "Skills")
        for skill in skills:
            keywords = skill.get("keywords") or []
            if keywords:
                label = skill.get("name") or ""
                text = ("%s: %s" % (label, ", ".join(keywords))) if label else ", ".join(keywords)
            else:
                text = skill.get("name") or ""
            if text:
                _para(document, text, space_after=1)

    if resume.get("leadership"):
        _heading(document, "Leadership")
        _para(document, resume["leadership"])

    document.save(out_path)
    return out_path


def main(argv):
    if len(argv) != 3:
        print("usage: python3 render_docx.py tailored.json out.docx")
        return 2
    with open(argv[1]) as fh:
        resume = json.load(fh)
    path = render(resume, argv[2])
    print("wrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
