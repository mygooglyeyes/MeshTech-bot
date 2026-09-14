# cleanmodem bench runbook (hilltop) - verify before switching

The goal: prove cleanmodem (v0.0.148) hears the real mesh at wire
speed, BEFORE anything in the live stack changes permanently.

Honest scope, stated up front:

- TODAY the BOT owns the radio (`radio_mode: "spi"` in the bot's
  config) and feeds the old modem process, which serves the
  visualization tool. The old modem has NO radio of its own.
- So benching cleanmodem on the real antenna means the BOT steps
  aside for 15-30 minutes: the bot service stops, cleanmodem takes
  the radio, we watch it hear traffic, then everything comes back.
- During the bench: no bot on the channels, no feed to the
  visualization tool. That is the whole cost, and it ends at the
  rollback step.
- TX through cleanmodem is NOT part of this bench. The bot's TX path
  gets verified at the switchover session, with its own rollback.

One step at a time. Paste each output back before moving on.

## Step 0 - get the branch onto hilltop

👁️ READ - The branch is committed locally (3808d11 + the DEV merge)
but NOT pushed, so the box cannot see it yet. After you say OK to the
push, the box gets it like any other branch. This does not touch the
running bot.

▶️ DO - After the push is done, on hilltop:

```bash
cd ~/meshtech-bot && sudo ./manage.sh update clean-modem
```

📋 PASTE - the update output.

## Step 1 - find the exact service names and paths

👁️ READ - I will not guess the service names, the python path, or the
user - the runbook must match your box exactly.

▶️ DO - On hilltop:

```bash
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater" ; systemctl cat meshtech-bot | grep -E "User=|WorkingDirectory=|ExecStart="
```

📋 PASTE - both outputs.

## Step 2 - token files

👁️ READ - The modem refuses every client without two passwords. They
live in mode-600 files (first line = password), created here as YOUR
user so the service can read them later. Nothing goes into any config
file.

▶️ DO - On hilltop:

```bash
mkdir -p ~/cleanmodem-etc && cd ~/cleanmodem-etc
openssl rand -hex 32 > observer.token
openssl rand -hex 32 > controller.token
chmod 600 observer.token controller.token
ls -l
```

📋 PASTE - the `ls -l` (both files should show `-rw-------`).

## Step 3 - modem.conf for the bench

👁️ READ - One page of settings; the radio numbers are already the
live mesh values. The two token lines must point at the files from
Step 2 - the example points elsewhere on purpose, so we fix them.

▶️ DO - On hilltop:

```bash
cp ~/meshtech-bot/deploy/cleanmodem.conf.example ~/cleanmodem-etc/modem.conf
nano ~/cleanmodem-etc/modem.conf
```

In nano, make these two lines read (full paths, your home dir):

```yaml
token_file: "/home/k6bps/cleanmodem-etc/observer.token"
controller_file: "/home/k6bps/cleanmodem-etc/controller.token"
```

Save (Ctrl-O, Enter) and exit (Ctrl-X).

📋 PASTE - `grep -E "host|port|token_file|controller_file|pin_profile|frequency" ~/cleanmodem-etc/modem.conf`

## Step 4 - the radio handover (the one disruptive step)

👁️ READ - Two processes cannot share the SPI radio, and the old modem
holds port 5055 that cleanmodem wants. So for the bench we stop the
bot service AND the old modem service. The visualization tool's
repeater stays up - it will just sit quiet, then reconnect at
rollback. The outage ends at Step 8.

▶️ DO - On hilltop (names from your Step 1 paste - I confirm them
with you first):

```bash
sudo systemctl stop meshtech-bot meshtech-modem
```

📋 PASTE - `systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater"`

## Step 5 - first start of cleanmodem

👁️ READ - cleanmodem takes the radio and arms RX. What success looks
like: `Radio up` (or similar) with no SPI errors. We run it in the
foreground so everything is visible; Ctrl-C stops it cleanly.

▶️ DO - On hilltop (python path from your Step 1 paste):

```bash
cd ~/meshtech-bot && sudo -u k6bps PYTHONPATH=/home/k6bps/meshtech-bot <PYBIN> -m cleanmodem --config /home/k6bps/cleanmodem-etc/modem.conf
```

(`<PYBIN>` = the ExecStart python from Step 1 - I fill it in with you.)

📋 PASTE - the startup lines (looking for `Radio up` or an error).

## Step 6 - RX parity: does it hear the mesh?

👁️ READ - The pass bar: cleanmodem hears the same traffic the old
stack logged, and crc errors stay at the mesh's normal background
level. The metrics line prints once a minute with the counts and the
wire-speed numbers. Leave it listening 10-15 minutes.

▶️ DO - Watch the terminal (or in a second ssh session):

```bash
sudo journalctl -n 40 --no-pager | grep -E "metrics|RX_PACKET"
```

(foreground run: just copy the `metrics:` lines from the terminal)

📋 PASTE - the metrics lines.

## Step 7 - latency (the "wire speed" claim, made into a number)

👁️ READ - The pass bar: p50 under ~20 ms and p99 under ~100 ms. The
radio's airtime for one packet is already hundreds of ms - these
numbers measure only the modem's internal path (IRQ to fan-out).

▶️ DO - Copy the `irq→fanout p50/p99` numbers from the Step 6 metrics
line.

📋 PASTE - them here (or "no traffic" - then we generate some).

## Step 8 - rollback (everything back the way it was)

👁️ READ - However the bench went, the live stack comes back now: the
old modem gets its port, the bot gets its radio, the visualization
tool reconnects, and the bot is back on the channels.

▶️ DO - On hilltop:

```bash
sudo systemctl start meshtech-modem meshtech-bot
```

(names confirmed from Step 1 first)

📋 PASTE - `systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater"`

## Pass bar summary

- `Radio up` with no SPI errors (Step 5)
- rx climbs with real mesh traffic; crc_err stays at background level (Step 6)
- irq→fanout p50 < ~20 ms, p99 < ~100 ms (Step 7)
- Rollback leaves the live stack exactly as it was (Step 8)

After a pass: the NEXT session does the switchover - bot config
flips to `radio_mode: "modem"`, token files move to their final
homes, cleanmodem gets a systemd unit, and TX through cleanmodem is
verified on the air. Own runbook, own rollback.
