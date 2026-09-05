# Installing MeshTech-Bot

Simple, step-by-step instructions for running the bot on a Linux machine
(Debian / Ubuntu / Raspberry Pi OS) or in Docker. The bot connects over TCP
to the **companion endpoint** of an openHop Repeater on your network.

> **A note for the very first setup:** every option below ends with the bot
> running as a service that survives reboots — you do not need to start it
> by hand again. Re-running the installer later is how you update.

---

## 0. What you need

- A **Linux machine** (Debian/Ubuntu/Raspberry Pi OS) or any machine with
  Docker — Raspberry Pi 3 or newer is plenty.
- Your **openHop Repeater** reachable over your network, with a **companion
  identity enabled** (see step 1).
- One mesh radio/node so you can test the bot afterwards (and know your
  node's public-key prefix to become the admin).

---

## 1. Prepare the openHop Repeater (one time, on the repeater)

On the machine running openHop, its config usually lives at
`/etc/openhop_repeater/config.yaml`. Give the bot its own companion
identity (an openHop companion exposes a plain TCP port for clients):

```yaml
mesh:
  companions:
    - name: "BotCompanion"
      identity_key: "<a fresh companion identity key hex>"
      settings:
        node_name: "meshtech-bot"
        tcp_port: 5000
```

Then restart the repeater:

```bash
sudo systemctl restart openhop-repeater
```

**Notes**

- One TCP client may connect per companion — the bot is that one client.
- Port `5000` above is an example; whatever `tcp_port` you choose must match
  `connection.port` in the bot's `config.yaml`.
- If the bot machine and the repeater are the same box, the dashboard and
  bot can still run — they are separate ports.

---

## 2. Get the code from GitHub

The project lives at **https://github.com/mygooglyeyes/MeshTech-Bot**. The
examples below install it into `/opt/meshtech-bot` — a **system-owned**
folder — so the clone (and later updates) need `sudo`:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv   # python3-venv not needed for Docker

sudo git clone https://github.com/mygooglyeyes/MeshTech-Bot.git /opt/meshtech-bot
cd /opt/meshtech-bot
```

Notes:

- **HTTPS, not SSH, is used on purpose.** With `sudo`, git runs as the root
  account, which has no SSH keys of its own — an SSH clone would fail, while
  HTTPS needs nothing extra.
- **Private repo?** If the repo is set to *Private* (as published), the
  clone will ask for your GitHub **username** and a **personal access
  token** — *not* your GitHub password. Create one at GitHub → *Settings* →
  *Developer settings* → *Personal access tokens* → *Generate new token*
  (classic) and tick the `repo` scope. If you later make the repo *Public*,
  the prompt disappears.
- Cloned your own fork instead? Swap the URL above for your fork's.

The folder `/opt/meshtech-bot` is where the bot will live — that is the
**install folder**. You may clone anywhere you like, but only `/opt` paths
need the `sudo`.

> **Already have a config.yaml or data/ in the folder from testing?** They
> are git-ignored and stay local — cloning fresh simply skips them.

Now choose **one** install path:

- [Option A — Native Linux + systemd (recommended)](#option-a--native-linux--systemd-recommended)
- [Option B — Native Linux, no virtual environment](#option-b--native-linux-without-a-virtual-environment)
- [Option C — Docker](#option-c--docker)

---

## Option A — Native Linux + systemd (recommended)

The installer creates a dedicated **`meshtech`** service account, installs
the Python packages into a private environment inside the folder, copies
`config.example.yaml` to `config.yaml`, fixes all file/group permissions so
only the `meshtech` account can touch the data, and registers a systemd
service that starts the bot at every boot and restarts it if it crashes.

**Step A1 — create and edit your config**

`config.yaml` is **not** shipped in the GitHub repo (it would leak your
passwords and keys) — you make it from the example template:

```bash
cd /opt/meshtech-bot
sudo cp config.example.yaml config.yaml
sudo nano config.yaml
```

Save with `Ctrl+O`, exit with `Ctrl+X`. Set at least:

| Setting | What goes there |
|---|---|
| `connection.host` | LAN IP of the openHop Repeater machine |
| `connection.port` | the companion `tcp_port` from openHop's config (the example says 5000 — use whatever YOUR repeater actually uses) |
| `channels` | the `#channel` names to listen on; `reply: false` = log only |
| `dm.admin_pubkey_prefixes` | YOUR node's 12-hex prefix (run `!nodes` later, or see the dashboard) |
| `web.password` | a real password for the dashboard |

**Step A2 — run the installer**

```bash
cd /opt/meshtech-bot
sudo ./install.sh
```

The installer creates the `meshtech` account, installs the Python
dependencies, checks your config, and starts the service at boot.

> Skipped Step A1? The installer still creates `config.yaml` from the
> example automatically — but with placeholder values. Run
> `sudo nano config.yaml`, set the table above, and save: the bot reloads
> the file on its own.

**Step A3 — verify it is running**

```bash
systemctl status meshtech-bot          # active (running)?
journalctl -u meshtech-bot -f          # live logs (Ctrl-C to stop watching)
curl http://127.0.0.1:8081/api/login   # dashboard answering? (expect 200)
```

The service is **enabled at boot** already. Usual commands:

```bash
sudo systemctl restart meshtech-bot    # after editing install options
sudo systemctl stop meshtech-bot
sudo systemctl start meshtech-bot
```

**Permissions — what the installer did**

- Account: `meshtech` (system user, cannot log in, never runs as root).
- Ownership: the whole install folder + `data/` belong to
  `meshtech:meshtech`. Config is `640` (readable only by the bot and root).
- The bot writes its SQLite database, JSONL capture and exports under
  `<install>/data/`.
- Everything runs from `WorkingDirectory=<install folder>`, so relative
  paths in `config.yaml` just work.

---

## Option B — Native Linux without a virtual environment

Same as Option A, but the Python packages are installed with the **system
Python** instead of a private `.venv`. Choose this on an appliance box you
fully control where a shared Python is acceptable.

```bash
cd /opt/meshtech-bot
sudo apt update && sudo apt install -y python3 python3-pip
sudo ./install.sh --no-venv
```

Notes for this mode:

- The installer falls back to `pip install --break-system-packages` on
  Debian 12 / Ubuntu 23+ (these refuse global pip installs by default).
  That is fine on a dedicated bot box, but if you also develop on the same
  machine, prefer Option A so system packages stay untouched.
- The service runs `/usr/bin/python3 bot.py` — everything else (config,
  permissions, boot start) is identical to Option A.
- You can switch between the two modes any time: rerun the installer with
  the other flag; data in `data/` is unaffected.

---

## Option C — Docker

Run the bot in a container. Best if you already use Docker, want an
immutable install, or the machine runs something other than Debian.

**Step C1 — install Docker**

On a Raspberry Pi or Debian/Ubuntu box:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in so the group takes effect
```

**Step C2 — prepare config and data**

```bash
cd /opt/meshtech-bot            # your clone
cp config.example.yaml config.yaml
sudo nano config.yaml           # set host/port/channels/admin/dashboard password
mkdir -p data
sudo chown 1001:1001 data       # container runs as uid 1001 (meshtech)
```

**Step C3 — build and start**

```bash
docker compose up -d --build
docker compose logs -f          # watch logs (Ctrl-C stops watching)
```

The compose file uses `network_mode: host`, so the bot reaches your
repeater on the LAN directly and the dashboard listens on `127.0.0.1:8081`
of the host. The container restarts on boot via `restart: unless-stopped`.

Useful commands:

```bash
docker compose down             # stop the container
docker compose up -d --build    # rebuild + restart after a sudo git pull
docker compose logs --tail 100 meshtech-bot
```

**Updating the container after `sudo git pull`:** run `docker compose up -d --build`
again — your `config.yaml` and `data/` are mounted from the host and
survive.

---

## 3. End-to-end verification (first run)

Work through this in order — it proves every layer, from the service down
through the radio link to the captured data.

**Server-side first** (on the bot machine):

```bash
systemctl status meshtech-bot        # active (running)?
journalctl -u meshtech-bot -n 20     # "Startup complete: N channel(s)" present?
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/api/login   # 200?
```

**Then from your radio** (the openHop app on your phone or the radio itself):

| # | Do this | Expected result | What it proves |
|---|---|---|---|
| 1 | Send `!help` on a configured channel | The bot replies with the command list | Channel path + replies work |
| 2 | Send `!status` on the channel | A short status line | Config + database are sane |
| 3 | DM the bot `!status` | A longer, more detailed reply | Direct-message path works |
| 4 | DM `!nodes` | A list of nodes the bot knows | Advert/contact discovery works — find your own 12-hex prefix here |
| 5 | DM `!2byte` | A one-line bar, e.g. `2-byte path nodes: [▓▓▒▒…] 38% (10/26 of registered nodes)` | Packet capture + data handlers work |
| 6 | Add your prefix from step 4 to `dm.admin_pubkey_prefixes`, then DM `!diag` | A diagnostics summary | Admin access works |
| 7 | From a node **farther than** `mesh.max_inbound_hops` away, send `!status` | No reply — the log shows `[skip] hops N > limit 3` | Hop limiting works |
| 8 | Send a plain word like `hello` on the channel | Silence | The bot is not chatty (`replies: []` by default) |
| 9 | On the repeater host: `sudo systemctl restart openhop-repeater` | The bot logs a `Disconnected` event, then reconnects on its own | Auto-reconnect works |
| 10 | On the bot machine: `sudo reboot`, then after boot run `systemctl status meshtech-bot` | Active without you logging in | Starts at every boot |
| 11 | Open `http://<bot-machine>:8081` and log in | Nodes and Packets tables populate with live data | Dashboard + capture work |

**Pass criteria:** rows 1–5 and 9–11 green means the install is healthy. Row 6
is the only one that needs a config edit first (your node's prefix). Rows 7–8
are just as important — they verify the bot stays *quiet* when it should,
which is the whole point of a testing bot.

**Pacing:** the bot rate-limits replies (3 s between any two, 30 s per sender)
so wait a few seconds between tests. Do rows 1–6 from close range — anything
arriving over more hops than the limit is intentionally ignored.

---

## Updating the bot

```bash
cd /opt/meshtech-bot
sudo git pull            # needs sudo: the folder is owned by the system
sudo ./install.sh        # re-runs dependency install + permissions (safe)
```

The `meshtech` account, your `config.yaml` and your `data/` are all kept.
For Docker: `sudo git pull` then `docker compose up -d --build`.

---

## Backups

Everything worth keeping lives in one folder:

```bash
sudo tar czf meshtech-backup-$(date +%F).tar.gz /opt/meshtech-bot/config.yaml /opt/meshtech-bot/data
```

Restore = unpack into a fresh install before starting the service.

---

## Uninstalling

```bash
cd /opt/meshtech-bot
sudo ./install.sh --uninstall     # stops service, removes account (data kept)
sudo rm -rf /opt/meshtech-bot     # delete the files too, once you are sure
# Docker instead:
docker compose down && docker rmi meshtech-bot:latest
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Logs show `Connection refused` / retrying | The companion port is not open: confirm openHop runs, has a companion with `tcp_port`, and was restarted. `telnet <repeater-ip> <port>` from the bot machine should connect. |
| `only one client` / second bot cannot connect | One TCP client per companion — stop the old bot (`systemctl stop meshtech-bot`) before starting another. |
| `permission denied` writing `data/…` | Ownership fix: `sudo chown -R meshtech:meshtech /opt/meshtech-bot` (install.sh does this too). |
| Dashboard loads but says `not connected` | Bot is up but the repeater link is down — see first row. |
| `python3 -m venv` fails | Install the venv module: `sudo apt install python3-venv`. |
| `externally-managed-environment` with `--no-venv` | That is expected on Debian 12+/Ubuntu 23+; the installer handles it with `--break-system-packages`. Prefer Option A if unsure. |
| Want the dashboard from another device | Set `web.host: "0.0.0.0"` AND a strong `web.password` (or use a reverse proxy for HTTPS). |
| Bot answers far-away traffic | Raise/lower `mesh.max_inbound_hops`, and cap floods on the repeater (`max_flood_hops`) — see README. |

---

## Reference: files you may care about

| Path | Purpose |
|---|---|
| `config.yaml` | your settings (created from `config.example.yaml`) |
| `data/bot.db` | SQLite: nodes, messages, routes, overrides, packet capture |
| `data/packets.jsonl` | append-only packet log for offline analysis |
| `data/exports/` | CSV exports from `scripts/export_packets.py` |
| `/etc/systemd/system/meshtech-bot.service` | the native service unit |
| `journalctl -u meshtech-bot` | native logs (systemd captures stdout) |
