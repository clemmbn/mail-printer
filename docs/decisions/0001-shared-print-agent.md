# CLE-142: Shared print-agent on the Pi for multi-app printing

## Problem

`packages/pi` (as scoped by CLE-136/CLE-138) is mail-printer-specific: one
app, one WebSocket connection, one process holding the USB printer. The
goal is to have several Hetzner apps (mail-printer, task-printer, maybe
more later) all print to the same physical thermal printer at home. Two
processes both trying to open the same USB device conflict, so only one
process on the Pi may ever touch USB/ESC-POS.

## Decision

Rename `packages/pi` to **`packages/print-agent`** and generalise it to
accept WebSocket connections from multiple Hetzner apps instead of one.
It stays inside this repo (not a standalone sibling repo) — the workspace
already has one shared, dependency-free `protocol` package, and keeping
print-agent alongside it avoids a second repo/lockfile for what is still
a small amount of code.

- **Ownership**: `print-agent` is the only process that touches
  USB/ESC-POS. It is a dumb multiplexed relay, not a queue of record —
  each source app (mail-printer server, task-printer server, ...) keeps
  owning its own DB/queue/business logic server-side, exactly as already
  designed in CLE-135/CLE-138.
- **Auth**: one bearer token per app (`PRINTER_TOKEN_MAILPRINTER`,
  `PRINTER_TOKEN_TASKPRINTER`, ...), compared in constant time. The
  `hello` frame carries which app is connecting.
- **Job identity**: protocol job ids are namespaced per app (e.g. the
  agent tracks `(source, job_id)` pairs) so acks/fails from different apps
  never collide and always route back to the right app's connection.
- **Queueing across apps**: strict arrival order (FIFO), no per-app
  fairness logic. One job in flight at a time on the shared printer, same
  as the existing single-app design — just now the queue can interleave
  jobs from multiple app connections. Revisit only if one app's burst
  traffic becomes a real problem in practice.
- **task-printer**: stays on its existing bespoke Pi code
  (`../task-printer/src/task_printer/main.py`) for now. Porting it to
  this protocol is out of scope here and tracked as a separate follow-up
  issue, not blocking CLE-136/CLE-138.

## Consequences

- CLE-136 becomes: build `packages/print-agent` as a multi-client agent
  (multiple authenticated WS connections, one bearer token per app,
  namespaced job ids) instead of a single mail-printer-only client.
- CLE-138 becomes: `packages/server`'s `/ws/printer` client logic is
  unchanged in shape (still one outbound-from-Pi-style connection *from
  the agent's perspective per app*), but must include its app source in
  `hello` and use its own token. The "only one agent connection at a
  time" rule in CLE-138's original scope moves to the agent side (one
  connection **per app**, not one connection total).
- `packages/protocol` gains a `source` field (or equivalent) on `hello`
  and job ids, per the protocol description in the root `CLAUDE.md`,
  which has been updated accordingly.
- No new repo, no new lockfile. `mail-printer-pi` as an installable name
  is retired in favor of `mail-printer-print-agent` (or similar) — exact
  naming to be finalized when CLE-136 is implemented.
