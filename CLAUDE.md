# mail-printer

A small public website where anyone can send me a message that gets printed
on my thermal receipt printer at home. Visitors fill in a form (message,
optional name, optional photo); the message is rendered into a ticket image
and printed.

Ticket design (source of truth for the printed layout):
https://www.figma.com/design/Q11PCEFotPek15w3u9TSzF/Thermal-Printer?node-id=13-2
Layout (node 13:2): "New message" title centred on one line with a rule
either side of it → centred grey timestamp (`DD/MM/YYYY HH:MM`) → sender
name, indented → round avatar bottom-left + rounded speech bubble
containing the message → decorative "Reply ?" pill + send button (purely
visual, not functional). Icon artwork (Phosphor `paper-plane-tilt` for
the send button, `user-circle` for the avatar placeholder) lives in
`assets/`; the 96px PNGs are bundled with the server because Pillow
cannot read SVG. Designed at 1080px wide, printed at 576px.

## Architecture

```
Browser ──HTTPS──> Caddy ──> FastAPI server (Hetzner CX22)
                                │  SQLite (messages, bans, rate limits)
                                │  renders ticket PNG with Pillow
                                ▲
                                │ WebSocket (outbound from home, wss://…/ws/printer)
                                │ Authorization: Bearer <PRINTER_TOKEN_MAILPRINTER>
                                │
                       print-agent (Raspberry Pi Zero 2W at home) ──USB──> ESC/POS printer
```

`print-agent` is shared: other Hetzner apps (task-printer, maybe more
later) can hold their own outbound `/ws/printer` connection to the same
Pi process, each with its own bearer token, so only one process ever
owns the USB printer. See
[docs/decisions/0001-shared-print-agent.md](docs/decisions/0001-shared-print-agent.md)
for why. mail-printer only ever sees *its own* connection and queue —
the agent is a dumb multiplexed relay, not shared state mail-printer's
server needs to know about.

- **Server (VPS)**: FastAPI app. Serves the public form, the admin console,
  the submit API, and the printer WebSocket endpoint. Stores everything in
  SQLite. **Renders the ticket PNG server-side**, so the admin preview is
  exactly what gets printed and the Pi stays thin.
- **print-agent (home)**: opens an *outbound* WebSocket to the server (no
  port forwarding, no inbound exposure at home) for mail-printer's
  connection, authenticates with mail-printer's shared secret, receives
  print jobs, prints them, and acks/nacks each one. Reconnects forever
  with exponential backoff.
- **Queue semantics**: the DB is the queue. A message is `queued` until
  print-agent acks it (`printed`) or reports an error (`failed`). On every
  print-agent (re)connect, the server flushes all `queued` messages
  oldest-first. The site keeps accepting messages while print-agent is
  offline.
- **Run uvicorn with a single worker**: the live print-agent WebSocket
  connection is held in process memory; multiple workers would split it.

## Project structure

One git repo, **one uv workspace, three packages**. Each package lives on
exactly one machine (except `protocol`, which both share). Never import
server code from the print-agent package or vice versa: the only shared
code is `protocol`.

```
mail-printer/
├── pyproject.toml              # uv workspace root: no code, only [tool.uv.workspace]
│                               #   + dev tools (ruff, pytest) as a dev group
├── uv.lock                     # single lockfile for the whole workspace
├── CLAUDE.md  README.md  .env.example
│
├── packages/
│   ├── protocol/               # 📦 mail-printer-protocol  → BOTH machines
│   │   └── src/mail_printer_protocol/
│   │       └── messages.py     #   WebSocket message types + (de)serialisation
│   │                           #   STDLIB ONLY: must stay dependency-free
│   │
│   ├── server/                 # 📦 mail-printer-server    → HETZNER VPS
│   │   ├── pyproject.toml      #   deps: fastapi, uvicorn, jinja2, pillow,
│   │   │                       #         httpx (Turnstile), protocol
│   │   ├── src/mail_printer_server/
│   │   │   ├── main.py         #   app factory, router wiring, `mail-printer-server` entrypoint
│   │   │   ├── config.py       #   env var loading (secrets, limits, paths)
│   │   │   ├── db.py           #   SQLite schema + queries (messages, bans, rate limits)
│   │   │   ├── submit.py       #   public submit route: validation, Turnstile, bans, rate limits
│   │   │   ├── photos.py       #   upload sanitisation (verify, strip EXIF, crop, resize)
│   │   │   ├── ticket.py       #   Pillow ticket renderer (from task-printer) + preview CLI
│   │   │   ├── printer_ws.py   #   /ws/printer endpoint, connection state, queue flush
│   │   │   ├── admin.py        #   login, history, delete, reprint, ban, PDF export
│   │   │   ├── fonts/JetBrainsMono/
│   │   │   ├── templates/      #   Jinja2: index.html, admin/*.html
│   │   │   └── static/         #   css/, js/, img/ (default avatar), vendor/motion.js
│   │   └── tests/
│   │
│   └── print-agent/             # 📦 mail-printer-print-agent → RASPBERRY PI
│       ├── pyproject.toml      #   deps: websockets, python-escpos[usb], pillow, protocol
│       ├── src/mail_printer_print_agent/
│       │   ├── main.py         #   `mail-printer-print-agent` entrypoint: accepts one WS
│       │   │                   #     connection per app (per-app bearer token), serialises
│       │   │                   #     jobs across apps FIFO, reconnect/backoff per connection
│       │   └── printer.py      #   ESC/POS: lazy USB connect, print PNG, text fallback
│       └── tests/
│
├── deploy/
│   ├── hetzner/                # Caddyfile, mail-printer-server.service, deploy.sh
│   └── pi/                     # mail-printer-print-agent.service, sync.sh (rsync like task-printer)
│
└── data/                       # gitignored, runtime only (server): app.db, photos/, tickets/
```

**What runs where**

| | Hetzner VPS | Raspberry Pi |
|---|---|---|
| Package | `mail-printer-server` | `mail-printer-print-agent` |
| Install | `uv sync --package mail-printer-server` | `uv sync --package mail-printer-print-agent` |
| Run | `uv run mail-printer-server` (behind Caddy, systemd) | `uv run mail-printer-print-agent` (systemd) |
| Owns | web UI, admin, SQLite, rendering, rate limits, secrets for Turnstile/admin | the USB printer only |
| Network | public HTTPS on 443 | outbound `wss://` only, **no open ports** |
| Env | `.env` on the VPS | `.env` on the Pi (server URL(s), one printer token per app, USB IDs) |

Only the *server* renders tickets: print-agent receives a finished PNG
and prints it. So all design and layout work happens in
`packages/server`, and print-agent code should almost never change.

The old placeholder `src/mail_printer/` from `uv init` gets removed when
the workspace is scaffolded.

## WebSocket protocol (`packages/protocol`)

JSON text frames, defined once in `mail_printer_protocol.messages` and used
by both sides. Bump `PROTOCOL_VERSION` on any breaking change; the server
rejects a print-agent connection with a mismatching version.

print-agent accepts one connection per app (mail-printer, task-printer,
...), each authenticated with that app's own token. Job ids are scoped
per connection, so mail-printer's server only ever sees its own jobs —
see [docs/decisions/0001-shared-print-agent.md](docs/decisions/0001-shared-print-agent.md).

- **Connect**: print-agent → `wss://<domain>/ws/printer` (mail-printer's
  server), header `Authorization: Bearer <PRINTER_TOKEN_MAILPRINTER>`,
  first frame `{"type": "hello", "protocol_version": N}`. Each app's
  server exposes its own `/ws/printer`; print-agent holds one outbound
  connection per app.
- **Server → print-agent** `{"type": "print", "job_id": <message id>,
  "png_b64": "...", "fallback_text": "..."}`. `fallback_text` = timestamp +
  name + message, used if image printing fails.
- **print-agent → Server** `{"type": "ack", "job_id": …}` or
  `{"type": "fail", "job_id": …, "error": "..."}`.
- Keepalive via the WebSocket ping/pong built into both libraries.
- One job in flight at a time **per connection**: print-agent waits for
  ack/fail (with a timeout that marks the job `failed`) before sending the
  next job on that same connection. Jobs from different apps are
  interleaved in strict arrival (FIFO) order on the shared printer.

## Stack

- Python ≥3.11, managed with **uv** (`uv sync`, `uv run …`, `uv add
  --package <member> …`). Never use pip directly.
- **Server**: FastAPI + uvicorn, SQLite via the stdlib `sqlite3` (no ORM),
  Jinja2 templates, Pillow for rendering.
- **Frontend**: plain HTML + CSS + vanilla JS, **no build step, no framework**.
  Animations: CSS transitions/keyframes first; for sequenced/spring
  animations use [Motion](https://motion.dev)'s `animate()` vendored as a
  single ESM file under `static/vendor/` (no CDN at runtime). Respect
  `prefers-reduced-motion`.
- **Pi**: `websockets` client + `python-escpos[usb]` + Pillow (to decode the PNG).
- **Deployment**: Caddy (auto HTTPS) reverse proxy → uvicorn, managed by
  systemd on the Hetzner CX22. Custom domain points to the VPS.

## Reusing task-printer

`../task-printer` is a sibling project. Copy and adapt its code; don't
import from it:
- **→ `packages/server/.../ticket.py`**: the Pillow renderer
  (`src/task_printer/task_ticket.py`) and its bundled JetBrains Mono fonts
  (the font used in the Figma). Pure Pillow `ImageDraw`, 576px wide,
  `SCALE = 576 / 1080` to convert Figma px values, runnable directly to
  write a preview PNG without a printer.
- **→ `packages/print-agent/.../printer.py`**: the ESC/POS printing from
  `src/task_printer/main.py`: lazy printer connection, env-configured USB
  vendor/product ID and profile (default Epson TM-T20II), plain-text
  fallback if image printing fails.
- **→ `deploy/pi/sync.sh`**: the rsync deploy script and `.syncignore`.

## Features

### Public form
- `message` (required, 1–3000 chars, counted server-side after normalisation)
- `name` (optional, ≤40 chars): empty prints as **"Anonymous"**
- `photo` (optional, from gallery or camera via
  `<input type="file" accept="image/*" capture>`): printed in the avatar
  circle; Phosphor's `user-circle` icon is printed when absent
- Cloudflare Turnstile widget, verified server-side before anything else

### Admin console (`/admin`)
- Single password login (hash in env) → signed, HttpOnly, Secure,
  SameSite=Strict session cookie
- **History browser**: paginated list of all messages (newest first) with
  ticket preview, name, photo, timestamp, IP, status
  (`queued` / `printed` / `failed`)
- Actions: **delete** (removes the row and its stored images),
  **reprint as-is**, **ban IP**, **export to PDF**
- **Reprint as-is**: the rendered ticket PNG is stored at submit time and
  reprints send that exact stored image. Never re-render old messages, so
  later renderer or design changes don't alter history.
- **PDF export**: one message or a selection/all, one ticket per page, built
  from the stored PNGs with Pillow's PDF writer (no extra dependency)
- Shows whether print-agent is currently connected

## Safety requirements (non-negotiable)

- **Length**: 3000-char max on the message, enforced server-side (the client
  counter is only UX). Also cap the name length and total request body size.
- **Rate limits** (per IP, stored in SQLite so they survive restarts):
  - burst: max **5 messages per 10 minutes**
  - daily: max **50 messages per day**
  - When either is exceeded, return 429 with a `retry_after` (seconds) and a
    friendly message. The form then shows a **live countdown timer** and
    disables submit until it reaches zero.
- **Bot protection**: Cloudflare Turnstile token verified server-side on
  every submit.
- **IP bans**: banned IPs get rejected before any processing.
- **Client IP**: take it from the header set by Caddy (and `CF-Connecting-IP`
  if the domain is proxied through Cloudflare). Never trust
  client-supplied `X-Forwarded-For` directly.
- **Photo uploads**: cap size (e.g. 8 MB), verify with Pillow (not by
  extension/MIME), keep Pillow's decompression-bomb guard on, apply EXIF
  orientation then **strip all metadata**, re-encode, crop to square and
  resize small. Never serve or store the original bytes.
- **Text sanitisation**: Unicode-normalise (NFC), strip control characters,
  and replace glyphs JetBrains Mono can't render (emoji, etc.) rather than
  printing tofu boxes. Everything rendered in HTML is auto-escaped by Jinja2.
- **Printer WebSocket**: authenticated with a long random shared secret
  per app (env var), compared in constant time; reject any other
  connection. Only one print-agent connection at a time per app (a new
  one replaces the old one, logged).
- **Admin**: CSRF protection on state-changing actions, login attempt
  throttling.
- Secrets (Turnstile secret, admin password hash, session key, printer
  token) come from env vars / an `.env` file that is gitignored. Never commit
  them.

## Conventions

- Follow the global CLAUDE.md rules (heavy comments, lots of console
  logging, small functions, early returns). Use the stdlib `logging` module
  with a consistent format; log every submit, rejection reason, WS
  connect/disconnect, and print ack/fail.
- Keep it lightweight: no ORM, no JS framework, no task queue, no Redis.
  SQLite + one process is enough.
- Tests with `pytest` (`uv run pytest`). Cover validation, rate limiting,
  bans, image sanitisation, and the WS queue/ack flow. Rendering changes
  should be checked by generating a preview PNG and comparing to the Figma.
- Lint/format with `ruff` (`uv run ruff check`, `uv run ruff format`).
- Project tracking lives in Linear, in the **"Mail Printer"** project of team
  **CLE** ("Clemmbn personal work"). Check the issues before starting work and
  update them when a task is done. The standalone `linear` MCP server is
  connected to this workspace and can see the project directly.

## Open decisions

- How the photo is dithered for thermal print (Floyd–Steinberg vs
  threshold): decide after a test print. The fallback avatar is settled:
  Phosphor's `user-circle`.

## Data retention

Messages, photos, rendered tickets and IPs are kept indefinitely until
deleted from the admin console. Store images on disk under a gitignored
`data/` directory (paths in SQLite), not as DB blobs, so the DB stays small
and backups are easy.
