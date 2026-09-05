# Installing MeshTech-Bot

This guide walks you through setting up the bot on a Linux machine
(Debian, Ubuntu, or Raspberry Pi OS) or in Docker. The bot talks over TCP
to the **companion endpoint** of an openHop Repeater on your network.

Pick one install option below. Every option makes the bot start by itself
when the machine boots — you won't need to run it by hand.

---

## 1. What you need

- A Linux machine (a Raspberry Pi 3 or newer is plenty), or any machine with Docker.
- A working openHop Repeater with a **dedicated bot companion** already set
  up on it (its `tcp_port` is what the bot connects to).
- A mesh radio so you can test the bot (and find your node's prefix, which makes you the admin).

---

## 2. Download the code

```bash
sudo apt update
sudo apt install -y git python3 python3-venv   # python3-venv not needed for Docker

sudo git clone https://github.com/mygooglyeyes/MeshTech-bot.git /opt/meshtech-bot
cd /opt/meshtech-bot
```

Notes:

- `sudo` is needed because `/opt` belongs to the system. It also means git
  runs as root, which is why the guide uses HTTPS, not SSH.
- If the repo is private, git will ask for your GitHub **username** and a
  **personal access token** (not your GitHub password). Make a token at
  GitHub → Settings → Developer settings → Personal access tokens.
- Your `config.yaml` and `data/` are never touched by updates — they stay
  on the machine.

Now choose an install option:

- [Option A — Native install (recommended)](#option-a--native-install-recommended)
- [Option B — Native install, no virtual environment](#option-b--native-install-without-a-virtual-environment)
- [Option C — Docker](#option-c--docker)

---

## Option A — Native install (recommended)

The installer does everything: it creates a dedicated **`meshtech`** user,
installs the Python packages, creates your config, sets file permissions,
and installs a service that starts the bot at every boot and restarts it
if it crashes.

**A1. Create and edit your config**

`config.yaml` is not shipped with the code (it would leak your passwords),
so you make your own from the example:

```bash
cd /opt/meshtech-bot
sudo cp config.example.yaml config.yaml
sudo nano config.yaml    # save with Ctrl+O, exit with Ctrl+X
```

Set at least these:

| Setting | What to put there |
|---|---|
| `connection.host` | The LAN IP of your openHop Repeater machine |
| `connection.port` | The companion `tcp_port` from step 2 (e.g. 5000) |
| `channels` | The `#channel` names to listen on. `reply: false` = log only, never answer |
| `dm.admin_pubkey_prefixes` | Your node's 12-character hex prefix, so you can use admin commands later |
| `web.password_file` | Leave this as `data/.dashboard_password` — the installer creates it for you |

**A2. Run the installer**

```bash
cd /opt/meshtech-bot
sudo ./install.sh
```

The installer asks you to **choose a dashboard password** (type it twice).
That is the only password you need to make up.

Skipped step A1? The installer creates `config.yaml` anyway. Just edit it
afterwards — the bot picks up the file on its own.

**A3. Change the dashboard password later (one command)**

```bash
cd /opt/meshtech-bot
sudo ./set-password.sh
```

The password lives in its own file, not in `config.yaml`, so a leaked
config can't unlock your dashboard. (Alternative: set the
`MESHTECH_DASHBOARD_PASSWORD` environment variable — it wins over the file.)

**A4. Check it is running**

```bash
systemctl status meshtech-bot           # should say "active (running)"
journalctl -u meshtech-bot -n 20        # recent logs; look for "Startup complete"
curl -s http://127.0.0.1:8081/api/login # should reply {"auth_required":true}
```

Day-to-day commands:

```bash
sudo systemctl restart meshtech-bot
sudo systemctl stop meshtech-bot
sudo systemctl start meshtech-bot
```

---

## Option B — Native install, no virtual environment

Same as Option A, but the Python packages go into the system Python
instead of a private folder. Choose this only on a machine you fully
control.

```bash
cd /opt/meshtech-bot
sudo ./install.sh --no-venv
```

Everything else — config, permissions, service — works the same as
Option A.

---

## Option C — Docker

Choose this if you already use Docker or want a self-contained install.

**C1. Install Docker** (on Debian/Ubuntu/Raspberry Pi):

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out and back in so the group applies
```

**C2. Prepare config and data**

```bash
cd /opt/meshtech-bot
cp config.example.yaml config.yaml
sudo nano config.yaml                 # set host, port, channels, admin prefix
mkdir -p data
sudo chown 1001:1001 data             # the container runs as uid 1001

printf 'your-password\n' > data/.dashboard_password
sudo chown 1001:1001 data/.dashboard_password
sudo chmod 600 data/.dashboard_password
```

**C3. Build and start**

```bash
docker compose up -d --build
docker compose logs -f               # watch logs; Ctrl-C stops watching
```

Other useful commands:

```bash
docker compose down                  # stop
docker compose up -d --build         # rebuild and restart
```

---

## 3. Test that it works

**On the bot machine first:**

```bash
systemctl status meshtech-bot
journalctl -u meshtech-bot -n 20
```

**Then from your radio:**

| # | Do this | Expected |
|---|---|---|
| 1 | Send `!help` on a configured channel | The bot lists its commands |
| 2 | Send `!status` | A short status line |
| 3 | DM the bot `!status` (try `!status x`) | A reply; `x` gives more detail |
| 4 | DM `!nodes` | A list of nodes the bot knows — your own 12-hex prefix is in here |
| 5 | DM `!diag` | Works once your prefix is in `dm.admin_pubkey_prefixes` (step A1) |
| 6 | Send `!status` from a node more than `mesh.max_inbound_hops` away | No reply — that is correct |
| 7 | Restart the repeater | The bot disconnects, then reconnects by itself |
| 8 | Reboot the bot machine | The bot is running again without you logging in |
| 9 | Open the dashboard at `http://<bot-machine>:8081` and log in | Tables fill with live data |

The bot spaces out its replies (a few seconds between any two), so wait a
moment between tests.

---

## Updating the bot

```bash
cd /opt/meshtech-bot
sudo git pull
sudo ./install.sh        # safe to rerun — your config and data are kept
```

With Docker, the update is `sudo git pull` and then `docker compose up -d --build`.

---

## Backups

Everything worth keeping is in two places:

```bash
sudo tar czf meshtech-backup-$(date +%F).tar.gz \
  /opt/meshtech-bot/config.yaml /opt/meshtech-bot/data
```

To restore, unpack the file over a fresh install before starting the service.

---

## Uninstalling

```bash
cd /opt/meshtech-bot
sudo ./install.sh --uninstall     # stops the service, keeps your data
sudo rm -rf /opt/meshtech-bot     # delete the files too, when you are sure
```

With Docker instead:

```bash
docker compose down && docker rmi meshtech-bot:latest
```

---

## Security in one paragraph

Keep the dashboard bound to `127.0.0.1` unless you really need it from
another device — the radio side of the bot doesn't care where the
dashboard listens. Always use a real password, stored via
`sudo ./set-password.sh` (never inside `config.yaml`). If you do expose
the dashboard on the network, put a reverse proxy in front of it for
HTTPS: otherwise the password travels as plaintext on your LAN. Wrong
passwords are throttled automatically, and the bot itself runs as an
unprivileged user that can only write inside its `data/` folder.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Logs show `Connection refused`, keeps retrying | The companion port isn't open. Check openHop has a companion with `tcp_port`, and that you restarted it. |
| A second bot can't connect | One client per companion. Stop the old bot first. |
| `Permission denied` when entering the install folder | Use `sudo` — the folder belongs to the `meshtech` account. |
| `dubious ownership` on `git pull` | `sudo git config --global --add safe.directory /opt/meshtech-bot` |
| Dashboard loads but says `not connected` | The repeater link is down — see the first row. |
| `python3 -m venv` fails | `sudo apt install python3-venv` |
| Want the dashboard from another device | Set `web.host: "0.0.0.0"`, set a strong password, and ideally use a reverse proxy (HTTPS). |

---

## Files you may care about

| Path | Purpose |
|---|---|
| `config.yaml` | Your settings (created from `config.example.yaml`) |
| `data/bot.db` | The database: nodes, messages, routes, packet capture |
| `data/packets.jsonl` | Packet log for offline analysis |
| `data/exports/` | CSV exports made by `scripts/export_packets.py` |
| `data/.dashboard_password` | Your dashboard password (first line of the file) |
| `set-password.sh` | Set or change the dashboard password |
