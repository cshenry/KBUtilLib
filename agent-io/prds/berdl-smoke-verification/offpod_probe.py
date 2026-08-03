"""Off-pod BERDL smoke probe — items 1, 3, 4 (local only, no network)."""
import os, sys, traceback

print("=" * 70)
print("ITEM 3: subpackage imports cleanly WITHOUT berdl_notebook_utils")
print("=" * 70)
try:
    import berdl_notebook_utils  # noqa
    print("  !! berdl_notebook_utils IS importable here — not a true off-pod env")
except ImportError as e:
    print(f"  OK  berdl_notebook_utils absent as expected: {e}")

mods = [
    "kbutillib.domains.kbase.berdl",
    "kbutillib.domains.kbase.berdl.tokens",
    "kbutillib.domains.kbase.berdl.naming",
    "kbutillib.domains.kbase.berdl.transports",
    "kbutillib.domains.kbase.berdl.capability",
    "kbutillib.domains.kbase.berdl.membership",
]
import importlib
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  OK  import {m}")
    except Exception as e:
        print(f"  FAIL import {m}: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("ITEM 1: token resolution off-pod (resolution only, no value printed)")
print("=" * 70)
from kbutillib.domains.kbase.berdl import tokens as T
print(f"  KBASE_AUTH_TOKEN in env: {'KBASE_AUTH_TOKEN' in os.environ}")
print(f"  token file: {T.DEFAULT_TOKEN_FILE} exists={T.DEFAULT_TOKEN_FILE.exists()}")
try:
    raw = T.DEFAULT_TOKEN_FILE.read_text().strip()
    print(f"  token file stripped length: {len(raw)} chars")
except OSError as e:
    raw = ""
    print(f"  token file unreadable: {e}")
tok = T.resolve_token()
print(f"  resolve_token() -> {'a value of length %d' % len(tok) if tok else 'None'}")
try:
    T.require_token()
    print("  require_token() -> succeeded (a token was resolved)")
except T.NoTokenAvailableError as e:
    print(f"  require_token() -> NoTokenAvailableError: {e}")

# Does the OLD KBBERDLUtils path resolve? (design-time gap: raised 'No KBase token available')
print()
print("  -- legacy KBBERDLUtils path --")
try:
    from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils
    u = KBBERDLUtils()
    try:
        h = u._get_headers()
        print(f"  OK  KBBERDLUtils._get_headers() returned headers: keys={sorted(h)}")
    except Exception as e:
        print(f"  FAIL KBBERDLUtils._get_headers(): {type(e).__name__}: {e}")
except Exception as e:
    print(f"  FAIL constructing KBBERDLUtils: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("ITEM 4: load() refuses correctly off-pod, against the REAL env")
print("=" * 70)
from kbutillib.domains.kbase.berdl.capability import BerdlCapability
cap = BerdlCapability()
print(f"  locus() -> {cap.locus()!r}")
try:
    cap.load(dataset="smoke_scratch", tables=[{"name": "t1"}])
    print("  !! FAIL: load() did NOT raise off-pod")
except Exception as e:
    print(f"  raised {type(e).__name__}")
    msg = str(e)
    print("  --- message ---")
    for line in msg.splitlines():
        print("  | " + line)
    print("  --- checks ---")
    for label, needle in [
        ("names the pod requirement (Spark)", "Spark"),
        ("names kbhub", "kbhub"),
        ("states nothing was staged", "staged"),
        ("gives a concrete command", "python -c"),
        ("echoes the dataset name", "smoke_scratch"),
    ]:
        print(f"    {'OK ' if needle.lower() in msg.lower() else 'MISS'}  {label}")
