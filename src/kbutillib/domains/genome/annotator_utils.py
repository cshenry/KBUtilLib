"""Convenience alias — canonical home: kbutillib.domains.genome.annotation.annotator_utils."""
from kbutillib.domains.genome.annotation.annotator_utils import *  # noqa: F401,F403
from kbutillib.domains.genome.annotation.annotator_utils import (  # noqa: F401
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
)

__all__ = [
    "AnnotatorUtils",
    "AnnotationRecord",
    "AnnotationResult",
    "Term",
    "ToolUnavailableError",
]
