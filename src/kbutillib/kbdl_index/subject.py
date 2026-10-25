"""Subject/identity helpers for KBDL job results."""
from __future__ import annotations

import re

PREFIX = re.compile(r'^(.*?)_\d+$')


def locus_prefix(result):
    """Shared locus-tag prefix of an annotation result's feature keys, or None."""
    if not isinstance(result, dict):
        return None
    for key in result:
        m = PREFIX.match(str(key))
        if m:
            return m.group(1)
    return None
