# mail-printer-print-agent

The shared print-agent that runs on the Raspberry Pi at home. It is the **only
process that touches the USB/ESC-POS printer**, and it is shared by several
Hetzner apps (mail-printer today, task-printer later) — see
[docs/decisions/0001-shared-print-agent.md](../../docs/decisions/0001-shared-print-agent.md).

## How it works

- One **outbound** WebSocket connection per app to that app's
  `wss://<domain>/ws/printer`, authenticated with that app's own bearer token.
  No inbound ports are opened at home.
- Each connection sends `{"type": "hello", "protocol_version": N}` first, then
  loops on `print` jobs and replies `ack` / `fail` **on that same connection**,
  which is what scopes job ids per app.
- All connections feed a single shared FIFO queue drained by a single printer
  worker task: **one job in flight overall**, in strict arrival order across
  apps. No per-app fairness logic.
- Each connection reconnects forever, independently, with exponential backoff
  plus full jitter. The backoff resets only after a connection that stayed up.
- Printing: decode the base64 PNG, print it, cut. If the image path fails for
  any reason, print the job's `fallback_text` as plain text instead; if that
  fails too, the job is nacked with `fail` and the server keeps it.

## Install and run

```sh
uv sync --package mail-printer-print-agent
uv run --env-file .env mail-printer-print-agent
```

## Environment variables

Read from the Pi's `.env` (gitignored; systemd loads it in production).

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `SERVER_WS_URL_<APP>` | — | that app's `wss://…/ws/printer` URL |
| `PRINTER_TOKEN_<APP>` | — | that app's shared secret (Bearer token) |
| `PRINTER_APPS` | all discovered | optional comma-separated allow-list of app names |
| `PRINTER_USB_VENDOR_ID` | `0x0483` | printer USB vendor id (hex) |
| `PRINTER_USB_PRODUCT_ID` | `0x5743` | printer USB product id (hex) |
| `PRINTER_PROFILE` | `TM-T20II` | python-escpos profile |
| `RECONNECT_MIN_SECONDS` | `1.0` | first backoff delay |
| `RECONNECT_MAX_SECONDS` | `60.0` | backoff ceiling |

`<APP>` is the app name upper-cased. Apps are discovered by scanning the
environment for `SERVER_WS_URL_*`; every URL must have a matching token or the
agent refuses to start. Only mail-printer needs to be configured for now:

```sh
SERVER_WS_URL_MAILPRINTER=wss://mail.example.com/ws/printer
PRINTER_TOKEN_MAILPRINTER=<long random secret, same value on the VPS>
```

The legacy un-suffixed pair `SERVER_WS_URL` / `PRINTER_TOKEN` (as it still
appears in the root `.env.example`) is still accepted and registered as the app
`mailprinter`, so an existing Pi `.env` keeps working.

## Tests

```sh
uv run pytest packages/print-agent
```

The agent tests run a real `websockets` server in-process on an ephemeral
localhost port and fake the printer, so neither the VPS nor hardware is needed.
Printing on the real printer (USB permissions, paper width, cut behaviour) can
only be verified on the Pi.
