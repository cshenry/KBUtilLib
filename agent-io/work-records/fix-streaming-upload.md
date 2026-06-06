# Work Record — fix-streaming-upload

## task_id
fix-streaming-upload

## branch
task-fix-streaming-upload

## commit_shas
- `495d16e04a524bb9cf9e0fc633f5ef4fca566e74`

## summary
Replaced the buffered `requests.post(..., files=...)` multipart body in both
`KBWSUtils.upload_blob_file` definitions in `kb_ws_utils.py` with a streaming
`requests_toolbelt.multipart.encoder.MultipartEncoder` POST. The old code caused
the entire file to be read into memory to build the multipart body (~2× file
size RSS), which fails silently on typical hosts for large genomics files. The
new implementation streams chunks through the encoder, keeping RSS flat. Added
`requests_toolbelt>=0.10.0` to `pyproject.toml` dependencies. Added a docstring
note to both definitions explaining the Cloudflare ~5–9 GB edge cap that still
limits uploads from outside the KBase network. Added a pytest in
`tests/test_upload_blob_file_streaming.py` covering both the call-signature
assertion (`data=MultipartEncoder`, no `files=`) and the peak RSS delta (<10 MB
for a 1 MB buffer).

## files_touched
- `src/kbutillib/kb_ws_utils.py` — added `MultipartEncoder` import; rewrote
  both `upload_blob_file` definitions to use `data=encoder` instead of
  `files=`; updated docstrings to note the Cloudflare edge cap.
- `pyproject.toml` — added `"requests_toolbelt >=0.10.0"` to `[project]
  dependencies`.
- `tests/test_upload_blob_file_streaming.py` — new test file with two tests:
  call-signature assertion and peak RSS delta assertion.
- `agent-io/work-records/fix-streaming-upload.md` — this file.

## success_criteria_check

| Criterion | Assessment | Justification |
|---|---|---|
| All `upload_blob_file` in `kb_ws_utils.py` use `MultipartEncoder`; no `requests.post(..., files=...)` remains | PASS | Both definitions converted; `grep files=` in the file shows only docstring text, not call sites. |
| `requests_toolbelt>=0.10.0` declared in `pyproject.toml` dependencies | PASS | Added on line 25 of `pyproject.toml`. |
| `pytest tests/test_upload_blob_file_streaming.py` passes; asserts peak RSS delta < 10 MB while streaming 1 MB | PASS | Both tests pass (0.14 s): `test_upload_blob_file_uses_multipart_encoder` and `test_upload_blob_file_peak_rss_under_10mb`. |

## tests_run

```
/tmp/kbutillib-test-venv/bin/python -m pytest \
  /Users/chenry/.maestro/worktrees/fix-streaming-upload/tests/test_upload_blob_file_streaming.py -v

platform darwin -- Python 3.11.14, pytest-9.0.3
collected 2 items

tests/test_upload_blob_file_streaming.py::test_upload_blob_file_uses_multipart_encoder PASSED
tests/test_upload_blob_file_streaming.py::test_upload_blob_file_peak_rss_under_10mb   PASSED

2 passed in 0.14s
```

Venv used: `/tmp/kbutillib-test-venv` (Python 3.11.14) with `requests 2.33.1`,
`requests_toolbelt 1.0.0`, `pytest 9.0.3`, and KBUtilLib installed in editable
mode from the worktree. The KBUtilLib-py3.13 venv is broken on this machine due
to a libexpat ABI mismatch; a temporary venv was created instead of modifying
the global pyenv site-packages.

## caveats
- The `requests_toolbelt` version installed in the test venv was 1.0.0 (satisfies
  `>=0.10.0`). The pyproject.toml floor of `>=0.10.0` is conservative; versions
  0.10.x through 1.x all expose the same `MultipartEncoder` API.
- The KBUtilLib-py3.13 venv at `~/VirtualEnvironments/KBUtilLib-py3.13` is
  currently broken due to a `libexpat` ABI mismatch on this macOS system. Tests
  were run in a fresh `/tmp/kbutillib-test-venv` (Python 3.11). The reviewer may
  want to rebuild the main venv.
- There are two `upload_blob_file` definitions in `kb_ws_utils.py` because the
  file contains two separate classes (one around line 159, one around line 838).
  Both were fixed independently rather than consolidated, to minimize the diff and
  avoid disturbing the class structure. Consolidation is a separate refactor
  concern.
- The streaming fix corrects memory usage for all file sizes. It does NOT enable
  large-file (>5–9 GB) upload from outside the KBase network — that still hits
  the Cloudflare edge cap. The docstrings note this explicitly.
