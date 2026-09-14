# cleanmodem switchover runbook (hilltop) - cleanmodem takes the radio for good

Follows a PASSED bench (`BENCH-RUNBOOK-cleanmodem.md`). The bench proved
cleanmodem hears the mesh at wire speed. This session makes it permanent:

- cleanmodem gets its own systemd unit and owns the radio on port 5055.
- The bot flips to `radio_mode: "modem"` and rides cleanmodem as its
  controller client (RX feed + exclusive TX over TCP).
- The OLD modem (`meshtech-modem`) is retired from the box.
- TX through cleanmodem is verified ON AIR, including the restored
  continuous LBT jitter (v0.0.152+).
- The repeater (`openhop-repeater`) must end up reconnected as observer.

Honest scope: during the handover there is a short window with no bot
and no modem feed (the repeater just retries). The outage starts at
Step 4 and ends when Step 5 completes. Rollback is self-contained at
the bottom - no bench unit, no temporary tokens, nothing to remember.

One step at a time. Paste each output back before moving on.

PROGRESS (2026-09-14): runbook drafted; nothing executed yet.

## Preconditions

- Bench passed and rolled back (bench Steps 5-7 done; live stack up).
- Bench unit removed: `/etc/systemd/system/cleanmodem-bench.service`
  gone (`sudo rm ... && sudo systemctl daemon-reload` if not).
- The pin test `test_lbt_retry_delays_are_continuous` passed locally
  (13 passed) - the jitter property is already proven; the box work
  here verifies the WIRING, not the timing math.

## Step 0 - update the box to v0.0.153+

👁️ READ - The bench ran v0.0.151. The continuous jitter restore
(v0.0.152) and the reserved-knob docs (v0.0.153) landed after it, so
the box must update BEFORE cleanmodem ever transmits - the whole point
of the jitter is de-correlating retries from the other bots' timing.

▶️ DO - On hilltop (bench must be rolled back first):

```bash
sudo /opt/meshtech-bot/manage.sh update clean-modem
grep __version__ /opt/meshtech-bot/core/version.py
git -C /opt/meshtech-bot log --oneline -2
```

📋 PASTE - the three outputs. Expect `0.0.153` (or newer) and commit
`548201b` or later. Stop here if not.

## Step 1 - freeze the live stack (backup + baseline)

👁️ READ - Rollback is just restoring one file and restarting two
services, but only if we snapshot the current config first.

▶️ DO - On hilltop:

```bash
sudo cp /opt/meshtech-bot/config.yaml /opt/meshtech-bot/config.yaml.pre-modem
grep -nE "enabled|radio_mode|modem_" /opt/meshtech-bot/config.yaml | sed -n '/mcp/,$p'
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater"
```

📋 PASTE - the grep block (I need to see whether `mcp:` is enabled and
whether `modem_token_file` is already set) and the running-units list.

## Step 2 - repeater credential recon (decides the observer token)

👁️ READ - The repeater today authenticates to the OLD modem with some
credential. If we find where it lives, cleanmodem can accept the SAME
credential and the repeater needs ZERO changes - and rollback becomes
trivial. If not, we mint a new observer token and touch the repeater's
config once.

▶️ DO - On hilltop:

```bash
systemctl cat openhop-repeater --no-pager
sudo grep -rn "token\|password\|secret" /opt/openhop* 2>/dev/null | grep -v Binary | head -20
```

📋 PASTE - both outputs (redact nothing - these are local mode-600
files, not committed secrets). Decision after the paste:
- (a) a token FILE exists that the repeater reads -> cleanmodem's
  `token_file` points at that same file; repeater untouched.
- (b) only an inline password -> we write it into a fresh mode-600
  file in Step 3 and edit the repeater's config once.

## Step 3 - permanent config + tokens (/etc/cleanmodem)

👁️ READ - The documented install path: config at
`/etc/cleanmodem/modem.conf`, tokens beside it, mode 600, owned by
`meshtech`. The radio numbers come straight from the proven bench
conf. The controller token is ONE file both sides read: cleanmodem to
accept TX, the bot (mcp.modem_token_file) to send it.

▶️ DO - On hilltop (observer token per the Step 2 decision; the block
below shows branch (a) - point at the repeater's existing file; for
branch (b) generate a fresh one and put the same password in the
repeater's config):

```bash
sudo mkdir -p /etc/cleanmodem
sudo bash -c 'umask 077; openssl rand -hex 32 > /etc/cleanmodem/controller.token'
# Branch (a) only - reuse the repeater's existing credential:
sudo cp <repeater-token-file> /etc/cleanmodem/observer.token
# Branch (b) only:
# sudo bash -c 'umask 077; openssl rand -hex 32 > /etc/cleanmodem/observer.token'
sudo chown -R meshtech:meshtech /etc/cleanmodem
sudo cp /opt/meshtech-bot/deploy/cleanmodem.conf.example /etc/cleanmodem/modem.conf
sudo nano /etc/cleanmodem/modem.conf
```

In nano, make these lines read (everything else stays at the proven
bench values - radio numbers already match the live mesh):

```yaml
token_file: "/etc/cleanmodem/observer.token"
controller_file: "/etc/cleanmodem/controller.token"
```

Save (Ctrl-O, Enter), exit (Ctrl-X). Note: `lbt_max_attempts` may
appear in the conf - it is RESERVED (v0.0.153), changing it does
nothing; leave it alone.

📋 PASTE - `sudo ls -l /etc/cleanmodem` and
`sudo grep -E "host|port|token_file|controller_file" /etc/cleanmodem/modem.conf`

## Step 4 - the permanent unit + the handover (the disruptive step)

👁️ READ - Same handover as the bench, but installing the REAL unit
from `deploy/cleanmodem.service` (placeholders filled for hilltop).
Two processes cannot share the SPI radio, and the old modem holds
port 5055 - so bot + old modem stop, cleanmodem starts. The repeater
retries throughout.

▶️ DO - On hilltop:

```bash
sudo tee /etc/systemd/system/cleanmodem.service > /dev/null <<'EOF'
[Unit]
Description=cleanmodem - standalone LoRa modem (owns the radio; bot connects as controller)
Documentation=file:///opt/meshtech-bot/docs/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshtech
Group=meshtech
WorkingDirectory=/opt/meshtech-bot
ExecStart=/opt/meshtech-bot/.venv/bin/python -m cleanmodem --config /etc/cleanmodem/modem.conf
Restart=on-failure
RestartSec=5
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/meshtech-bot/data
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
sudo systemctl start cleanmodem.service
sleep 5
systemctl status cleanmodem.service --no-pager -n 15
```

📋 PASTE - the status output. Pass bar: `active (running)`, `Radio up`,
no SPI errors, and the listen line showing port 5055.

## Step 5 - the bot flips to radio_mode: "modem"

👁️ READ - One config change and a restart: the bot abandons SPI,
connects to cleanmodem on 127.0.0.1:5055 as controller, and everything
downstream (decode, replies, dashboard) works unchanged. The bot's
`modem_feed:` section becomes inert (the modem serves observers
directly) - leave it as is.

▶️ DO - On hilltop:

```bash
sudo nano /opt/meshtech-bot/config.yaml
```

In the `mcp:` block set (uncomment `modem_host` / `modem_port` if
commented; add `radio_mode` - it is new since the box last looked):

```yaml
mcp:
  enabled: true
  radio_mode: "modem"
  modem_host: "127.0.0.1"
  modem_port: 5055
  modem_token_file: "/etc/cleanmodem/controller.token"
```

Save, exit, then:

```bash
sudo systemctl restart meshtech-bot
sleep 8
sudo journalctl -u meshtech-bot -n 30 --no-pager | grep -E "modem|Radio up|identity"
sudo journalctl -u cleanmodem.service -n 15 --no-pager | grep -E "auth|client"
```

📋 PASTE - both journal greps. Pass bar: bot logs `Radio up via the
modem - the MCP drives the air.` and cleanmodem logs an authenticated
controller client. Do NOT proceed to TX testing before this line.

## Step 6 - TX verification (on air, with the new jitter)

👁️ READ - What "TX works" means, checked in order:
1. A bot reply leaves through cleanmodem (TX_DONE with real airtime).
2. The TX loopback reaches the repeater (it logs the bot's own packet
   as RX - a radio never hears itself; the loopback replaces that).
3. When the channel is busy, the clear-channel wait engages and the
   jittered retries still end in a clean TX (or the 4.0 s cap with the
   explicit may-collide warning - same semantics the old stack had).
   The continuous 0.10-0.30 s spacing itself is proven by the local
   pin test; on the box we verify the loop behaves (waits, then sends).

▶️ DO - From a phone on the mesh, DM the bot a command that forces a
reply (e.g. `!nodes`). Then on hilltop:

```bash
sudo journalctl -u cleanmodem.service -n 40 --no-pager | grep -E "TX|clear-channel|metrics"
sudo journalctl -u openhop-repeater -n 20 --no-pager | grep -iE "rx|tx"
```

📋 PASTE - both. Expected: one `TX_DONE`-path line with airtime in the
hundreds of ms, `tx=` climbing in the metrics line, the bot's packet
visible in the repeater's RX (loopback), and an ACK/reply heard back.
If the mesh was busy when you sent, you will also see
`clear-channel wait engaged before TX` - that line is the jitter loop
working; paste it if you get it.

## Step 7 - repeater reconnect + one-hour soak

👁️ READ - The repeater's client should reconnect to port 5055 (now
cleanmodem) on its own. Then the box runs unattended for an hour: the
metrics line every minute is the verdict - RX parity with the bench
numbers (~8-12 unique packets/min on this mesh), crc_err at background
(~one per couple of minutes), and no auth failures.

▶️ DO - After at least 60 minutes of real traffic:

```bash
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater|cleanmodem"
sudo journalctl -u cleanmodem.service --since "-70 min" --no-pager | grep metrics | tail -5
```

📋 PASTE - the units list (cleanmodem + meshtech-bot + repeater
running, meshtech-modem ABSENT) and the last 5 metrics lines.

## Step 8 - make it permanent

👁️ READ - Only after the soak passes: enable cleanmodem at boot,
retire the old modem's unit, and keep the config backup for rollback.

▶️ DO - On hilltop:

```bash
sudo systemctl enable cleanmodem.service
sudo systemctl disable meshtech-modem 2>/dev/null || true
git -C /opt/meshtech-bot status --short
```

📋 PASTE - the enable/disable output. Done - cleanmodem owns the air;
the bot rides it; the repeater watches.

## Rollback (works from ANY step, self-contained)

👁️ READ - Back to exactly this morning: bot owns the radio over SPI,
old modem holds port 5055, repeater reconnects to it. Nothing else
needs remembering.

▶️ DO - On hilltop:

```bash
sudo systemctl stop cleanmodem.service meshtech-bot
sudo cp /opt/meshtech-bot/config.yaml.pre-modem /opt/meshtech-bot/config.yaml
sudo systemctl start meshtech-modem meshtech-bot
sleep 8
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater|cleanmodem"
sudo journalctl -u meshtech-bot -n 20 --no-pager | grep -E "Radio up|spi"
```

📋 PASTE - the running-units list (cleanmodem absent, the other three
present) and the bot log showing the SPI radio up.

Branch-(b) rollback note: if the repeater's config was edited in
Step 2(b), revert that edit too - that is the only extra file.

## Pass bar summary

- Box on v0.0.153+ before any TX (Step 0)
- cleanmodem permanent unit `active (running)`, `Radio up` (Step 4)
- Bot logs `Radio up via the modem` (Step 5)
- TX on air: airtime numbers, loopback seen by the repeater, mesh ACK
  heard back (Step 6)
- Soak: rx parity with bench, crc_err at background, no auth failures
  (Step 7)
- Rollback proven available at every point (bottom section)

## Dead knobs in radio_mode: "modem" (do not tune)

- `mcp.cad_peak` / `mcp.cad_min` / `precheck_cad_*` /
  `inter_packet_politeness_seconds` / `clear_channel_wait_seconds`:
  SPI-mode-only. In modem mode the MODEM owns LBT + politeness
  (`cleanmodem` conf: `cad_peak`, `cad_min`, `politeness_seconds`,
  `clear_channel_wait_seconds`).
- `lbt_max_attempts` (cleanmodem conf): reserved, no effect (v0.0.153).
- `modem_feed:`: inert in modem mode (logged at start, by design).
