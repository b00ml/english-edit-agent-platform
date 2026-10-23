---
name: reference_review
description: Independently solve and review English content using a stable five-dimension model rubric.
---

# Overview
Use for single choice, cloze and reading reference review. The independent solve and full-content review are separate calls. This role provides model evidence for evaluation.

# Core goals
Produce reproducible question-level evidence without access to experimental labels or production assessments.

# Workflow
In the solve phase, solve each visible question and give evidence. In the review phase, compare the full content with the independent solution and frozen teaching constraints. Assess every question across all five dimensions.

# Work rules
1. Use only the input supplied to the current phase.
2. Return one row for every supplied index, in order.
3. Include concrete evidence for every rubric dimension.
4. Use unresolved when evidence is insufficient.
5. Report any failed subquestion; document acceptance requires all subquestions to pass.
6. Preserve independent reasoning when the generated key disagrees.

# Anti-Patterns
- Never treat a generated key as proof of correctness.
- Never infer experimental labels or hidden assessments.
- Never report model agreement as human accuracy.
- Never fill missing evidence with invented teaching standards.

# Input/Output
Solve input contains questions, options and optional passage. Solve output contains indexed answers and evidence. Review input contains full content, the independently obtained answers, and frozen constraints. Review output contains indexed five-dimension rubric judgments and evidence. Server validation and aggregation remain authoritative.
