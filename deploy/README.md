# Remote deployment

Runs ApplAI and its MCP server on an always-on host, so Claude (web, iPhone, Desktop, scheduled tasks) can use them while the Mac sleeps. Claude does the scoring and writing through the claude.ai connector, and a Claude Code scheduled task replaces n8n.

```
claude.ai / iPhone / Desktop / scheduled task ─▶ Anthropic ─HTTPS─▶ Caddy mcp.saaddriouech.com ─▶ mcp:8080 (WorkOS JWT)
        sign-in (browser) ─▶ WorkOS AuthKit                                                       ├─X-API-Key─▶ applai-api:8000
Discord ─HTTPS─▶ Caddy hooks.saaddriouech.com/discord/interactions ─▶ applai-api:8000             └─read-only─▶ job_applications
Mac ◀── Syncthing over Tailscale ──▶ host job_applications/          Mac stdio applai-mcp ── tailnet ──▶ applai-api:8000
```

| Service | Exposed |
|---|---|
| `caddy` | 80 and 443 (tcp + udp) to the internet. Only `mcp.` and `hooks.` are served. |
| `mcp` | Nothing; Caddy reaches it on the compose network. Runs with `CONFIRMATION_MODE=token`. |
| `applai-api` | `127.0.0.1:8000`, shared to the tailnet with `tailscale serve` for the Mac's stdio server. |
| `syncthing` | Sync port on the Tailscale IP only; GUI on `127.0.0.1:8384`. |
| `n8n` | Not started (compose profile `n8n`). It stays on the Mac for rollback. |

Host layout: `/srv/applai/applai` (this repo), `/srv/applai/job_applications` (synced), `/srv/applai/backups` (DB backups, sent to the Mac). Containers run as UID 1000.

## 1. Server

**Oracle Cloud (primary).**
1. Sign up with an EU home region (it can't be changed later). If A1 capacity runs out in Frankfurt, try Amsterdam or Stockholm.
2. Upgrade to Pay-As-You-Go, which avoids idle reclaim, and create a budget alert at €1.
3. Create a VM: Ubuntu 24.04 aarch64, `VM.Standard.A1.Flex` with 2 OCPU and 12 GB, a 100 GB boot volume and your SSH key. Assign a reserved public IP.
4. In the VCN security list, allow ingress TCP 80 and 443 and UDP 443, plus TCP 22 from your IP for now.
5. Oracle's Ubuntu images reject inbound traffic in iptables. Open the web ports:
   ```bash
   sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT
   sudo iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

**Hetzner (fallback).** Create a CAX11 (ARM) with a Cloud Firewall allowing TCP 80/443, UDP 443 and, for now, TCP 22. Everything below is the same.

## 2. Tailscale

```bash
brew install --cask tailscale                                   # Mac
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up --ssh   # server
tailscale ip -4                                                 # server: note the 100.x address
```

Check that `ssh <server>` works over the tailnet, then remove port 22 from the security list or firewall.

## 3. Docker and the repo

Work on the server as a user with UID 1000. The containers run as UID 1000, so you share the folders and env files with them. On Ubuntu images that's usually `ubuntu`; check with `id -u`. If it isn't, create one with `sudo useradd --uid 1000 --create-home --groups sudo applai`, set a password, and continue as that user.

```bash
# Docker Engine and the Compose plugin from Docker's apt repository (docs.docker.com/engine/install/ubuntu)
sudo usermod -aG docker "$USER"     # log out and back in
sudo apt install -y unattended-upgrades
sudo install -d -o 1000 -g 1000 /srv/applai /srv/applai/job_applications /srv/applai/backups
git clone https://github.com/Saad-Driouech/applai.git /srv/applai/applai
mkdir -p /srv/applai/applai/data/db
```

The Syncthing container binds the Tailscale IP, so Docker must start after Tailscale. Run `sudo systemctl edit docker` and add:

```ini
[Unit]
After=tailscaled.service
Wants=tailscaled.service
```

## 4. DNS (Spaceship)

Add `A mcp` and `A hooks` pointing at the server's public IP, TTL 300. Don't add AAAA records, and leave the apex and `www` (Vercel) alone.

## 5. WorkOS

Follow "Remote connector (HTTP)" in [`mcp_server/README.md`](../mcp_server/README.md#remote-connector-http): production environment, passkeys or MFA, sign-ups off, DCR and CIMD on, the resource indicator `https://mcp.saaddriouech.com/mcp`. Then copy your user id (`user_…`) and the AuthKit domain.

## 6. Configuration

Copy the env files from the Mac over the tailnet only, logging in as the UID 1000 user:

```bash
scp config/.env <server>:/srv/applai/applai/config/.env
scp mcp_server/.env <server>:/srv/applai/applai/mcp_server/.env
```

On the server, edit `config/.env`:
- `APPLAI_WORKING_DIR=/srv/applai/job_applications` and `APPLAI_LOGS_DIR=/srv/applai/job_applications/logs`
- `APPLAI_CV_TEMPLATE=/mnt/job_applications/resumes/tex/applai/Saad_Driouech_CV_template.tex` and `APPLAI_CL_TEMPLATE` as on the Mac (container paths)
- `GOOGLE_AI_API_KEY=` and `ANTHROPIC_API_KEY=` empty: Claude does the LLM work, and ApplAI's own `score`/`generate` phases are disabled
- add `TAILSCALE_IP=<the 100.x address>`

Edit `mcp_server/.env`: keep `APPLAI_API_KEY`, and add `MCP_PUBLIC_URL=https://mcp.saaddriouech.com`, `AUTHKIT_ISSUER=https://<authkit-domain>` and `ALLOWED_USER_ID=user_…`. Compose sets the API URL, folder path and confirmation mode.

```bash
cd /srv/applai/applai
ln -s config/.env .env          # compose reads .env for ${...} interpolation, as on the Mac
chmod 600 config/.env mcp_server/.env
```

## 7. Syncthing

1. `brew install --cask syncthing` on the Mac. In its settings, turn off global discovery and relays.
2. Start only Syncthing on the server and open its GUI through an SSH tunnel:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d syncthing
   ssh -L 8384:127.0.0.1:8384 <server>      # then open http://localhost:8384
   ```
   Set a GUI password, and turn off global discovery and relays.
3. Pair the devices using the server address `tcp://<TAILSCALE_IP>:22000`.
4. Share `~/Documents/job_applications` from the Mac. On the server, accept it at `/var/syncthing/job_applications`. Before the first scan, set the same ignore patterns on **both** devices (they aren't synced):
   ```
   // Personal documents stay on the Mac
   /transcripts
   /certificates
   /*.pdf
   /*.png
   /*.jpg
   /profile_private.md
   // Written by each machine's own ApplAI
   /logs
   (?d).DS_Store
   (?d)~$*
   ```
   Only the root is filtered. `resumes/tex/applai/` (the CV template and its photo) and `.pending/` are synced.
5. On the server, add `/var/syncthing/backups` as a **Send Only** folder and share it with the Mac.
6. Wait until both sides show "Up to Date".

## 8. Cut-over

Pick a time when no phase is running.

1. On the Mac, take an online backup and stop the stack:
   ```bash
   docker compose exec applai-api python -c "import sqlite3; sqlite3.connect('/data/db/applai.db').backup(sqlite3.connect('/data/db/applai-migrate.db'))"
   docker compose stop
   scp data/db/applai-migrate.db <server>:/srv/applai/applai/data/db/applai.db
   ```
2. On the server, check and start it:
   ```bash
   cd /srv/applai/applai
   python3 -c "import sqlite3; print(sqlite3.connect('data/db/applai.db').execute('PRAGMA integrity_check').fetchone()[0])"   # ok
   docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d --build
   docker compose -f docker-compose.yml -f docker-compose.remote.yml ps   # all healthy
   KEY=$(grep '^APPLAI_API_KEY=' config/.env | cut -d= -f2-)
   curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8000/stats             # compare with the Mac
   ```
3. Discord Developer Portal: set the Interactions Endpoint URL to `https://hooks.saaddriouech.com/discord/interactions`. Discord sends a signed PING, so saving it confirms the route.
4. claude.ai: Settings → Connectors → Add custom connector, URL `https://mcp.saaddriouech.com/mcp`, client ID and secret empty. Connect and sign in at WorkOS.
5. Create the scheduled task (section 10).
6. On the Mac: `docker compose down`. Keep `data/db/applai.db` and the n8n volume, and copy the DB to `data/db/applai.db.pre-remote-<date>`.
7. Mac stdio server: on the server, share the API on the tailnet with `sudo tailscale serve --bg --tcp 8000 tcp://localhost:8000`. On the Mac, set `APPLAI_API_URL=http://<server>.<tailnet>.ts.net:8000` in `mcp_server/.env`. In Claude Desktop, enable either the stdio server or the connector, not both.

## 9. Backups

As the UID 1000 user, run `crontab -e` and add:

```
15 3 * * * /srv/applai/applai/scripts/backup-db.sh >> /srv/applai/backup.log 2>&1
```

[`scripts/backup-db.sh`](../scripts/backup-db.sh) takes an online SQLite backup through the running API container, checks it with `PRAGMA integrity_check`, writes it to `/srv/applai/backups` (synced to the Mac) and keeps the newest 14.

## 10. Scheduled task

At claude.ai/code/routines (the repo has to be on GitHub with `automation/` on the default branch):
- Repository: `applai`. Prompt: `Follow automation/scheduled_run.md`. Model: Sonnet.
- Schedule: `CRON_TZ=Europe/Berlin 7 8,20 * * *`.
- Connectors: **ApplAI only**.
- Click **Run now** once and compare its summary with `/stats`.

The playbook is [`automation/scheduled_run.md`](../automation/scheduled_run.md). It caps a run at 30 scores and 5 documents. Watch claude.ai/settings/usage in the first week.

## 11. Lock `/mcp` to Anthropic

After the connector works, restrict `/mcp` to Anthropic's outbound range. Discovery and `/healthz` stay open.

```bash
echo 'MCP_ALLOWED_CIDRS=160.79.104.0/21' >> config/.env
docker compose -f docker-compose.yml -f docker-compose.remote.yml up -d caddy
```

Then use the connector from the iPhone and run the scheduled task once. If either now gets 403, remove the line and run the same `up -d caddy` again.

## Checks

- `https://saaddriouech.com` and `www` are unchanged.
- `curl https://mcp.saaddriouech.com/healthz` returns 200, and `http://` redirects to `https://`.
- `curl -X POST https://mcp.saaddriouech.com/mcp` returns 401 with `resource_metadata` in `WWW-Authenticate`.
- `curl https://mcp.saaddriouech.com/.well-known/oauth-protected-resource/mcp` shows `"resource":"https://mcp.saaddriouech.com/mcp"` and the AuthKit issuer.
- `curl -H "Authorization: Bearer x" -X POST https://mcp.saaddriouech.com/mcp` returns 401.
- A second WorkOS test user can sign in at WorkOS but gets 401 from the server. Delete the user afterwards.
- `curl https://hooks.saaddriouech.com/stats` returns 404.
- An external port scan shows only 80 and 443, and SSH works only over the tailnet.
- Sync works both ways. Editing the same file on both sides while one is offline leaves a `.sync-conflict` copy.
- Discord Approve files the bundle on the server, and it appears on the Mac.
- The connector still works after an hour, and after `docker compose ... restart mcp`.

## Operations

```bash
alias dcr='docker compose -f docker-compose.yml -f docker-compose.remote.yml'
git pull && dcr up -d --build        # update
dcr logs -f mcp                      # token rejections are logged with the reason
dcr restart mcp                      # pending confirmation tokens are lost; ask again
```

## Rollback

1. Pause the scheduled task.
2. On the server, run `scripts/backup-db.sh`, then `dcr stop applai-api mcp`. Copy the newest backup to the Mac's `data/db/applai.db`.
3. On the Mac: `docker compose up -d`. n8n and ApplAI's own LLM path come back with the Mac's keys. Start the cloudflared tunnel and paste its URL into Discord.
4. Set the Mac's `APPLAI_API_URL=http://127.0.0.1:8000` in `mcp_server/.env` and disconnect the connector.

Folders need nothing, and the code needs no revert: the HTTP transport, the new tools and the optional LLM keys are all opt-in.
