**You can wake Chris's persistent agents on demand.** Neither is running right
now waiting for you; you cause one to start.

Two long-running agents front Chris's two machines. Each is a Claude session
launched on a schedule, with its own memory store and its own charter:

- **`luna` — primary-laptop.** Owns Chris's day: DocDB (his email, calendar,
  meetings and documents), the tiered task list, session records, briefings.
  This is the machine you are running on.
- **`miles` — h100.** Owns compute and infrastructure: GPU jobs, Maestro
  workers, and the h100 filesystem — none of which is reachable from here.

`persistentai trigger emit` writes an envelope into a Dropbox-synced inbox. A
poller on the target machine notices it within 5 minutes and launches a fresh
Claude session that does the **one task** your envelope names, then stops.

## What this is not

Read these before using it; every one of them has a wrong assumption attached.

- **Not a function call.** End-to-end latency is 3–15 minutes and you get no
  return value. `emit` returning success means *the file was written*, nothing
  more.
- **Not request/response.** The rail is deliberately one-way. Replies are
  opt-in and an originating envelope cannot ask for one, so **you cannot ask a
  question and receive an answer.** If you need something back, the answer is
  to tell Chris, not to fire a wake and wait.
- **Not an authorisation.** An envelope is a request from a peer. The woken
  agent stays inside its own charter and write scope, and will refuse anything
  outside it. Do not ask luna or miles to do something you were told not to do.
- **Not free.** Each envelope costs a paid Claude session on the other end.

Use it when the work genuinely belongs to the other agent or the other machine.
Anything you can do yourself with `Bash` here, do yourself.

## Firing one

The payload is a JSON file — never an argv string.

```bash
cat > /tmp/wake-payload.json <<'JSON'
{
  "subject": "One line naming the task",
  "task": "What you want done, concretely and completely.",
  "context": "Why, and anything the receiving session needs to act.",
  "origin": "KOROS session on primary-laptop, <what you were working on>"
}
JSON

PERSISTENTAI_MACHINE=primary-laptop persistentai trigger emit \
  --from koros \
  --to luna --to-machine primary-laptop \
  --task cowork \
  --payload-file /tmp/wake-payload.json
```

To wake Miles instead: `--to miles --to-machine h100`.

**`PERSISTENTAI_MACHINE` is required and you must set it yourself.** It names
the machine you are *sending from*, and the CLI refuses to guess it — without
it every `emit` fails with `PERSISTENTAI_MACHINE is not set`. Agents that run
on the rail get it from their own environment; a KOROS session does not
inherit it. You are on primary-laptop, so it is always
`PERSISTENTAI_MACHINE=primary-laptop`, whichever agent you are waking.

**`--to` and `--to-machine` are different axes and both must be right.** `--to`
is the agent name written *inside* the envelope; `--to-machine` is the
*directory* it is written to. The receiving poller reads its own machine's
directory and then filters on the agent name, so a mismatched pair produces a
file nobody ever reads and no error anywhere. The only valid pairs are
`luna`/`primary-laptop` and `miles`/`h100`.

`--task` is one of `notify` (FYI, no action needed), `cowork` (do this), or
`query`. **Prefer `cowork` or `notify`.** `query` is a label with no return
mechanism behind it — sending one does not get you an answer.

## Writing a payload that works

The woken session receives your payload **and nothing else**. It has no KOROS
transcript, no shared context with you, and no way to ask you a follow-up. So:

- Use **absolute paths** for every file you reference. Its working directory is
  not yours.
- State the task so it is complete on its own. If your payload needs a question
  answered to be actionable, the wake will be wasted.
- Say what "done" looks like, so it knows when to stop.
- Name yourself in `origin`. Otherwise the receiving agent cannot tell a KOROS
  request from its own scheduled work.

## Confirming, and what you cannot see

`emit` prints the `trigger_id` of the envelope it wrote. That is the whole of
your feedback. You can confirm the file exists:

```bash
ls ~/Dropbox/Projects/AIAssistant/trigger-inbox/{primary-laptop,h100}/
```

An envelope that has been picked up moves to that directory's `done/`
subdirectory. **You cannot observe whether the task was performed, or its
result.** Do not poll for one, and do not tell the user the work is finished —
tell them a wake was requested, and name the `trigger_id`.

## Rules

- **One envelope per subject.** Repeat firings on the same topic each cost a
  session and are treated as a loop that got past the depth cap.
- Envelopes carry a depth counter capped at 3. Do not try to work around it.
- `--from` and `--to` must differ; writing into your own inbox is refused.
- **This capability exists only on primary-laptop.** A KIND session
  running on the BERDL pod has neither `persistentai` nor the Dropbox-synced
  inbox, and there is no cross-machine substitute. If `persistentai` is not on
  `PATH`, say the capability is unavailable here rather than looking for
  another route.
