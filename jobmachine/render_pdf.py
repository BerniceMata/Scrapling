#!/usr/bin/env python3
"""Render a structured resume to a one-page PDF in Mitchell's own template.

The layout is copied from the resumes Mitchell already uses, measured off the
source PDFs rather than guessed:

  margins          38pt left and right, 32pt top
  name             bold, all caps, centred, 16pt
  contact line     centred, 9.5pt, " | " between fields
  section heading  bold, all caps, 10.5pt, left, with a black rule beneath
  competencies     one flowing paragraph joined by " • ", not labelled groups
  job line         "Company | Title" bold, then " | Location | Dates" regular,
                   all on one line, left aligned
  job context      italic, 9.5pt
  bullets          "• " with a hanging indent, and a bold lead-in phrase
  body             10pt

Pipes are used as separators here because that is what the template does and
it is what these documents have been submitted with. That overrides the
usual advice to avoid them.

One page is guaranteed mechanically, not hoped for. Body size steps down a
short ladder until the content provably fits inside the frame, and the
finished PDF is reopened and its pages counted. If it will not fit at the
smallest readable size, render() reports failure so the caller cuts content
rather than silently spilling onto a second page.

Bullet text may contain **bold** spans, which is how the template emphasises
the first phrase of a bullet.

Usage:
    python3 render_pdf.py resume.json out.pdf
"""

from __future__ import annotations

import json
import re
import sys

from reportlab.lib.colors import black
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

PAGE_W, PAGE_H = LETTER
REG, BOLD, ITAL = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 38, 32, 34
SIZE_LADDER = [10.0, 9.7, 9.4, 9.1, 8.8, 8.5]
LEAD = 1.19  # line height multiplier, matched to the template's density

_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")


def runs(text):
    """Split '**bold** then regular' into [(text, is_bold), ...]."""
    out, pos = [], 0
    for m in _BOLD_SPAN.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


def wrap_runs(text, size, width):
    """Wrap mixed bold/regular text, measuring each word in its own font.

    Tokens carry whether a space preceded them in the source, so punctuation
    that follows a bold span ("**Cami**, a production") stays attached to it
    instead of drifting off as its own word.
    """
    tokens = []  # (text, bold, space_before)
    first = True
    for chunk, is_bold in runs(text):
        parts = chunk.split(" ")
        for i, w in enumerate(parts):
            if not w:
                continue
            space_before = not first and (i > 0 or chunk.startswith(" "))
            tokens.append((w, is_bold, space_before))
            first = False
    space_w = stringWidth(" ", REG, size)
    lines, cur, cur_w = [], [], 0.0
    for w, b, sp in tokens:
        ww = stringWidth(w, BOLD if b else REG, size)
        add = ww + (space_w if (sp and cur) else 0)
        if cur and cur_w + add > width:
            lines.append(cur)
            cur, cur_w = [(w, b, False)], ww
        else:
            cur.append((w, b, sp))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines or [[]]


class Layout:
    def __init__(self, c, size, dry=False):
        self.c, self.s, self.dry = c, size, dry
        self.x = MARGIN_X
        self.w = PAGE_W - 2 * MARGIN_X
        self.y = PAGE_H - MARGIN_TOP

    def _draw_runs(self, line, x, y, size):
        space_w = stringWidth(" ", REG, size)
        for i, (w, b, sp) in enumerate(line):
            if sp and i:
                x += space_w
            font = BOLD if b else REG
            self.c.setFont(font, size)
            self.c.setFillColor(black)
            self.c.drawString(x, y, w)
            x += stringWidth(w, font, size)

    def para(self, text, size=None, indent=0, gap=0, font=None):
        size = size or self.s
        self.y -= gap
        for line in wrap_runs(text, size, self.w - indent):
            self.y -= size * LEAD
            if not self.dry:
                if font in (ITAL,):
                    self.c.setFont(ITAL, size)
                    self.c.setFillColor(black)
                    self.c.drawString(self.x + indent, self.y,
                                      " ".join(w for w, _, _ in line))
                else:
                    self._draw_runs(line, self.x + indent, self.y, size)
        return self

    def centered(self, text, font, size, gap=0):
        self.y -= gap
        self.y -= size * LEAD
        if not self.dry:
            self.c.setFont(font, size)
            self.c.setFillColor(black)
            self.c.drawCentredString(PAGE_W / 2, self.y, text)
        return self

    def heading(self, text):
        size = self.s + 0.5
        self.y -= self.s * 0.85
        self.y -= size * LEAD
        if not self.dry:
            self.c.setFont(BOLD, size)
            self.c.setFillColor(black)
            self.c.drawString(self.x, self.y, text.upper())
            self.c.setStrokeColor(black)
            self.c.setLineWidth(0.9)
            self.c.line(self.x, self.y - 3.0, self.x + self.w, self.y - 3.0)
        self.y -= 4.5
        return self

    def bullet(self, text):
        ind = self.s * 0.92
        lines = wrap_runs(text, self.s, self.w - ind)
        for i, line in enumerate(lines):
            self.y -= self.s * LEAD
            if not self.dry:
                if i == 0:
                    self.c.setFont(REG, self.s)
                    self.c.setFillColor(black)
                    self.c.drawString(self.x + 2, self.y, "•")
                self._draw_runs(line, self.x + ind, self.y, self.s)
        return self


def _dates(e):
    a, b = e.get("startDate") or "", e.get("endDate") or ""
    return "%s - %s" % (a, b) if a and b else (a or b)


def _contact(b):
    loc = b.get("location") or {}
    city = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
    return "  |  ".join(x for x in [city, b.get("phone"), b.get("email"),
                                    b.get("url")] if x)


def compose(L, resume):
    b = resume.get("basics") or {}
    L.centered(b.get("name", "").upper(), BOLD, L.s + 6.0)
    if _contact(b):
        L.centered(_contact(b), REG, L.s - 0.5, gap=2.0)

    if b.get("summary"):
        L.heading("Professional Summary")
        L.para(b["summary"])

    # Core competencies as one flowing line, exactly as the template does it.
    kws = [k for s in (resume.get("skills") or []) for k in (s.get("keywords") or [])]
    if kws:
        L.heading("Core Competencies")
        L.para("  •  ".join(kws))

    if resume.get("work"):
        L.heading("Professional Experience")
        for j in resume["work"]:
            head = "**%s**" % "  |  ".join(
                x for x in (j.get("name"), j.get("position")) if x)
            tail = "  |  ".join(x for x in (j.get("location"), _dates(j)) if x)
            L.para(head + ("  |  " + tail if tail else ""), gap=self_gap(L))
            if j.get("summary"):
                L.para(j["summary"], size=L.s - 0.5, font=ITAL)
            for h in j.get("highlights") or []:
                L.bullet(h)

    if resume.get("projects"):
        L.heading("Projects")
        for p in resume["projects"]:
            tail = "  |  ".join(x for x in (p.get("role"), _dates(p)) if x)
            L.para("**%s**" % p.get("name", "") + ("  |  " + tail if tail else ""),
                   gap=self_gap(L))
            if p.get("description"):
                L.bullet(p["description"])
            for h in p.get("highlights") or []:
                L.bullet(h)

    if resume.get("education"):
        L.heading("Education")
        for e in resume["education"]:
            deg = ", ".join(x for x in (e.get("studyType"), e.get("area")) if x)
            bits = [x for x in (deg, e.get("institution"), _dates(e)) if x]
            line = " - ".join(bits[:2]) + (", " + bits[2] if len(bits) > 2 else "")
            if e.get("courses"):
                line += "  |  Coursework: " + ", ".join(e["courses"])
            L.para(line, gap=1.5)

    if resume.get("leadership"):
        L.para("**Leadership:** " + resume["leadership"], gap=2.0)


def self_gap(L):
    return L.s * 0.55


def fits(resume, size):
    L = Layout(None, size, dry=True)
    compose(L, resume)
    return L.y >= MARGIN_BOT


def render(resume, out_path):
    """Render at the largest size that fits one page. Returns (path, size, pages)."""
    chosen = next((s for s in SIZE_LADDER if fits(resume, s)), None)
    if chosen is None:
        return None, None, None

    c = canvas.Canvas(out_path, pagesize=LETTER)
    c.setTitle("%s - Resume" % (resume.get("basics") or {}).get("name", ""))
    compose(Layout(c, chosen), resume)
    c.showPage()
    c.save()

    import pymupdf
    with pymupdf.open(out_path) as d:
        pages = len(d)
    return out_path, chosen, pages


def main(argv):
    if len(argv) != 3:
        print("usage: python3 render_pdf.py resume.json out.pdf")
        return 2
    with open(argv[1]) as fh:
        resume = json.load(fh)
    path, size, pages = render(resume, argv[2])
    if path is None:
        print("FAILED: does not fit one page even at %.1fpt. Cut content."
              % SIZE_LADDER[-1])
        return 1
    print("wrote %s at %.1fpt body" % (path, size))
    print("VERIFIED PAGE COUNT: %d" % pages)
    return 0 if pages == 1 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
