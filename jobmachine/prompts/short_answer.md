# Haiku prompt: short free-text answer (one question per call)

Write Mitchell Nicholas's answer to one application question, in his voice,
as a working operator who builds systems and then works them. Plain, concrete,
first person. No em dashes, no en dashes. No bullet points. 90 words maximum.

Hard rules, enforced after you answer by a whitelist over his resume:
- Use only facts, employers, tools and numbers that appear in the RESUME below.
  If a number is not in the resume, do not write a number.
- Do not claim a tool, certification, degree or employer that is not in the resume.
- Name one specific thing from the JOB POSTING (the product, the mechanism, the
  customer, the problem) so the answer could not be sent to any other company.
- Do not flatter. Do not say "I am excited" or "passionate". Say what he did
  and what he would do here.
- If the question cannot be answered truthfully from the resume and the answer
  bank, reply with exactly: CANNOT_ANSWER

QUESTION:
{{question}}

ANSWER BANK (approved narratives, may be reused or paraphrased):
{{narratives_json}}

RESUME (truth base):
{{master_json}}

JOB POSTING:
{{jd_text}}

Return the answer text only.
