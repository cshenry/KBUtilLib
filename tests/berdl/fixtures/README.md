# `tests/berdl/fixtures`

## `AUTHORITATIVE-JOB-PROVENANCE.json`

A VERBATIM copy of `~/Dropbox/Jobs/function-to-go/AUTHORITATIVE-JOB-PROVENANCE.json`
as it stood on 2026-10-25 (written 2026-10-23, 7308 bytes, authored by the
agent that ran the jobs). It names which of the 21 jobs in the ADP1+PMI
council store constitute the published result: 12 `LOAD`, 9 `DO NOT LOAD`.

It is COPIED rather than read from Dropbox at test time on purpose. A test
that reads a Dropbox path passes or fails according to whether a sync client
has caught up on the machine running it, and this file is the seed for a
decision -- which jobs may be published -- that must be reproducible from the
repo alone.

It is INPUT, not a schema: nothing may hand-transcribe its job ids into code.
`council_disposition.read_provenance()` derives every disposition from the
file's own structure and refuses a shape it cannot classify.
