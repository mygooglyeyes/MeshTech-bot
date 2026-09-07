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
- Optional **modules** — weather, NWS alerts, earthquakes — with scheduled pushes to channels of your choice
- Flood guardrails: per-person reply limits plus an overall transmission budget, both tunable in the dashboard
- Keeps a SQLite database of nodes, messages, routes, and packet captures
- Gives short replies by default; add `x` for the extended version (`!nodes x`)
- Shows everything on a live web dashboard
- Every change ships with its own version number and a plain-language
  entry in **[CHANGELOG.md](CHANGELOG.md)** — the dashboard header shows
  exactly which build your bot is running

## Requirements

- Python 3.10+
- A working openHop Repeater with a **dedicated bot companion** already set up on it.
- **Companion order matters:** create your everyday-use companion
  **first**, and the bot's companion **second**. The repeater's web
  console attaches to the first companion, and a companion serves one
  client at a time — if the bot's companion is first, the console takes
  it and the bot can't connect.

## Install

Pick one way — full step-by-step instructions live in
**[docs/INSTALL.md](docs/INSTALL.md)**:

- **Native Linux (recommended)**
  - Clone into your home folder:

    ```bash
    git clone https://github.com/mygooglyeyes/MeshTech-bot.git ~/meshtech-bot
    ```

  - Run the installer (it also creates the boot service):

    ```bash
    cd ~/meshtech-bot && sudo ./install.sh
    ```

  - Update later from that same folder: `sudo ./manage.sh update`

- **Docker**
  - Copy and edit the config: `cp config.example.yaml config.yaml`
  - Build and start: `docker compose up -d --build`

- **Quick local test** (any machine)
  - Clone and enter the folder:

    ```bash
    git clone https://github.com/mygooglyeyes/MeshTech-bot.git && cd MeshTech-bot
    ```

  - Create a virtual environment and install:

    ```bash
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    ```

  - Copy the config, check it, run:

    ```bash
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

- `connection.host` — the IP of your openHop Repeater machine.
- `connection.port` — the companion `tcp_port` from the repeater's config (5000 by default).
- `channels` — the `#channel` names to listen on.
- `mesh.max_inbound_hops` — e.g. `3`: only answer messages that reached you within 3 hops.
- `mesh.channel_sender_name` — how much to trust the name embedded in channel
  text (`"Name: message"`): `trust` (default), `smart`, or `off`.
- `dm.admin_pubkey_prefixes` — one line per admin node, using each node's 12-character hex prefix.
- `bot.answer_unknown_senders` — default `false`: stay silent when the sender can't be identified.

## Set the dashboard password

On a native install, `install.sh` asks you to choose a password during
setup. To set or change it later:

```bash
sudo ./set-password.sh
```

The password lives in its own file (not in `config.yaml`), so a leaked
config can't unlock the dashboard. An alternative is the
`MESHTECH_DASHBOARD_PASSWORD` environment variable.

## The dashboard

With `web.enabled: true`, open `http://<bot-machine>:8081` in a browser
and enter the password you set. The dashboard shows live activity,
per-channel mute toggles, the node table with drill-down and per-node
**block checkboxes** (ignore everything from a node), a message browser,
packet capture views, a **Modules card** for the optional add-ons, and
reload/shutdown buttons. Blocks survive restarts. The header shows the
running version, uptime, and how close the bot is to its transmission
budget.

## Commands on the mesh

| Command | Who | What it does |
|---|---|---|
| `!help` | anyone | List commands |
| `!dm` | anyone | The bot DMs you back, starting a direct thread |
| `!status` / `!status x` | DM | Bot health, channels, counters |
| `!2byte` | anyone | Share of nodes using 2-byte path hashes (bar chart) |
| `!nodes` / `!nodes x` | DM | Known nodes from the database |
| `!path` / `!pathx` / `!path <node>` | anyone | The path your message took to the bot, or the route to another node |
| `!wx` / `!weather <zip>` | anyone | Current conditions for a US zip (module) |
| `!alerts [zip]` | anyone | Active NWS weather alerts (module) |
| `!quake [zip]` | anyone | Recent USGS earthquakes near a zip (module) |
| `!up` / `!down` | admin (DM) | Raise or cancel the extra transmission budget (see below) |
| `!stats <node or #channel>` | admin (DM) | Propagation delay + hop stats |
| `!diag` / `!diag x` | admin (DM) | Database + traffic summary |
| `!reload` | admin (DM) | Re-read `config.yaml` |
| `!shutdown` | admin (DM) | Stop the bot |

- "Admin" means your node's prefix is listed in `dm.admin_pubkey_prefixes`.
- Append **`x`** for the extended version. The words `brief`/`full` also work.
- Plain-word replies (`replies:` in config.yaml) are empty by default — the
  bot is for testing, not chat, so `hello` gets no answer.

## Modules (optional add-ons)

Modules are features you switch on from the dashboard's **Modules card**
(no restart needed) or in the `modules:` section of `config.yaml`:

| Module | Command | Also does |
|---|---|---|
| weather | `!wx <zip>` / `!weather <zip>` | Daily forecast post to a channel at a time you set |
| alerts | `!alerts [zip]` | Pushes new severe NWS alerts to your chosen channels |
| quake | `!quake [zip]` | Pushes new USGS earthquakes near a zip to your channels |

Each module keeps its own settings (default zip, push channels, check
interval) edited right in the card. Pushes land in the channels you
pick — `novato` and `#novato` are the same channel. Modules need
internet access; the mesh commands work even when pushes are off.

## Keeping the mesh quiet

The bot never floods the channel. Two limits, both tunable in the
dashboard's **Push budget** card:

- **Per person** — one requester gets at most one answer every 30 s,
  5 per hour and 15 per day (covers replies; pushes have no person).
- **Total** — everything the bot transmits (replies + pushes combined)
  keeps at least 30 s between transmissions, max 30 per hour and 250
  per day.

Busy day? Admins can raise the total budget with the **Budget up**
button (or DM `!up`): +30/hour and +150/day per use, up to 90/hour and
2200/day, easing back after 24 hours. **Budget down** (`!down`) cancels
the extras immediately. Over-budget messages are dropped and noted in
the activity feed; admin DMs are exempt.

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

For most people, the **no-code** route is enough — add a reply for a
keyword in `config.yaml`:

```yaml
replies:
  - keywords: ["call", "frequency"]
    text: "Weekly net: Sundays 20:00 local on #net."
```

If you want a brand-new command (like `!weather`), that means writing a
small Python handler. It's not something you need as a normal user — but
the full walkthrough lives in **[docs/ADDING_FEATURES.md](docs/ADDING_FEATURES.md)**
if you ever want it.

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
- The dashboard binds to `127.0.0.1` by default — local access only. To
  reach it from other machines, set `web.host: "0.0.0.0"` so any local
  address can reach it, and use a real dashboard password.

## License

MIT — see [LICENSE](LICENSE). Attribution for the third-party
libraries and fonts this project uses is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

MeshCore is by its authors; this project is independent of the
MeshCore and openHop projects.
