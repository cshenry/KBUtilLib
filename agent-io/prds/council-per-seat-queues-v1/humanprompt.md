# Per-seat queues and batched CLI calls for the council engine

The `function_to_go` council run on 19,276 items settled 4 items in 18 minutes
and projected 1,286 hours.
Nothing about the models was the problem.
One worker drained one shared FIFO for every seat at once, and its claim batch
was sized by the *sum* of every account's free capacity while being filled from
the *global* queue head with no per-account quota — so the fastest seat starved
behind the slowest, and the engine over-committed one account's budget by 8x.

This PRD splits the queue by **seat**: one worker process per council chair,
one for the combined critic/meta stage, each with its own concurrency budget
taken from a config file and supervised by the existing council daemon.

It also makes a CLI seat send **up to 20 items per invocation** instead of one.
Each `claude -p` call carries ~7,500 tokens of CLI harness before the ~300-token
question, so 96% of what we pay for today is boilerplate paid 38,552 times.
Batching 20 items cuts per-item input tokens 11.6x and leaves output tokens as
the dominant cost, which is the right place for them to be.

Three smaller fixes ride along, each one a measured cause of a real failure:
the Claude seat's prompt moves from a pipe to a file (23 of 24 attempts died on
a 3-second stdin timeout), the CLI temp root becomes configurable (it defaults
to `/tmp`, which fills on this shared host), and the attempt clock moves inside
the concurrency-slot acquire so that per-seat service time becomes measurable at
all — today every latency in the store is queue-wait plus service conflated, and
that is why two honest measurements of codex disagreed by 65x.

## What is NOT here

The council disagrees on essentially every item — 24 of the ~25 items whose
three chairs both settled went on to a critic, so the expensive Claude stage
fires ~100% of the time and `decided_by="unanimous"` never fires at all.
That is the largest single cost term in the whole pipeline and it is a query
design problem, not a throughput problem.
It belongs in its own design session against the `go_mf` query type.
