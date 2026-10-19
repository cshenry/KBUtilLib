"""Placeholder-function detection: descriptions that carry no function to map.

"hypothetical protein" is a legitimate abstention, not a request for the least
bad GO term -- yet a text ranker will always return *something*, with a score
that looks like a real one (go-text scores "hypothetical protein" ~0.59). So
:func:`function_to_go` abstains on these before ranking, and the caller does
not have to know the rule.

The rule is deliberately conservative: a description abstains only when,
after stripping bare identifiers (KO, COG, VOG, Pfam, TIGRFAM, InterPro, EC),
NOTHING but placeholder wording is left. "DUF1234 domain-containing protein"
abstains; "DUF1234 domain-containing ABC transporter permease" does not.
Bump :data:`PLACEHOLDER_RULES_VERSION` whenever the rule changes -- it is part
of the mapping hash.
"""

from __future__ import annotations

import re
from typing import Optional

PLACEHOLDER_RULES_VERSION = "placeholder-v1"

_IDENTIFIER = re.compile(
    r"\b(?:K\d{5}|COG\d{4}|VOG\d{3,6}|PF\d{5}|TIGR\d{5}|IPR\d{6}|"
    r"EC[: ]?\d+\.[\d-]+\.[\d-]+\.[\d-]+|\d+\.[\d-]+\.[\d-]+\.[\d-]+)\b",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[|;,/()\[\]{}]+")

#: Whole-string placeholder patterns, matched after identifiers are stripped.
_PLACEHOLDERS = [
    r"(?:conserved |putative |predicted |probable |possible )*(?:hypothetical|uncharacteri[sz]ed|unknown|unannotated) (?:conserved )?(?:protein|polypeptide|orf|gene product)(?: \w+)?",
    r"(?:conserved |putative )*(?:protein|gene product|polypeptide) of unknown function(?: \w+)?",
    r"(?:conserved |putative )*(?:domain|protein) of unknown function(?: \w+)?(?: (?:domain[- ]containing )?protein)?",
    r"(?:duf|upf)\d+(?: family)?(?: (?:domain[- ]containing )?protein)?(?: \w+)?",
    r"(?:conserved |putative )*(?:hypothetical|uncharacteri[sz]ed) (?:membrane|exported|secreted|lipo|periplasmic|cytoplasmic|phage|integral membrane) ?protein",
    r"(?:predicted|putative|conserved) protein",
    r"(?:protein|orf|gene)(?: \w+)?",
    r"(?:no|unknown|none|na|n/a|-)(?: function)?",
    r"",
]
_PLACEHOLDER_RE = re.compile(r"^(?:" + "|".join(_PLACEHOLDERS) + r")$")


def placeholder_reason(text: str) -> Optional[str]:
    """Return why ``text`` carries no mappable function, or ``None`` if it does."""
    stripped = _IDENTIFIER.sub(" ", text or "")
    stripped = _SEPARATORS.sub(" ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .-_:").lower()
    if not stripped:
        return "no text beyond identifiers"
    if _PLACEHOLDER_RE.match(stripped):
        return f"placeholder function: {stripped!r}"
    return None
