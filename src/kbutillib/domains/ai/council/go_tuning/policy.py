"""The mapping policy the GO adjudication council judges against.

:data:`MAPPING_POLICY` is a PROMPT FRAGMENT, not documentation.  It is spliced
into all three role prompts of ``go_mf_adjudicate@1`` and is the standard the
adjudicators are asked to apply, so its words are part of the question those
calls were asked under.  Three consequences follow, and they are why it lives
in a module of its own rather than inline in the YAML:

* **Every role sees the SAME text.**  A proposer judging against one wording
  and a critic auditing against another would disagree for a reason no one
  reading either reply could see.  One constant, three roles.
* **Editing it is a new query-type version, never an edit.**  ``go_mf``'s
  stored spec is immutable per ``(name, version)``, but the policy reaches the
  model through the renderer rather than through the stored spec, so a change
  here moves what was asked WITHOUT moving ``go_mf_adjudicate``'s version
  number or any ``prompt_hash``.  The test suite pins the text
  (``tests/.../test_go_mf_adjudicate_builtin.py``) for exactly that reason: a
  reworded rule must be a deliberate act with a new version beside it, not a
  silent re-identification of a decided corpus.
* **The rules are CITED BY NUMBER.**  Every role's answer carries a
  ``rules_cited`` / ``misapplied_rules`` array over ``P1``..``P6``, so the
  labels are machine-read and renumbering them would silently re-point every
  stored citation at a different rule.

Written by Jane for the ``go-mf-council-autoresearch-v1`` PRD (2026-11-16)
from the brief's Appendix A.  NOT derived from CONCORDIA V3: the V3 prompts
ask a council to MAP an annotation, and this asks a council to judge two
finished mappings against a written standard.
"""

from __future__ import annotations

__all__ = ["MAPPING_POLICY"]

#: The six mapping rules, verbatim, as the adjudicators are shown them.
#:
#: Do not reflow, renumber or "tidy" this string.  The two-space gap after
#: each label, the quoted example annotations and the ``P1-P3`` span in P4 are
#: all part of the text a model was shown.
MAPPING_POLICY = """MAPPING POLICY (cite rules by number):
P1  Map to the most specific function the annotation names. When the protein is a named component of a transporter, complex or system, map to that system's activity, not to the generic chemistry of the component. "Glucose ABC transporter, ATP-binding protein" maps to the glucose ABC transporter activity, not to ATP binding or ATP hydrolysis.
P2  When the annotation names a substrate, cofactor or target, prefer the term that names it over the parent term that does not.
P3  Generic terms (binding, ATP binding, catalytic activity, transporter activity, oxidoreductase activity and the like) are correct ONLY when the annotation names nothing more specific, or when no more specific term is on the menu.
P4  Pick from the candidate menu only. If the term P1-P3 call for is NOT on the menu, say so explicitly: that is a retrieval failure, and it must not be hidden by settling for a generic term.
P5  A string that states no function (a bare gene symbol, "hypothetical protein", "DUF1234") gets no terms.
P6  Extra terms must each be independently supported. A correct specific term plus an unsupported extra is worse than the specific term alone."""
