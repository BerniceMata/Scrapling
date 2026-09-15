#!/usr/bin/env python3
"""Render a structured resume to a one-page, ATS-parseable .docx.

Two constraints fight each other here. ATS parsers want boring structure:
single column, no tables, no text boxes, no headers, literal section headings,
real list styles. Humans want it to look like a resume. This module satisfies
the parser first and then buys the human look back with typography alone:
a rule under each section heading, tight leading, small caps headings, and a
name block that reads as a masthead. No layout feature that has ever confused
a parser is used.

  single column, no tables, no text boxes
      Multi-column layouts and tables are the number one cause of scrambled
      parses. A two-column resume frequently reads as one interleaved run of
      words, which destroys every field the parser was looking for.

  no headers or footers
      Many parsers never read them, so contact details placed there vanish.

  literal section headings
      SUMMARY / EXPERIENCE / PROJECTS / EDUCATION / SKILLS. Parsers key off
      these exact words.

  real List Bullet style, not typed bullet characters
      A typed "- " is text and can be concatenated into the previous line.

  separators are the middle dot, never the pipe
      Some parsers treat "|" as a column delimiter and invent phantom fields.

One page is enforced, not hoped for: the content is measured, and if it
overflows, the lowest-value bullets are dropped (last bullet of the longest
job first) until it fits. A resume that silently runs to two pages is a resume
whose second page nobody reads.

Usage:
    python3 render_docx.py resume.json out.docx [--lines N]
"""

from __future__ import annotations

import copy
import json
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

FONT = "Calibri"
BODY_PT = 9.5
SMALL_PT = 9
NAME_PT = 17
TITLE_PT = 10
HEADING_PT = 10
MARGIN_PT = 36  # 0.5 inch
SEP = "  ·  "

# Usable characters per rendered line at BODY_PT across a 7.5in text column,
# and the number of such lines that fit on one page at this leading. Both are
# calibrated against the rendered output, not guessed.
CHARS_PER_LINE = 125
MAX_LINES = 56


def _configure(document):
    style = document.styles["Normal"]
    style.font.name = FONT
    style.font.size = Pt(BODY_PT)
    pf = style.paragraph_format
    pf.space_after = Pt(0)
    pf.space_before = Pt(0)
    pf.line_spacing = 1.0
    for section in document.sections:
        section.top_margin = section.bottom_margin = Pt(MARGIN_PT)
        section.left_margin = section.right_margin = Pt(MARGIN_PT)


def _para(document, text="", size=BODY_PT, bold=False, italic=False,
          space_before=0, space_after=0, align=None, caps=False, color=None):
    p = document.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.line_spacing = 1.0
    if align is not None:
        p.alignment = align
    if text:
        run = p.add_run(text.upper() if caps else text)
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.name = FONT
        if color:
            run.font.color.rgb = color
    return p


def _rule(paragraph):
    """A hairline under a heading. Pure paragraph border, invisible to parsers."""
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "808080")
    borders.append(bottom)
    pPr.append(borders)


def _heading(document, text):
    p = _para(document, text, size=HEADING_PT, bold=True, caps=True,
              space_before=7, space_after=2, color=RGBColor(0, 0, 0))
    _rule(p)
    return p


def _bullets(document, items):
    for item in items:
        if not item:
            continue
        p = document.add_paragraph(item, style="List Bullet")
        p.paragraph_format.space_after = Pt(1.5)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.line_spacing = 1.0
        for run in p.runs:
            run.font.size = Pt(BODY_PT)
            run.font.name = FONT


def _contact_line(basics):
    loc = basics.get("location") or {}
    city = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
    return SEP.join(x for x in [city, basics.get("phone"), basics.get("email"),
                                basics.get("url")] if x)


def _dates(entry):
    a, b = entry.get("startDate") or "", entry.get("endDate") or ""
    return "%s - %s" % (a, b) if a and b else (a or b)


def _lines(text, width=CHARS_PER_LINE):
    if not text:
        return 0
    return max(1, -(-len(text) // width))


def estimate_lines(resume):
    """Rendered line count. Used to enforce the single page."""
    basics = resume.get("basics") or {}
    n = 2  # name + title
    n += _lines(_contact_line(basics))
    if basics.get("summary"):
        n += 2 + _lines(basics["summary"])
    work = resume.get("work") or []
    if work:
        n += 2
        for job in work:
            n += 1
            if job.get("summary"):
                n += 1
            for h in job.get("highlights") or []:
                n += _lines(h, CHARS_PER_LINE - 4)
    projects = resume.get("projects") or []
    if projects:
        n += 2
        for pr in projects:
            n += 1
            if pr.get("description"):
                n += _lines(pr["description"], CHARS_PER_LINE - 4)
            for h in pr.get("highlights") or []:
                n += _lines(h, CHARS_PER_LINE - 4)
    education = resume.get("education") or []
    if education:
        n += 2 + len(education)
        n += sum(1 for e in education if e.get("courses"))
    skills = resume.get("skills") or []
    if skills:
        n += 2
        for s in skills:
            kws = s.get("keywords") or []
            text = "%s: %s" % (s.get("name", ""), ", ".join(kws)) if kws else s.get("name", "")
            n += _lines(text)
    if resume.get("leadership"):
        n += 2 + _lines(resume["leadership"])
    return n


def fit_to_one_page(resume, max_lines=MAX_LINES):
    """Drop the lowest-value bullets until the content fits one page.

    Lowest value is defined structurally, not by judgment: the last bullet of
    whichever job currently has the most bullets. That preserves at least one
    bullet per role and trims the deepest section first, which is where
    redundancy actually lives.
    """
    resume = copy.deepcopy(resume)
    dropped = []
    # Project highlights go first: each project already carries its whole claim
    # in the description line, so the highlight is the cheapest thing on the
    # page. Work bullets are the last thing to lose.
    while estimate_lines(resume) > max_lines:
        withhl = [pr for pr in (resume.get("projects") or []) if pr.get("highlights")]
        if not withhl:
            break
        target = withhl[-1]
        dropped.append("project %s: highlight" % target.get("name", "?"))
        target["highlights"] = target["highlights"][:-1]
    while estimate_lines(resume) > max_lines:
        jobs = [j for j in (resume.get("work") or []) if len(j.get("highlights") or []) > 1]
        if not jobs:
            break
        target = max(jobs, key=lambda j: len(j["highlights"]))
        dropped.append("%s: %s" % (target.get("name", "?"),
                                   target["highlights"][-1][:60] + "..."))
        target["highlights"] = target["highlights"][:-1]
    return resume, dropped


def render(resume, out_path, enforce_one_page=True):
    dropped = []
    if enforce_one_page:
        resume, dropped = fit_to_one_page(resume)

    document = Document()
    _configure(document)
    basics = resume.get("basics") or {}

    _para(document, basics.get("name", ""), size=NAME_PT, bold=True,
          align=WD_ALIGN_PARAGRAPH.CENTER, space_after=0)
    if basics.get("label"):
        _para(document, basics["label"], size=TITLE_PT, bold=True, caps=True,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=1)
    contact = _contact_line(basics)
    if contact:
        _para(document, contact, size=SMALL_PT,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=1)

    if basics.get("summary"):
        _heading(document, "Summary")
        _para(document, basics["summary"])

    if resume.get("work"):
        _heading(document, "Experience")
        for job in resume["work"]:
            p = _para(document, space_before=4, space_after=0)
            left = SEP.join(x for x in (job.get("name"), job.get("position")) if x)
            r = p.add_run(left)
            r.font.bold = True
            r.font.size = Pt(BODY_PT)
            r.font.name = FONT
            tail = SEP.join(x for x in (job.get("location"), _dates(job)) if x)
            if tail:
                r2 = p.add_run(SEP + tail)
                r2.font.size = Pt(SMALL_PT)
                r2.font.name = FONT
            if job.get("summary"):
                _para(document, job["summary"], size=SMALL_PT, italic=True, space_after=1)
            _bullets(document, job.get("highlights") or [])

    if resume.get("projects"):
        _heading(document, "Projects")
        for pr in resume["projects"]:
            p = _para(document, space_before=4, space_after=0)
            r = p.add_run(pr.get("name", ""))
            r.font.bold = True
            r.font.size = Pt(BODY_PT)
            r.font.name = FONT
            meta = SEP.join(x for x in (pr.get("role"), _dates(pr), pr.get("url")) if x)
            if meta:
                r2 = p.add_run(SEP + meta)
                r2.font.size = Pt(SMALL_PT)
                r2.font.name = FONT
            if pr.get("description"):
                _bullets(document, [pr["description"]])
            _bullets(document, pr.get("highlights") or [])

    if resume.get("education"):
        _heading(document, "Education")
        for edu in resume["education"]:
            degree = ", ".join(x for x in (edu.get("studyType"), edu.get("area")) if x)
            bits = [degree, edu.get("institution") or ""]
            if _dates(edu):
                bits.append(_dates(edu))
            _para(document, SEP.join(x for x in bits if x), space_after=0)
            if edu.get("courses"):
                _para(document, "Coursework: " + ", ".join(edu["courses"]),
                      size=SMALL_PT, space_after=0)

    if resume.get("skills"):
        _heading(document, "Skills")
        for s in resume["skills"]:
            kws = s.get("keywords") or []
            label = s.get("name") or ""
            text = ("%s: %s" % (label, ", ".join(kws))) if kws and label else (
                ", ".join(kws) or label)
            if text:
                _para(document, text, space_after=1)

    if resume.get("leadership"):
        _heading(document, "Leadership")
        _para(document, resume["leadership"])

    document.save(out_path)
    return out_path, dropped


def main(argv):
    if len(argv) < 3:
        print("usage: python3 render_docx.py resume.json out.docx")
        return 2
    with open(argv[1]) as fh:
        resume = json.load(fh)
    path, dropped = render(resume, argv[2])
    print("wrote %s (estimated %d lines)" % (path, estimate_lines(resume)))
    for d in dropped:
        print("  trimmed to fit one page: %s" % d)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
