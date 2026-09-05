# MeshTech-Bot

A modular Python bot for MeshCore mesh networks, built for answering and —
coming next — node and packet analysis. It connects over TCP to the companion
endpoint of an **openHop Repeater**, listens on your channels and direct
messages, answers keyword questions from local nodes only, keeps a local
SQLite database of nodes and message history, and includes a password
protected browser dashboard.

No radio keys live on the bot machine — the bot's mesh identity belongs to
the openHop companion it talks to.

```
radio / mesh  <->  openHop Repeater  <->  TCP (companion port, default 5000)  <->  this bot
```

## Features

- Listens on several channels at once (channels can be *listen-only* — logged, never answered)
- Keyword-triggered answers from `config.yaml` (no code) **and** pluggable command handlers
- Direct-message replies; admin commands (`!diag`, `!reload`, `!shutdown`) restricted to allowlisted nodes
- Hop-limit filter — ignores messages that travelled more than N hops, so distant nodes are not answered
- Friendly replies using stored node names (`K7ABC (a1b2c3)` instead of raw hex)
- SQLite database: node registry, message log, route snapshots, propagation-time statistics
- Compact replies by default; append `x` for the extended version (`!nodes x`)
- Plain-text formatting tuned for 133-character MeshCore messages (wrapping, aligned tables, `[1/2]` chunking)
- Live-feed browser dashboard: status, channels, nodes with drill-down, message browser, mute toggles, reload/shutdown

## Requirements

- Python 3.10+
- An openHop Repeater on your network with a **companion identity** enabled.
  In `/etc/openhop_repeater/config.yaml` on the repeater you need something like:

  ```yaml
  mesh:
    companions:
      - name: "BotCompanion"
        identity_key: "your_companion_identity_key_hex_here"
        settings:
          node_name: "meshtech-bot"
          tcp_port: 5000
  ```

  Restart the repeater service afterwards (`sudo systemctl restart openhop-repeater`).
  One TCP client can connect per companion — the bot is that client.

## Install

Pick one way to run it — full step-by-step instructions for each live in
**[docs/INSTALL.md](docs/INSTALL.md)**.

1. **Native Linux service (recommended)** — Debian/Ubuntu/Raspberry Pi OS.
   `sudo git clone https://github.com/mygooglyeyes/MeshTech-bot.git /opt/meshtech-bot`
   (`sudo` because `/opt` is system-owned), then `cd /opt/meshtech-bot`, run
   `sudo ./install.sh` once (it will ask for a dashboard password) and edit
   `config.yaml`: the bot gets its own `meshtech` account, correct file
   permissions, and a systemd service that starts at boot and restarts on
   crashes. Change the dashboard password any time with
   `sudo ./set-password.sh`.
2. **Docker** — `cp config.example.yaml config.yaml`, edit it, then
   `docker compose up -d --build`. Config and data stay on the host.
3. **Local development / quick test** (this machine):

   ```bash
   git clone https://github.com/mygooglyeyes/MeshTech-bot.git && cd MeshTech-bot
   python -m venv .venv            # Windows: python -m venv .venv ; .venv\Scripts\activate
   source .venv/bin/activate       # Linux/macOS
   pip install -r requirements.txt
   cp config.example.yaml config.yaml
   python bot.py --check
   python bot.py
   ```

Every install mode needs the openHop Repeater companion endpoint ready
(step 1 of the install doc) — the bot has no radio of its own; it speaks
TCP to the repeater's companion identity.

## Configure

Create your config from the example (first install only):

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml`:

1. `connection.host` — IP of the machine running openHop Repeater.
2. `connection.port` — the companion `tcp_port` from openHop's config (5000 by default).
3. `channels` — the `#channel` names to listen on; `reply: false` = log only.
4. `mesh.max_inbound_hops` — e.g. `3`: the bot answers only messages that reached it within 3 hops.
5. `mesh.channel_sender_name` — how much to trust the sender name MeshCore embeds
   in channel text (`"Name: message"`): `trust` (always strip — default),
   `smart` (strip only when the whole text wouldn't already match, so a
   message like `hello: anyone around?` keeps its greeting), or `off`
   (never strip — for gateways that relay messages without the embedded
   name).
6. `dm.admin_pubkey_prefixes` — your node's public-key prefix (12 hex chars) so YOU can run admin commands.
7. `bot.answer_unknown_senders` — default `false`: the bot stays silent when it
   cannot identify who sent a message (a DM without a sender prefix, or a
   channel message with no embedded sender name). Set `true` only for meshes
   where sender identity is never available.

Validate, then run:

```bash
python bot.py --check     # prints a summary or clear errors
python bot.py             # run
```

Stop with `Ctrl-C`. Reconnect and config reloads happen automatically when
the file is edited (`!reload` from an admin node, or the dashboard button).

## Limit how far replies travel (repeater side)

Channel replies are flooded by repeaters, so also cap propagation on the
repeater itself. On the openHop Repeater host:

```bash
sudo nano /etc/openhop_repeater/config.yaml
# set  max_flood_hops: 3   (under the repeater settings)
sudo systemctl restart openhop-repeater
```

or use openHop's policy engine (web UI at `http://<repeater-ip>:8000`) to add
a rule like *"drop channel packets over 2 hops"*. This bounds the geographic
spread of every flood packet, including the bot's replies.

## Packet capture (for later analysis)

The bot records every companion frame it sees, ready for node and packet
analysis off the radio:

- **Decoded layer** (on by default) — one record per frame the companion
  dispatches: channel messages, DMs, adverts/contacts, path updates, and
  command responses. Each record carries frame type, hops, SNR, sender,
  channel, text (for messages) and the bot's receive timestamp.
- **Raw layer** (opt-in, `storage.packet_raw_hex: true`) — additionally
  records the raw wire bytes of every frame with its size, for packet
  size / timing / type-distribution analysis of the companion link itself.
- **SQLite** (`packets` table in `data/bot.db`, capped by
  `storage.packet_max_rows` — oldest rows are pruned automatically).
- **JSONL file** (`storage.packet_jsonl`, default `data/packets.jsonl`) —
  an append-only, one-JSON-object-per-line file. Load it later with:

  ```python
  import pandas as pd
  pkts = pd.read_json("data/packets.jsonl", lines=True)
  pkts.groupby("frame_type").size()        # traffic mix
  pkts[pkts.layer == "decoded"]["hops"]    # hop distribution
  ```

The dashboard's **Packets** panel shows the latest frames (toggle decoded /
raw) and a live **raw link profile**: packet size stats and distribution,
the receive rate, and the inter-frame gap distribution (min/avg/p50/p95/max)
of the companion link over the most recent 5,000 raw frames. `!diag`
reports the capture total. Set `storage.capture_packets: false` to disable
capture entirely.

### Exporting to CSV

For spreadsheet-friendly offline analysis, export the packets table with
the built-in script (no radio needed — it reads the database directly):

```bash
python scripts/export_packets.py                  # everything
python scripts/export_packets.py --hours 24       # last 24 hours
python scripts/export_packets.py --layer raw      # raw frames only
python scripts/export_packets.py --limit 10000    # newest 10,000 frames
python scripts/export_packets.py --full           # also the payload JSON column
```

Each run writes into a fresh `data/exports/packets-<timestamp>/` folder
(or `--out <folder>`): `packets.csv` with one row per frame (id, local
timestamp, layer, direction, frame type, sender, hops, SNR, channel,
text, size) plus five prebuilt summaries — `summary_hourly.csv` (traffic
per hour), `summary_frame_types.csv` (mix per layer), `summary_hops.csv`
(hop distribution), `summary_snr.csv` (average/min/max SNR per hour) and
`summary_senders.csv` (most active senders). Every file opens directly
in Excel/LibreOffice or loads with `pd.read_csv("packets.csv")`.

With raw capture on (`storage.packet_raw_hex: true`) the same profile is
also available as JSON via `GET /api/packets/profile` and can be computed
offline from the JSONL file:

```python
import pandas as pd
raw = pd.read_json("data/packets.jsonl", lines=True)
raw = raw[raw.layer == "raw"].sort_values("ts")
raw["size"].describe()                       # packet sizes
raw["ts"].diff().describe()                  # inter-frame gaps
raw.groupby("frame_type").size()             # frame-type mix
```

## Roadmap

- **Node analysis** — enrich the node registry with activity profiles, routes,
  SNR trends and propagation statistics (already partially fed by `!stats` and
the dashboard drill-down).
- **Packet analysis** — capture and inspect raw companion traffic: hop counts,
  SNR, timing and frame types, surfaced in the dashboard and exports.

## Commands on the mesh

| Command | Who | What it does |
|---|---|---|
| `!help` | anyone | list commands; `x` gives the extended version |
| `!status` / `!status x` | anyone | bot health, channels, counters |
| `!2byte` | anyone | share of nodes using 2-byte path hashes (ASCII bar chart from packet capture) |
| `!nodes` / `!nodes x` | DM | known nodes from the database |
| `!path` / `!pathx` / `!path <node>` | anyone | path your message took to the bot, or route to another node |
| `!stats <node or #channel>` | admin (DM) | propagation delay + hop stats |
| `!diag` / `!diag x` | admin (DM) | database + traffic summary |
| `!reload` | admin (DM) | re-read config.yaml, refresh handlers |
| `!shutdown` | admin (DM) | graceful stop |

Admin = nodes whose prefix is listed in `dm.admin_pubkey_prefixes`. All
replies default to the compact view to keep radio traffic short; append
**`x`** to a command for the extended version (`!nodes x`, `!path <node> x`).
The canonical words `brief`/`full` still work if you prefer them. Defaults
are configurable under `verbosity`.

Plain-word canned replies (`replies:` in config.yaml) are **empty by default**
— the bot is built for testing, not chat, so it stays silent on words like
`hello`. Add your own trigger words there when you need them.

## Web dashboard

With `web.enabled: true` open `http://127.0.0.1:8081` on the bot machine
(or the configured host) and enter the dashboard password — set it once with
`sudo ./set-password.sh` (it lives in a `web.password_file` secrets file, not
in `config.yaml`; a legacy inline `web.password` also works but warns). The dashboard shows live
activity, per-channel mute toggles, the node table with drill-down
(propagation stats, route snapshots) and per-node **block checkboxes**
(tick one to ignore every message from that node — blocked senders show
up in the live feed as `[skip] node blocked`), a message browser, and
reload / shutdown actions. Blocked nodes are stored in the database, so
blocks survive restarts.

## How the pieces fit

```
config.yaml ─ core/config.py      typed settings + validation
bot.py       entry point, --check, shutdown handling
core/client.py   meshcore TCP connection, reconnect, channel sync, contacts
core/router.py   guards (channels, hops, DM access, rates) + trigger matching
core/store.py    SQLite: nodes, messages, routes, overrides
core/format.py   plain-text layout (wrap, tables, [1/2] chunks)
core/feed.py     live events for the dashboard
handlers/        one file per capability (auto-discovered)
web/             FastAPI + static dashboard
```

Received messages are stored in `data/bot.db`, then passed through guards:
channel allowlist → reply-enabled? → hop limit → DM allowlist → rate limit →
handler match → reply. Hop counts, SNR and sender timestamps are logged with
every message; `!stats` and the dashboard use them for propagation analysis
— the foundation for the node and packet analysis on the roadmap.

## Adding a feature

Two ways:

1. **No code** — add an entry to the `replies:` list in config.yaml:

   ```yaml
   replies:
     - keywords: ["call", "frequency"]
       text: "Weekly net: Sundays 20:00 local on #net."
   ```

2. **Python handler** — copy `handlers/_template.py` to `handlers/<name>.py`,
   rename the class, fill in `handle()`, restart (or `!reload`). It is picked
   up automatically. Handlers can read/write the database and any data source
   you like; keep replies compact; use `x` for detail.

## Tests

The test suite needs no radio or network:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Notes & limitations

- Channel messages carry **no sender identity** in MeshCore by design — nodes
  are named from DMs/adverts/contacts; channel rows still feed distance and
  hop statistics.
- Propagation delay uses sender-provided timestamps, so clocks can skew;
  treat numbers as relative trends.
- The dashboard binds to `127.0.0.1` by default. To reach it from another
  device set `web.host` to the LAN IP **and** set a strong dashboard
  password via `web.password_file` (never keep it in config.yaml — or put
  the dashboard behind a reverse proxy for HTTPS).
- Hop metadata may be unavailable on some frames — the `mesh.unknown_hops`
  policy decides whether those are answered or ignored.

## License

MIT — see the project repository. MeshCore is by its authors; this project
is independent of and unaffiliated with the MeshCore project or openHop.
