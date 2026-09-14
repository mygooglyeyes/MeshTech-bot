# cleanmodem bench runbook (hilltop) - verify before switching

The goal: prove cleanmodem (v0.0.151) hears the real mesh at wire
speed, BEFORE anything in the live stack changes permanently.

Honest scope, stated up front:

- The BOT owns the radio today (`radio_mode` defaults to "spi") and
  feeds the old modem process, which serves the visualization tool.
  The old modem has NO radio of its own.
- Benching cleanmodem on the real antenna means the BOT and the OLD
  MODEM step aside for 15-30 minutes: their services stop, cleanmodem
  takes the radio, we watch it hear traffic, then everything comes
  back. During the bench there is no bot on the channels and no feed
  to the visualization tool. That is the whole cost, and it ends at
  the rollback step.
- TX through cleanmodem is NOT part of this bench (verified at the
  switchover session, with its own runbook).

Box facts confirmed on hilltop (Step 1 paste, 2026-09-14):

- services: meshtech-bot (radio owner), meshtech-modem (old modem,
  holds port 5055), openhop-repeater (observer, stays up)
- bot service runs as User=meshtech, /opt/meshtech-bot,
  .venv/bin/python

One step at a time. Paste each output back before moving on.

PROGRESS (2026-09-14, after a lost session): Steps 2-4 are DONE -
token files created, modem.conf written, radio handover executed.
cleanmodem-bench holds the radio RIGHT NOW; the bot and old modem are
stopped until the Step 7 rollback. NEXT: Step 5 metrics paste.

## Step 0 - branch on the box (DONE 2026-09-14)

`manage.sh update clean-modem` succeeded on the second attempt
(v0.0.151 after the config-sync incident fix). Box runs
clean-modem@6a0d9b1 with radio_mode still "spi".

## Step 1 - service names + unit details (DONE 2026-09-14)

Paste received and recorded above.

## Step 2 - token files (service-readable location) - DONE 2026-09-14

👁️ READ - The modem refuses every client without two passwords. They
live as mode-600 files owned by the SERVICE user (meshtech), in the
bot's own data area - the bench unit will read them from there.
Nothing goes into any config file.

▶️ DO - On hilltop:

```bash
sudo mkdir -p /opt/meshtech-bot/data/cleanmodem-bench
sudo bash -c 'umask 077; openssl rand -hex 32 > /opt/meshtech-bot/data/cleanmodem-bench/observer.token; openssl rand -hex 32 > /opt/meshtech-bot/data/cleanmodem-bench/controller.token; chown -R meshtech:meshtech /opt/meshtech-bot/data/cleanmodem-bench'
ls -l /opt/meshtech-bot/data/cleanmodem-bench
id meshtech
```

📋 PASTE - the `ls -l` (two `-rw-------` files owned by meshtech) and
the `id meshtech` line (I want to see whether gpio/spi groups ride
along; the bench unit carries them explicitly either way).

## Step 3 - modem.conf for the bench - DONE 2026-09-14

👁️ READ - One page of settings; the radio numbers already match the
live mesh. The two token lines must point at the Step 2 files.

▶️ DO - On hilltop:

```bash
sudo cp /opt/meshtech-bot/deploy/cleanmodem.conf.example /opt/meshtech-bot/data/cleanmodem-bench/modem.conf
sudo nano /opt/meshtech-bot/data/cleanmodem-bench/modem.conf
```

In nano, make the two token lines read:

```yaml
token_file: "/opt/meshtech-bot/data/cleanmodem-bench/observer.token"
controller_file: "/opt/meshtech-bot/data/cleanmodem-bench/controller.token"
```

Save (Ctrl-O, Enter), exit (Ctrl-X).

📋 PASTE - `sudo grep -E "host|port|token_file|controller_file|pin_profile|frequency" /opt/meshtech-bot/data/cleanmodem-bench/modem.conf`

## Step 4 - the bench unit + the radio handover (the disruptive step) - DONE 2026-09-14

👁️ READ - Two processes cannot share the SPI radio, and the old modem
holds port 5055. We stop bot + old modem, then install a small bench
unit for cleanmodem that carries the same radio-access flags as the
proven bot unit (PrivateDevices=false, gpio+spi groups). The repeater
stays up and simply retries its connection. The outage ends at Step 8.

▶️ DO - On hilltop (writes the unit, then does the handover):

```bash
sudo tee /etc/systemd/system/cleanmodem-bench.service > /dev/null <<'EOF'
[Unit]
Description=cleanmodem bench (temporary - RX parity test)
After=network-online.target

[Service]
Type=simple
User=meshtech
Group=meshtech
WorkingDirectory=/opt/meshtech-bot
Environment=PYTHONPATH=/opt/meshtech-bot
ExecStart=/opt/meshtech-bot/.venv/bin/python -m cleanmodem --config /opt/meshtech-bot/data/cleanmodem-bench/modem.conf
Restart=on-failure
RestartSec=5
PrivateTmp=true
ProtectSystem=full
PrivateDevices=false
SupplementaryGroups=gpio spi
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl stop meshtech-bot meshtech-modem
sudo systemctl start cleanmodem-bench
sleep 5
systemctl status cleanmodem-bench --no-pager -n 15
```

📋 PASTE - the status output (looking for `active (running)` and
`Radio up` in the log lines, or any SPI error).

## Step 5 - RX parity: does it hear the mesh? - NEXT (paste metrics)

👁️ READ - Pass bar: cleanmodem hears the same traffic the old stack
logged, and CRC errors stay at the mesh's normal background level.
The metrics line prints once a minute with the counts and the
wire-speed numbers. Leave it listening 10-15 minutes.

▶️ DO - On hilltop, after 10-15 minutes of real traffic:

```bash
sudo journalctl -u cleanmodem-bench -n 40 --no-pager | grep -E "metrics|RX_PACKET"
```

📋 PASTE - the metrics lines.

## Step 6 - latency (the "wire speed" claim, made into a number)

👁️ READ - Pass bar: p50 under ~20 ms and p99 under ~100 ms. Radio
airtime for one packet is already hundreds of ms - these numbers
measure only the modem's internal path (IRQ to fan-out).

▶️ DO - Copy the `irq→fanout p50/p99` numbers from the Step 5 metrics
line.

📋 PASTE - them here (or "no traffic" - then we generate some).

## Step 7 - rollback (everything back the way it was)

👁️ READ - However the bench went, the live stack comes back now: the
old modem gets its port back, the bot gets its radio, the
visualization tool reconnects, and the bench unit is stopped.

▶️ DO - On hilltop:

```bash
sudo systemctl stop cleanmodem-bench
sudo systemctl start meshtech-modem meshtech-bot
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater|cleanmodem"
```

📋 PASTE - the running-units list (cleanmodem-bench must be ABSENT,
the other three present).

## Pass bar summary

- `Radio up` with no SPI errors (Step 4)
- rx climbs with real mesh traffic; crc_err stays at background (Step 5)
- irq→fanout p50 < ~20 ms, p99 < ~100 ms (Step 6)
- Rollback leaves the live stack exactly as it was (Step 7)

After a pass: the bench unit can be removed (`sudo rm
/etc/systemd/system/cleanmodem-bench.service && sudo systemctl
daemon-reload`) and the NEXT session does the switchover - bot config
flips to `radio_mode: "modem"`, tokens move to their final homes,
cleanmodem gets its permanent systemd unit, and TX through cleanmodem
is verified on the air. Own runbook, own rollback.
