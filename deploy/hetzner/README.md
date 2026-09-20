# Hetzner VPS setup (mail-printer server)

One-time setup for the CX22 that runs the public site, the admin console and
the `/ws/printer` WebSocket endpoint. After this, day-to-day deploys are just
`./deploy/hetzner/deploy.sh`.

## Files here

| File | Goes where | What it does |
|---|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` | HTTPS + reverse proxy to uvicorn, body cap, client-IP header |
| `mail-printer-server.service` | `/etc/systemd/system/` | Runs `uv run mail-printer-server` as a non-root user |
| `mail-printer-backup.service` | `/etc/systemd/system/` | One-shot nightly backup job |
| `mail-printer-backup.timer` | `/etc/systemd/system/` | Schedules the backup at 03:15 |
| `deploy.sh` | run from your laptop | rsync → `uv sync` → restart → verify |
| `backup.sh` | stays in the repo on the VPS | SQLite `.backup` + images, rotated |

## Placeholders to replace

| Placeholder | Where | Replace with |
|---|---|---|
| `mail.example.com` | `Caddyfile`, `deploy.sh`, `backup.sh` comment | your real domain |
| `mailprinter` | both `.service` files, `deploy.sh` | the non-root user you create below |
| `/srv/mail-printer` | both `.service` files, `deploy.sh`, `backup.sh` | your install path |
| `127.0.0.1:8000` | `Caddyfile` | must match `HOST`/`PORT` in `.env` |

---

## 1. DNS

Point the domain at the VPS **before** starting Caddy — Caddy needs the
record to resolve in order to complete the ACME HTTP challenge and issue the
certificate.

- `A` record: `mail.example.com` → the VPS IPv4
- `AAAA` record: `mail.example.com` → the VPS IPv6 (optional but free)
- TTL: 300 while setting up, raise it later.

Verify from your laptop before continuing:

```bash
dig +short mail.example.com
```

If you plan to put Cloudflare in front (orange cloud), start with it
**grey** (DNS only) so Caddy can issue its own certificate, then read the
Cloudflare notes in the `Caddyfile` before turning the proxy on — the client
IP handling has to change at the same time or rate limits and bans will see
Cloudflare's IPs instead of visitors'.

## 2. Non-root user

```bash
# as root, on the VPS
adduser --disabled-password --gecos "" mailprinter
mkdir -p /srv/mail-printer
chown mailprinter:mailprinter /srv/mail-printer

# let your laptop's key reach that user
mkdir -p /home/mailprinter/.ssh
cp ~/.ssh/authorized_keys /home/mailprinter/.ssh/authorized_keys
chown -R mailprinter:mailprinter /home/mailprinter/.ssh
chmod 700 /home/mailprinter/.ssh
chmod 600 /home/mailprinter/.ssh/authorized_keys
```

`deploy.sh` runs `sudo systemctl restart` as this user, so grant exactly that
one command — no general sudo:

```bash
cat >/etc/sudoers.d/mail-printer <<'EOF'
mailprinter ALL=(root) NOPASSWD: /bin/systemctl restart mail-printer-server, /bin/systemctl status mail-printer-server
EOF
chmod 440 /etc/sudoers.d/mail-printer
visudo -c   # validate before logging out!
```

Then harden SSH (`/etc/ssh/sshd_config`): `PasswordAuthentication no`,
`PermitRootLogin prohibit-password`. `systemctl restart ssh` — and keep your
current session open until you have confirmed a new one works.

## 3. Firewall (ufw)

Only 22, 80 and 443 should ever be reachable. Note that uvicorn binds
`127.0.0.1`, so port 8000 is not exposed even without the firewall — but
belt and braces.

```bash
apt update && apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     comment 'SSH'
ufw allow 80/tcp     comment 'HTTP (ACME challenge + redirect)'
ufw allow 443/tcp    comment 'HTTPS'
ufw enable
ufw status verbose
```

Port 80 must stay open even though the site is HTTPS-only: Caddy uses it for
the ACME challenge and to redirect http→https.

If you later enable the Cloudflare proxy, replace the blanket 80/443 rules
with Cloudflare's published ranges so nobody can hit the origin directly and
forge `CF-Connecting-IP`:

```bash
ufw delete allow 80/tcp && ufw delete allow 443/tcp
for ip in $(curl -s https://www.cloudflare.com/ips-v4) $(curl -s https://www.cloudflare.com/ips-v6); do
  ufw allow from "$ip" to any port 443 proto tcp comment 'Cloudflare'
done
```

## 4. System packages

```bash
apt install -y curl git sqlite3 tar
# uv, installed system-wide so systemd finds it at /usr/local/bin/uv
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
uv --version

# Caddy (official apt repo)
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

> Never use `pip` on this box. Everything Python goes through `uv`.

## 5. First code sync

From your laptop:

```bash
REMOTE_HOST=mailprinter@mail.example.com ./deploy/hetzner/deploy.sh
```

The first run will fail at the restart step because the systemd unit does not
exist yet — that is expected. The code is now on the VPS; continue below.

## 6. Secrets (`.env`)

`.env` lives **only** on the VPS, is gitignored, and is never touched by
`deploy.sh`. Create it from the repo's `.env.example`:

```bash
# as mailprinter, on the VPS
cd /srv/mail-printer
cp .env.example .env
chmod 600 .env
nano .env
```

Where each value comes from:

| Variable | Source |
|---|---|
| `LOG_LEVEL` | `INFO` in production |
| `HOST` | `127.0.0.1` — uvicorn must not be publicly reachable; Caddy is the only door |
| `PORT` | `8000` — must match the `reverse_proxy` line in the `Caddyfile` |
| `DATA_DIR` | `data` (relative to `/srv/mail-printer`), or an absolute path |
| `PRINTER_TOKEN_MAILPRINTER` | Generate once: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. The **same** value goes in the Pi's `.env` |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | Cloudflare dashboard → Turnstile → add the domain → widget site key + secret key |
| `ADMIN_PASSWORD_HASH` | Hash of your admin password — generate with the hashing scheme the server uses (see `packages/server/.../admin.py`); never store the plaintext |
| `SESSION_SECRET` | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` — rotating it logs you out of the admin console |

Store all of these in your password manager too. If the VPS dies, the code
comes back from git but these do not.

## 7. Install the units and Caddy config

```bash
# as root
cp /srv/mail-printer/deploy/hetzner/mail-printer-server.service /etc/systemd/system/
cp /srv/mail-printer/deploy/hetzner/mail-printer-backup.service /etc/systemd/system/
cp /srv/mail-printer/deploy/hetzner/mail-printer-backup.timer   /etc/systemd/system/
systemctl daemon-reload

# dependencies, as the app user
sudo -u mailprinter bash -c 'cd /srv/mail-printer && uv sync --frozen --package mail-printer-server'

systemctl enable --now mail-printer-server
systemctl status mail-printer-server

# backups
mkdir -p /var/backups/mail-printer
chown mailprinter:mailprinter /var/backups/mail-printer
chmod +x /srv/mail-printer/deploy/hetzner/backup.sh
systemctl enable --now mail-printer-backup.timer
systemctl start mail-printer-backup.service   # test it once, now
systemctl list-timers mail-printer-backup.timer

# Caddy
cp /srv/mail-printer/deploy/hetzner/Caddyfile /etc/caddy/Caddyfile
# …edit the domain in it first!
caddy validate --config /etc/caddy/Caddyfile
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
systemctl reload caddy
```

Then check `https://mail.example.com` loads with a valid certificate.

## 8. Day-to-day

```bash
./deploy/hetzner/deploy.sh          # deploy
DRY_RUN=1 ./deploy/hetzner/deploy.sh  # preview changes only
ssh mailprinter@mail.example.com 'journalctl -u mail-printer-server -f'
```

## Restoring from a backup

```bash
systemctl stop mail-printer-server
cd /srv/mail-printer/data
tar -xzf /var/backups/mail-printer/mail-printer-YYYYMMDD-HHMMSS.tar.gz
chown -R mailprinter:mailprinter /srv/mail-printer/data
systemctl start mail-printer-server
```

The archived `app.db` is a complete, WAL-folded snapshot — no `-wal`/`-shm`
files are needed, and any stale ones in `data/` should be deleted first.

## Troubleshooting

- **Service flaps then stops** — `StartLimitBurst` tripped, usually a bad
  `.env`. `journalctl -u mail-printer-server -n 50`, fix, then
  `systemctl reset-failed mail-printer-server && systemctl start …`.
- **Cert not issued** — DNS not resolving yet, or port 80 blocked.
  `journalctl -u caddy -n 50`.
- **`/ws/printer` connects then drops** — check the token matches on both
  machines and that the protocol version is the same; the server logs the
  rejection reason.
- **Everyone appears to come from the same IP** — the client-IP header is
  wrong. See the Cloudflare block in the `Caddyfile`.
