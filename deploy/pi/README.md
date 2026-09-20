# Raspberry Pi setup (mail-printer print-agent)

One-time setup for the Pi Zero 2W that owns the USB thermal printer. After
this, day-to-day updates are just `./deploy/pi/sync.sh`.

The Pi **never listens on a port**. It dials out to the server
(`wss://…/ws/printer`), so there is no port forwarding and nothing at home is
exposed to the internet.

## Files here

| File | Goes where | What it does |
|---|---|---|
| `sync.sh` | run from your laptop | rsync → `uv sync` → restart → verify |
| `.syncignore` | stays in the repo | rsync excludes (protects `.env`, `.venv/`) |
| `mail-printer-print-agent.service` | `/etc/systemd/system/` | Runs the agent, restarts always, starts on boot |
| `99-mail-printer-usb.rules` | `/etc/udev/rules.d/` | USB printer access without root |

## Placeholders to replace

| Placeholder | Where | Replace with |
|---|---|---|
| `clemmbn@raspzero.local` | `sync.sh` | your Pi's user@host |
| `/home/clemmbn/Desktop/Printer/mail-printer` | `sync.sh`, `.service` | your install path |
| `/home/clemmbn/.local/bin/uv` | `.service` `ExecStart` | `which uv` on the Pi |
| `0483` / `5743` | `99-mail-printer-usb.rules` | your printer's ids from `lsusb` |

---

## 1. Base Pi setup

Raspberry Pi OS Lite (64-bit) is plenty — no desktop needed.

```bash
# on the Pi
sudo apt update && sudo apt upgrade -y
sudo apt install -y git libusb-1.0-0 usbutils

# uv, per-user (matches the ExecStart path in the unit)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

Make sure the Pi joins your Wi-Fi automatically and that SSH is enabled
(`raspi-config`). Never use `pip` here; everything goes through `uv`.

## 2. Identify the printer

Plug the printer in, then:

```bash
lsusb
# Bus 001 Device 004: ID 0483:5743 ...
#                        ^^^^ ^^^^
#                        vendor product
```

Note both ids — they go in the udev rule *and* in `.env`.

## 3. USB access without root

```bash
sudo cp ~/Desktop/Printer/mail-printer/deploy/pi/99-mail-printer-usb.rules \
        /etc/udev/rules.d/99-mail-printer-usb.rules
# edit the ids in it if yours differ
sudo udevadm control --reload-rules
sudo udevadm trigger

# make sure the service user is in the lp group
sudo usermod -aG lp clemmbn
```

Then **unplug and replug the printer** — udev only applies rules to devices
added after the reload. Verify:

```bash
ls -l /dev/bus/usb/001/004     # should show group `lp`, mode crw-rw----
```

Group membership only takes effect in new sessions, so log out and back in
(or just reboot, which also tests the boot path).

## 4. First code sync

From your laptop:

```bash
PI_HOST=clemmbn@raspzero.local NO_RESTART=1 ./deploy/pi/sync.sh
```

`NO_RESTART=1` because the service does not exist yet. On a Pi Zero 2W the
`uv sync` step takes several minutes the first time — that is normal.

`sudo systemctl restart` is run by later syncs, so grant that one command:

```bash
# on the Pi
sudo tee /etc/sudoers.d/mail-printer >/dev/null <<'EOF'
clemmbn ALL=(root) NOPASSWD: /bin/systemctl restart mail-printer-print-agent, /bin/systemctl status mail-printer-print-agent
EOF
sudo chmod 440 /etc/sudoers.d/mail-printer
sudo visudo -c
```

## 5. Secrets (`.env`)

Lives **only** on the Pi, gitignored, never overwritten by `sync.sh`.

```bash
cd ~/Desktop/Printer/mail-printer
cp .env.example .env
chmod 600 .env
nano .env
```

Only these matter on the Pi:

| Variable | Value |
|---|---|
| `LOG_LEVEL` | `INFO` (use `DEBUG` while bringing the printer up) |
| `SERVER_WS_URL_MAILPRINTER` | `wss://mail.example.com/ws/printer` — your real domain |
| `PRINTER_TOKEN_MAILPRINTER` | **Exactly** the same value as in the VPS's `.env`. A mismatch is rejected at connect time and logged on the server |
| `PRINTER_USB_VENDOR_ID` | e.g. `0x0483` — from `lsusb`, matching the udev rule |
| `PRINTER_USB_PRODUCT_ID` | e.g. `0x5743` — same |
| `PRINTER_PROFILE` | `TM-T20II` (a python-escpos capability profile name) |

The server-side variables in `.env.example` (Turnstile, admin, `DATA_DIR`…)
are simply left blank here — the agent never reads them.

## 6. Install the service

```bash
sudo cp ~/Desktop/Printer/mail-printer/deploy/pi/mail-printer-print-agent.service \
        /etc/systemd/system/
# check the ExecStart uv path matches `which uv`
sudo systemctl daemon-reload
sudo systemctl enable --now mail-printer-print-agent
systemctl status mail-printer-print-agent
journalctl -u mail-printer-print-agent -f
```

You should see it connect and the server should report the agent as
connected in `/admin`.

## 7. Reboot test

The whole point is that the Pi recovers unattended from a power cut:

```bash
sudo reboot
# wait ~40s, then from your laptop:
ssh clemmbn@raspzero.local 'systemctl is-active mail-printer-print-agent'
# -> active
```

## 8. Day-to-day

```bash
./deploy/pi/sync.sh                    # update + restart
DRY_RUN=1 ./deploy/pi/sync.sh          # preview
ssh clemmbn@raspzero.local 'journalctl -u mail-printer-print-agent -f'
```

## Sharing the printer with other apps

This Pi runs **one** print-agent process that holds a separate outbound
connection per app (mail-printer, task-printer, …), each with its own bearer
token, so only one process ever owns the USB device. See
[`docs/decisions/0001-shared-print-agent.md`](../../docs/decisions/0001-shared-print-agent.md).
Practically: do not start a second agent process, and do not leave the old
task-printer service running against the same printer.

## Troubleshooting

- **`Access denied (insufficient permissions)`** — udev rule not applied, or
  the user is not in `lp`, or the printer was not replugged after the reload.
  Re-check step 3.
- **Agent reconnects in a loop** — token mismatch, wrong `SERVER_WS_URL_MAILPRINTER`, or
  a `PROTOCOL_VERSION` mismatch between the two machines. The server logs the
  exact rejection reason; the agent logs the close code.
- **Jobs ack but nothing prints** — printer out of paper or cover open. The
  agent's text fallback also fails silently in that state.
- **`uv run` fails at boot with a resolve error** — the unit uses `--no-sync`
  on purpose. Run `./deploy/pi/sync.sh` to install dependencies properly.
- **`.local` hostname does not resolve** — use the Pi's IP, or install
  `avahi-daemon` on the Pi.
