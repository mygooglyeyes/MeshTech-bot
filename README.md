# MeshTech-Bot

A simple Python bot for **MeshCore mesh networks**, built for testing and
analysis. It connects over TCP to the **companion endpoint** of an
openHop Repeater, listens on your channels and direct messages, answers
keyword questions from nearby nodes only, and stores what it sees in a
local database with a password-protected web dashboard.

The bot has no radio of its own — it speaks to the repeater, and the
repeater is the bot's mesh identity.

## What it does

- Listens on several channels at once (a channel can be *listen-only*: logged but never answered)
- Answers keywords from `config.yaml` (no code needed) or from pluggable command handlers
- Answers direct messages; admin commands are restricted to allowlisted nodes
- Ignores messages that travelled more than a set number of hops — far-away nodes don't get replies
- Keeps a SQLite database of nodes, messages, routes, and packet captures
- Gives short replies by default; add `x` for the extended version (`!nodes x`)
- Shows everything on a live web dashboard

## Requirements

- Python 3.10+
- A working openHop Repeater with a **dedicated bot companion** already set up on it.

## Install

Pick one way — full step-by-step instructions live in
**[docs/INSTALL.md](docs/INSTALL.md)**:

1. **Native Linux (recommended)** — one command gets the code, one command
   installs it as a service that starts at boot:
   `sudo git clone https://github.com/mygooglyeyes/MeshTech-bot.git /opt/meshtech-bot`,
   then `cd /opt/meshtech-bot && sudo ./install.sh`.
2. **Docker** — `cp config.example.yaml config.yaml`, edit it, then
   `docker compose up -d --build`.
3. **Quick local test** (any machine):

   ```bash
   git clone https://github.com/mygooglyeyes/MeshTech-bot.git && cd MeshTech-bot
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp config.example.yaml config.yaml
   python bot.py --check
   python bot.py
   ```

## Configure

Copy the example to `config.yaml` (first install only), then edit it:

```bash
cp config.example.yaml config.yaml
```

The settings that matter:

1. `connection.host` — the IP of your openHop Repeater machine.
2. `connection.port` — the companion `tcp_port` from the repeater's config (5000 by default).
3. `channels` — the `#channel` names to listen on. `reply: false` = log only.
4. `mesh.max_inbound_hops` — e.g. `3`: only answer messages that reached you within 3 hops.
5. `mesh.channel_sender_name` — how much to trust the name embedded in channel
   text (`"Name: message"`): `trust` (default), `smart`, or `off`.
6. `dm.admin_pubkey_prefixes` — your node's 12-character hex prefix, so you can use admin commands.
7. `bot.answer_unknown_senders` — default `false`: stay silent when the sender can't be identified.

Check and run:

```bash
python bot.py --check     # prints a summary or clear errors
python bot.py             # run (stop with Ctrl-C)
```

## The dashboard

With `web.enabled: true`, open `http://<bot-machine>:8081` in a browser.
Set the password once with `sudo ./set-password.sh` (it lives in its own
secrets file, never in `config.yaml`). The dashboard shows live activity,
per-channel mute toggles, the node table with drill-down and per-node
**block checkboxes** (ignore everything from a node), a message browser,
packet capture views, and reload/shutdown buttons. Blocks survive restarts.

## Commands on the mesh

| Command | Who | What it does |
|---|---|---|
| `!help` | anyone | List commands |
| `!status` / `!status x` | anyone | Bot health, channels, counters |
| `!2byte` | anyone | Share of nodes using 2-byte path hashes (bar chart) |
| `!nodes` / `!nodes x` | DM | Known nodes from the database |
| `!path` / `!pathx` / `!path <node>` | anyone | The path your message took to the bot, or the route to another node |
| `!stats <node or #channel>` | admin (DM) | Propagation delay + hop stats |
| `!diag` / `!diag x` | admin (DM) | Database + traffic summary |
| `!reload` | admin (DM) | Re-read `config.yaml` |
| `!shutdown` | admin (DM) | Stop the bot |

- "Admin" means your node's prefix is listed in `dm.admin_pubkey_prefixes`.
- Append **`x`** for the extended version. The words `brief`/`full` also work.
- Plain-word replies (`replies:` in config.yaml) are empty by default — the
  bot is for testing, not chat, so `hello` gets no answer.

## Packet capture and export

The bot records every companion frame it sees (channel messages, DMs,
adverts, and more) with frame type, hops, SNR, sender, and timing.
Decoded capture is on by default; raw wire bytes are opt-in
(`storage.packet_raw_hex: true`). Everything lands in `data/bot.db` and an
append-only `data/packets.jsonl` file.

The dashboard shows the latest frames, a raw-link profile (packet sizes,
timing), and traffic analysis charts. For offline work, export to CSV:

```bash
python scripts/export_packets.py               # everything
python scripts/export_packets.py --hours 24    # last 24 hours
```

Each run creates a `data/exports/packets-<timestamp>/` folder with the
CSV plus hourly, frame-type, hops, SNR, and sender summaries.

## Adding features

Two ways:

1. **No code** — add an entry to the `replies:` list in `config.yaml`:

   ```yaml
   replies:
     - keywords: ["call", "frequency"]
       text: "Weekly net: Sundays 20:00 local on #net."
   ```

2. **Python** — copy `handlers/_template.py` to a new file, rename the
   class, fill in `handle()`, restart (or `!reload`). It's picked up
   automatically.

## Tests

No radio or network needed:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Where things live

- `core/` — the bot engine (config, radio client, routing, database, capture)
- `handlers/` — one file per command, auto-discovered
- `web/` — the dashboard (FastAPI server + static page)
- `scripts/export_packets.py` — CSV export tool

## Notes

- Channel messages carry no sender identity in MeshCore by design — nodes
  are named from DMs, adverts, and contacts.
- Propagation delay uses sender-provided timestamps, so treat numbers as
  trends rather than exact values.
- The dashboard binds to `127.0.0.1` by default. To reach it from another
  device, set `web.host` to your LAN IP, use a real password, and consider
  a reverse proxy for HTTPS.

## License

MIT. MeshCore is by its authors; this project is independent of the
MeshCore and openHop projects.
