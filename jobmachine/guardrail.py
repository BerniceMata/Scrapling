#!/usr/bin/env python3
"""No-fabrication enforcement for tailored resumes.

Telling a model "do not invent experience" is a request, not a control. This is
the control: after tailoring, every load-bearing fact in the output must trace
back to the master resume. Anything that does not is a violation.

Ported from BerniceMata/simply-apply (AGPL-3.0) services/guardrail.py and
adapted: stdlib only (no pydantic, so it runs on a bare workbench), operates on
plain JSON Resume dicts, and adds the two jobmachine house rules that were
learned the expensive way - no em dashes, and the degree spelled in full.

What is checked, and why:

  employer / title / institution / degree / field
      The facts a recruiter verifies first, and the ones that get an offer
      rescinded. Must match a master-resume value exactly after normalization.

  dates
      Stretching an end date to close a gap is the most tempting single edit and
      the easiest to disprove.

  metrics
      "45+ signed agreements" becoming "60+" is fabrication even when every word
      around it is true. Every numeric token in the output must appear in the
      master.

  skills
      The one place tailoring legitimately surfaces things, but only things
      already present. A skill may be promoted from anywhere in the master (a
      bullet, a project line); it may not be introduced because the JD asked.

Bullet order, section order, phrasing and the summary are free. That is the
whole point of tailoring, and none of it asserts a new verifiable fact.

This is a whitelist over the master, not a blocklist of suspicious phrases. A
blocklist only catches fabrications someone anticipated; a whitelist catches
every one by construction, and its failure mode is a false positive (annoying)
rather than a false negative (career damage).

Usage:
    python3 guardrail.py master.json tailored.json
    exit 0 = clean, exit 1 = violations found (printed)
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata

# 40%, 1.2M, $500k, 3x, 12,000
_NUMERIC = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]\b|[xX]\b)?")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Numbers carrying no factual claim alone: years inside dates, incidental small
# counts. Flagging these is noise, not fabrication detection.
_TRIVIAL_NUMBERS = {str(n) for n in range(0, 11)}

# House rule: the degree must be spelled out. A bare "B.S." fails keyword
# screens. Mitchell paid for this lesson once.
REQUIRED_EDUCATION_PHRASES = ("bachelor of science", "healthcare studies")


def fold(value):
    """Lowercase, strip accents, collapse to alphanumerics and spaces."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", stripped.lower()).strip()


def key(value):
    return _NON_ALNUM.sub("", fold(value))


def _strip_trailing_zeros(number):
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number or "0"


def normalize_number(token):
    """`$1,200.00` equals `1200`; `40%` stays distinct from `40`."""
    token = token.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    if token.endswith("%"):
        return _strip_trailing_zeros(token[:-1]) + "%"
    for suffix in ("k", "m", "b", "x"):
        if token.endswith(suffix):
            return _strip_trailing_zeros(token[:-1]) + suffix
    return _strip_trailing_zeros(token)


def numbers_in(text):
    found = set()
    for match in _NUMERIC.finditer(text or ""):
        normalized = normalize_number(match.group())
        if normalized and normalized not in _TRIVIAL_NUMBERS:
            found.add(normalized)
    return found


def all_text(resume):
    """Every free-text field joined - the corpus a skill may be promoted from."""
    basics = resume.get("basics") or {}
    parts = [basics.get(f, "") for f in ("name", "label", "summary", "email", "url")]
    location = basics.get("location") or {}
    parts += [location.get("city", ""), location.get("region", "")]
    for job in resume.get("work") or []:
        parts += [job.get(f, "") for f in ("name", "position", "location", "summary")]
        parts += job.get("highlights") or []
    for edu in resume.get("education") or []:
        parts += [edu.get(f, "") for f in ("institution", "area", "studyType", "score")]
        parts += edu.get("courses") or []
    for skill in resume.get("skills") or []:
        parts += [skill.get("name", ""), skill.get("level", "")]
        parts += skill.get("keywords") or []
    for project in resume.get("projects") or []:
        parts += [project.get("name", ""), project.get("description", "")]
        parts += (project.get("highlights") or []) + (project.get("keywords") or [])
    return " ".join(p for p in parts if p)


class BaseFacts:
    """The set of things a tailored resume is allowed to assert."""

    def __init__(self, base):
        work = base.get("work") or []
        education = base.get("education") or []
        projects = base.get("projects") or []

        self.employers = {key(j["name"]) for j in work if j.get("name")}
        self.titles = {key(j["position"]) for j in work if j.get("position")}
        self.institutions = {key(e["institution"]) for e in education if e.get("institution")}
        self.degrees = {key(e["studyType"]) for e in education if e.get("studyType")}
        self.fields = {key(e["area"]) for e in education if e.get("area")}
        self.project_names = {key(p["name"]) for p in projects if p.get("name")}

        self.dates = set()
        for entry in list(work) + list(education) + list(projects):
            for field in ("startDate", "endDate"):
                if entry.get(field):
                    self.dates.add(key(entry[field]))

        text = all_text(base)
        self.corpus = fold(text)
        self.numbers = numbers_in(text)

        # Per-employer number sets, so a figure from one role cannot be used to
        # justify an inflated figure in another. Keyed on the folded employer
        # name; an employer absent from the master is caught by the employer
        # check itself, and falls back to an empty set here.
        self._numbers_by_employer = {}
        for job in work:
            if not job.get("name"):
                continue
            job_text = " ".join(
                [job.get("summary", "")] + (job.get("highlights") or [])
            )
            bucket = self._numbers_by_employer.setdefault(key(job["name"]), set())
            bucket.update(numbers_in(job_text))

    def numbers_for_employer(self, employer):
        return self._numbers_by_employer.get(key(employer), set())

    def mentions(self, value):
        """True if `value` appears anywhere in the master resume's text."""
        folded = fold(value)
        return bool(folded) and folded in self.corpus


def check(base, tailored):
    """Return every fact in `tailored` unsupported by `base`. Empty list = clean."""
    facts = BaseFacts(base)
    violations = []

    def flag(kind, value, where, detail):
        violations.append({"kind": kind, "value": value, "where": where, "detail": detail})

    unknown_employers = set()
    for i, job in enumerate(tailored.get("work") or []):
        where = "work[%d]" % i
        if job.get("name") and key(job["name"]) not in facts.employers:
            flag("employer", job["name"], where, "Employer is not in the master resume.")
            unknown_employers.add(i)
        if job.get("position") and key(job["position"]) not in facts.titles:
            flag("title", job["position"], where, "Job title is not in the master resume.")
        for field in ("startDate", "endDate"):
            value = job.get(field)
            if value and key(value) not in facts.dates:
                flag("date", value, "%s.%s" % (where, field), "Date is not in the master resume.")

    for i, edu in enumerate(tailored.get("education") or []):
        where = "education[%d]" % i
        if edu.get("institution") and key(edu["institution"]) not in facts.institutions:
            flag("institution", edu["institution"], where, "Institution is not in the master resume.")
        if edu.get("studyType") and key(edu["studyType"]) not in facts.degrees:
            flag("degree", edu["studyType"], where, "Degree is not in the master resume.")
        if edu.get("area") and key(edu["area"]) not in facts.fields:
            flag("field", edu["area"], where, "Field of study is not in the master resume.")
        for field in ("startDate", "endDate"):
            value = edu.get(field)
            if value and key(value) not in facts.dates:
                flag("date", value, "%s.%s" % (where, field), "Date is not in the master resume.")

    for i, project in enumerate(tailored.get("projects") or []):
        if project.get("name") and key(project["name"]) not in facts.project_names:
            flag("project", project["name"], "projects[%d]" % i, "Project is not in the master resume.")

    # Promoting a buried skill is the legitimate core of tailoring; introducing
    # one Mitchell never claimed is not. `name` is only a factual claim when it
    # stands alone - with keywords beneath it, it is a grouping label.
    for i, skill in enumerate(tailored.get("skills") or []):
        where = "skills[%d]" % i
        keywords = skill.get("keywords") or []
        if skill.get("name") and not keywords and not facts.mentions(skill["name"]):
            flag("skill", skill["name"], where, "Skill does not appear anywhere in the master resume.")
        for keyword in keywords:
            if keyword and not facts.mentions(keyword):
                flag("skill", keyword, "%s.keywords" % where,
                     "Keyword does not appear anywhere in the master resume.")

    # Metrics are scoped PER EMPLOYER, not document-wide. A document-wide set
    # lets any number legitimize itself anywhere: inflating HostraGroup's "45+
    # signed agreements" to "90+" passes a global check purely because a
    # "90-day audit" appears under a different employer. Scoping per entry
    # catches that cross-contamination, which is the exact fabrication class
    # that gets an offer rescinded.
    for i, job in enumerate(tailored.get("work") or []):
        # An unrecognized employer has no number bucket, so every figure under
        # it would flag. That is one root cause, already reported above; listing
        # its fallout as eight more metric violations just buries the signal.
        if i in unknown_employers:
            continue
        allowed = facts.numbers_for_employer(job.get("name", ""))
        job_text = " ".join(
            [job.get("summary", "")] + (job.get("highlights") or [])
        )
        for number in sorted(numbers_in(job_text) - allowed):
            flag("metric", number, "work[%d]" % i,
                 "Figure is not in this employer's entry in the master resume - "
                 "possible inflated metric or a number borrowed from another role.")

    # Everything outside the work section (summary, skills, education) still
    # checks against the whole master, where those numbers legitimately roam.
    outside = tailored.get("basics") or {}
    outside_text = " ".join([
        outside.get("summary", ""), outside.get("label", ""),
        " ".join(k for s in (tailored.get("skills") or [])
                 for k in ([s.get("name", "")] + (s.get("keywords") or []))),
        " ".join(c for e in (tailored.get("education") or [])
                 for c in (e.get("courses") or [])),
    ])
    for number in sorted(numbers_in(outside_text) - facts.numbers):
        flag("metric", number, "summary/skills/education",
             "Figure is not in the master resume - possible inflated or invented metric.")

    # House rule: no em dashes anywhere Mitchell-voiced. It is an AI tell.
    for source, label in ((all_text(tailored), "document"),):
        if "—" in source or "–" in source:
            flag("style", "em dash", label,
                 "Em or en dash found - replace with a comma, colon, or period.")

    # House rule: the degree stays spelled out or it fails keyword screens.
    education_text = fold(" ".join(
        "%s %s %s" % (e.get("studyType", ""), e.get("area", ""), e.get("institution", ""))
        for e in (tailored.get("education") or [])
    ))
    if education_text:
        for phrase in REQUIRED_EDUCATION_PHRASES:
            if phrase not in education_text:
                flag("style", phrase, "education",
                     "Education line must spell out '%s' - a bare abbreviation fails screens." % phrase)

    return violations


def summarize(violations, limit=20):
    """Feedback block, suitable for handing back on a retry."""
    lines = ["- %s %r at %s: %s" % (v["kind"], v["value"], v["where"], v["detail"])
             for v in violations[:limit]]
    if len(violations) > limit:
        lines.append("- ...and %d more." % (len(violations) - limit))
    return "\n".join(lines)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().rsplit("Usage:", 1)[-1].strip())
        return 2
    with open(argv[1]) as fh:
        base = json.load(fh)
    with open(argv[2]) as fh:
        tailored = json.load(fh)
    violations = check(base, tailored)
    if not violations:
        print("GUARDRAIL CLEAN: every fact in %s traces to %s" % (argv[2], argv[1]))
        return 0
    print("GUARDRAIL FAILED: %d violation(s)\n" % len(violations))
    print(summarize(violations, limit=100))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
