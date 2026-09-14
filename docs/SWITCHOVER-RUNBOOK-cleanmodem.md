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
and no modem feed (the repeater just retries). Rollback is
self-contained at the bottom - no bench unit, no temporary tokens,
nothing to remember.

PROGRESS (2026-09-14, live session):

- Box updated: v0.0.153 @ 82147f3, live config validated, bot
  restarted. Backup made: `/opt/meshtech-bot/config.yaml.pre-modem`.
- Live config read: bot's own radio block ENABLED (SPI today);
  modem_host/port already 127.0.0.1:5055; modem_token_file ABSENT
  (add at the flip); modem_feed still on (goes inert in modem mode).
- Repeater recon: it reaches the OLD modem with an EMPTY token
  (`modem_tcp.token: ''` in /etc/openhop_repeater/config.yaml).
  cleanmodem fails closed on empty credentials, so branch (b): fresh
  observer token minted; the repeater's modem_tcp.token gets set at
  its reconnect step.
- Tokens installed: /etc/cleanmodem/{observer,controller}.token,
  mode 600, owned by meshtech.
- Handover executed 07:33: bot + old modem STOPPED, cleanmodem
  FAILING to start. OUTAGE IN PROGRESS.
- Failure 1: modem.conf root-owned 640 -> PermissionError. chown
  fixed.
- Failure 2 (REAL): the shipped example conf is COLON style
  (`key: value`) but the parser splits on `=`; colon lines are
  SILENTLY SKIPPED, defaults apply. Consequence: the BENCH also ran
  on defaults with tokens never loaded (worked only because the
  example's default token paths are unset - fail-closed would have
  refused everyone; investigate the bench auth story after the air
  is back). Only coding_rate's inline comment contains `=` so only
  that line raised `bad key`.
- Unblocked with a minimal `=`-style conf (token paths only; radio
  defaults are the mesh-proven numbers anyway). Repo fix for the
  example/parser mismatch AFTER the air is back.
- Failure 3 (CURRENT): `=`-conf passed config load, then radio
  bring-up failed: `No module named 'RPi'`. Root cause chain now
  clear: (1) venv had NO gpiod and NO RPi.GPIO (spidev only) -
  deps were never pinned for cleanmodem and tonight's deploy
  refresh pruned/resolved away whatever the bench had; (2) after
  installing gpiod 1.5.4 the service STILL fails because
  `_GpiodGpio()` cannot open its hardcoded `/dev/gpiochip0` as the
  meshtech user (probe: PermissionError naming /dev/gpiochip4);
  (3) the factory then falls back to RPi.GPIO -> `No module named
  RPi` -> crash loop.
- CHIP LAYOUT SOLVED: /dev/gpiochip0..1 exist (root:gpio 660),
  /dev/gpiochip4 is a SYMLINK to gpiochip0, board is Pi 4 Model B.
  `id meshtech` shows groups=meshtech ONLY - user is NOT in gpio
  (unit's SupplementaryGroups should have covered the service -
  recheck later; probes run without it).
- Group test (`sudo -g gpio -u meshtech`): NEW error -
  `TypeError: iter() returned non-iterator of type 'NoneType'`
  from gpiod's OWN pure-python shim (libgpiod/__init__.py
  gpiod_chip_iter.__iter__ returns None when ANY /dev/gpiochip*
  fails to open: the symlinked gpiochip4 -> gpiochip0... it opens
  both paths; one fails under this uid for an unrelated reason
  (ENODEV/EACCES on the second?) and the whole iter aborts ->
  gpiod_chip_open_by_label -> TypeError). This is a BUG in the
  gpiod 1.5.4 python bindings, not our code. CONCLUSION: the
  gpiod 1.x bindings are unusable here - the ROBUST path is the
  RPi.GPIO-compatible backend the driver already prefers on Pi.
  Plan: install `rpi-lgpio` (maintained RPi.GPIO-compatible shim on
  the lgpio stack; correct for Bookworm-era kernels; import name is
  RPi.GPIO so _RpiGpio works unchanged; must NOT coexist with real
  RPi.GPIO in the same venv - it isn't installed, fine). Also ensure
  the meshtech user reaches /dev/gpiochip* (unit already declares
  SupplementaryGroups=gpio spi - verify it lands), restart, and pin
  the dep in requirements after the air is back. rpi-lgpio pip
  install FAILED: its lgpio C dependency needs to compile against
  liblgpio-dev which the box lacks (cannot find -llgpio). ALTERNATIVE
  PATH: the OS package python3-rpi-lgpio (Debian/Ubuntu repo) or
  python3-rpi.gpio / RPi.GPIO classic. BEFORE more installs: check
  what GPIO python libs the OS already provides
  (dpkg -l | grep -i -E 'gpio|lgpio') - the venv can use
  --system-site-packages or the bench may have used OS python.
  RESULT: the box (Pi OS trixie, kernel 6.18) ALREADY SHIPS
  python3-rpi-lgpio 0.6 + python3-lgpio, and /usr/bin/python3
  imports RPi.GPIO fine. The venv just cannot see OS packages.
  ALSO recheck the bench evidence: bench ran the SAME venv - check
  manage.sh/requirements history for what changed (deploy
  dependency refresh).
- PLAN (Brett's call - KEEP THE VENV WALL): do NOT flip the service
  to system python. Instead give pip the missing build headers via
  apt (liblgpio-dev), then pip install rpi-lgpio INTO the venv -
  the compiled extension links the OS shared lib (like spidev)
  but the Python package set stays isolated. RESULT 08:0x:
  liblgpio-dev installed via apt, then `Successfully installed
  lgpio-0.2.2.0 rpi-lgpio-0.6` IN THE VENV. Wall intact.
- Step 4 PASSED 08:08:05 - `active (running)`, `SX1262 up: 910.525MHz
  SF7 BW62.5kHz CR4/5 20dBm sync=0x12 pre=32`, listening on
  127.0.0.1:5055, both tokens set. Radio handover complete.
  NEXT: flip the bot to radio_mode modem (Step 5).
- Step 5 config edit DONE: lines 280-281 now hold
  `radio_mode: "modem"` + `modem_token_file: "/etc/cleanmodem/controller.token"`.
  NOTE: live config's mcp block shows `enabled: true` LAST in the
  block (after cad_min) - the sed anchors on modem_port, safe. ALSO
  noted: live comment says clear-channel retries are `0.3s apart`
  (pre-jitter text) - stale comment, harmless.
  NEXT: restart meshtech-bot, expect `Radio up via the modem`.
- Step 5 PROBLEM: bot connects then drops every 30 s; cleanmodem
  logs auth_fail climbing (10 -> 14) and NO `auth accepted`. The bot
  is rejected at the raw-token handshake. Token mismatch suspect:
  the bot reads /etc/cleanmodem/controller.token as USER meshtech -
  the file is mode 600 owned by meshtech, so readable... BUT the
  bot service may run as a DIFFERENT user, or the token file has a
  trailing newline/whitespace mismatch (server strips, client
  strips...). Server strips `.strip()` on both paths; client sends
  `self.token` from `readline().strip()`. Next: verify what the bot
  actually reads - check the bot's journal for its own error line
  (`modem rejected the controller token`) and compare file content
  hash as the BOT user vs the SERVER user. Bot journal only shows a
  CancelledError line (grep window too narrow - client errors are
  log.debug, invisible at INFO). Reconnect pattern every 30 s with
  auth_fail +2 per cycle = the bot's two auth attempts rejected.
  NOTE: auth_fail jumps by 2 per connect (10->12->14) though client
  sends ONE attempt per connect... unless the client connects, gets
  0x00, closes, and the server ALSO sees the frame-based fallback?
  Or: BOTH modem_host 127.0.0.1:5055 connections are the bot, and
  the +2 comes from... NEXT: run the auth by hand: read the token
  file AS the bot's flow reads it and compare against what the
  server loaded (server logs tokens only as set/not-set). Hand
  probe: python on the box: open both files, print sha256 of
  first-line-stripped contents (never print the token). FINGERPRINTS
  BACK: controller.token = 902a6b79..., observer.token = 1aabb4d8...,
  both 64 hex chars, clean. Files are consistent, so WHERE is the
  mismatch? Server 'peek' logic: reads up to 256 bytes; first byte
  not 0xAA -> raw-token path. Bot sends its 64-char token raw.
  Hex chars are ASCII so first byte is '7' (0x37) - fine, raw path.
  Server strips, compares against controller_token loaded at START.
  Both read the SAME file... UNLESS the bot service read the token
  file BEFORE... no, bot reads at _modem_up each start.
  REMAINING SUSPECT: the bot is NOT actually using line 281 - e.g.
  the config RELOADED an older cached copy, or a duplicate mcp:
  block later in the file OVERRIDES ours (last block wins in YAML).
  NEXT: grep the FULL config for every mcp/modem_token_file
  occurrence (count them). COUNT: exactly ONE mcp block, lines
  271/280/281 - config is correct. CODE READ (client.py +
  server.py handshake): client sends token raw (64 ASCII hex,
  first byte 0x37 '7' != 0xAA) -> server raw-token path ->
  compare_digest(supplied, controller_token) - both stripped 64-
  char strings from THE SAME FILE -> must pass. But the pasted
  server log showed connect/disconnect pairs with NO 'raw-token
  auth rejected' and NO 'auth accepted', while auth_fail climbs +2
  per 30 s cycle. That pattern = the client is NOT the bot's
  ModemClient at all... OR the log window missed the reject lines.
  ALSO suspicious: +2 per cycle. auth_fail increments live at 4
  sites (raw reject, frame unauthorized, TX-by-observer, handle_auth
  reject). A 30 s cycle matches the client's reconnect backoff
  (RETRY_MIN... capped 30?). NEXT: grab an UNSOLICITED server log
  window (no grep) around one connect to see the full story.
  WAIT - reread the bot journal paste: the user's grep on the BOT
  log matched CLEANMODEM lines only because BOTH greps ran; the
  first grep (meshtech-bot) returned NOTHING - i.e. the bot unit
  produced NO modem/Radio-up lines in 30 lines. And the bot log's
  only line was a bare CancelledError from an earlier restart.
  HYPOTHESIS: the bot never got far enough to log - or journalctl
  -n 30 starts after them. The 30 s reconnect cadence: RETRY backoff
  caps at 30 s (RETRY_MAX_S presumably) - consistent with the bot
  client failing raw auth and backing off to 30 s retries
  (RETRY_MAX_S = 30 confirmed). CODE-WALK CONCLUSION: neither
  'raw-token auth rejected' nor 'auth accepted (raw token)' appears
  in the server log, and BOTH always log. So _raw_token_auth never
  runs => the connector's first byte is 0xAA (frame client) OR the
  connector is NOT the bot. 30 s cadence + 3 ms sessions + the
  repeater ON THIS BOX configured modem_tcp host 127.0.0.1:5055
  (pointing at the OLD modem!) => the RETRYING CONNECTOR IS THE
  REPEATER, not the bot. It speaks frames, sends its (empty) token,
  gets bounced; the bot's own connection may not even be attempted
  (or is fine). BOT STATUS SETTLES IT: the bot is CRASH-LOOPING
  (restart counter 73!) with `FATAL: GPIO Pin 6 is already in use`
  - the bot is STILL TRYING TO OWN THE RADIO over SPI (it never
  reached modem mode; its config change did not take effect... but
  lines 280-281 exist. WAIT: line 280 radio_mode is INSIDE the mcp
  block but the bot boots _radio_up -> checks radio_mode... unless
  the bot's RUNNING code is OLDER than the config (bot runs the
  deployed v0.0.153 - supports radio_mode). OR: the mcp block's
  YAML got broken by our sed insert (indentation!) - line 280/281
  printed WITHOUT visible indent in the paste - `radio_mode:` at
  column 3 (2 spaces) is right... but if sed put them at a different
  indent they'd be a NEW top-level key and radio_mode would default
  to spi! `sudo sed -n '278,282p' config.yaml | cat -A` will show
  exact bytes.  That's the next probe. ALSO SETTLED: the every-30s connector is
  the REPEATER (frame protocol, pre-auth commands bounce silently -
  auth_fail +2 per attempt with no log line, 3 ms sessions) probing
  its old modem address; harmless for now, gets its new token later.
  The bot meanwhile crash-loops in SPI mode (GPIO pin 6 held by
  cleanmodem) DESPITE config lines 280-281 - so either the config
  bytes are subtly wrong or the bot is reading another path.
  BYTES VERIFIED: both lines perfectly indented (2 spaces) inside
  the mcp block, config is CORRECT. So why does the bot boot SPI?
  => The bot process predates... no, restarted 08:24 (crash loop
  restarts every ~6 s). NEXT HYPOTHESIS: the config-validation/
  normalization layer or a YAML anchor/duplicate-key quirk - or
  the bot loads config from a DIFFERENT PATH than
  /opt/meshtech-bot/config.yaml (the deploy's apply step copies
  config from /home/k6bps/meshtech-bot? The unit's WorkingDirectory
  is /opt/meshtech-bot; default --config is relative config.yaml).
  Check: does ANOTHER config.yaml exist + which one does the
  running bot parse (bot.py --check prints the path!).
- ROOT CAUSE #4 FOUND IN CODE: core/config.py McpCfg builder
  (lines 716-777) NEVER READS radio_mode / modem_host / modem_port /
  modem_token_file from the YAML - it constructs McpCfg() with only
  the legacy fields, so every value stays at the dataclass default
  (radio_mode='spi', modem_token_file='data/.modem_token'). The
  config parser DROPS our two lines silently. The v0.0.151 deploy
  only checked 'live config has every documented setting' (config-
  sync adds keys to config.example.yaml, not the parser!). THE BOT
  CANNOT ENTER MODEM MODE ON THIS BUILD no matter what config says.
  This is the second half of the v0.0.148 incomplete-wiring story.
  Options: (a) hot-patch core/config.py on the box to parse the
  four keys, restart bot; (b) rollback now, fix in repo, redeploy.
  Given Brett is at the box and the outage is contained, (a) hot-
  patch = fastest path to a working stack; the proper fix ships
  next commit. ASK BRETT which. DECISION (Brett, 08:2x): option (b)
  - ROLL BACK NOW, fix the parser properly in the repo with tests,
  redeploy, redo the switchover. Rollback executed this session:
  live stack restored (bot SPI + old modem + repeater), cleanmodem
  stopped. Tokens/unit/conf at /etc/cleanmodem + the cleanmodem
  unit stay installed and harmless (disabled, stopped) for the
  rerun. Repo work for v0.0.154: parse the four mcp modem keys +
  validation + tests; also pin rpi-lgpio in requirements; fix
  cleanmodem.conf.example format; then ONE update on hilltop and
  the switchover reruns from Step 5. ROLLBACK EXECUTED AND VERIFIED:
  meshtech-modem active, meshtech-bot active, cleanmodem inactive -
  live stack is BACK, outage over (07:33-08:2x). Bench-runbook-style
  session record kept above.
- v0.0.154 BUILT (repo, tests green: 568 passed, 1 skipped): bot
  config parser now reads radio_mode/modem_host/modem_port/
  modem_token_file (validated; six new tests pin the wiring),
  cleanmodem.conf.example rewritten in = style, rpi-lgpio pinned in
  requirements with its liblgpio-dev build note. NEXT: commit/push
  (Brett's OK), ONE `manage.sh update clean-modem` on hilltop, then
  the switchover re-runs from Step 5 (modem side already in place:
  tokens, unit, =-conf).
- NEXT: `pip list` in the venv for gpiod / RPi.GPIO variants, install
  the right one, restart. RESULT: venv has spidev only (no gpiod, no
  RPi.GPIO) - the factory falls through to the RPi.GPIO import, which
  raises. gpiod==1.6 does not exist on PyPI (latest 1.x is 1.5.4; 2.x
  is the new incompatible API). gpiod==1.5.4 INSTALLED OK but the
  service still fails 07:49:40 - need the fresh traceback: likely a
  gpiod runtime error (chip access from the meshtech user? pin claim
  conflict with a held line?) or the next layer down. TRACEBACK
  SHOWED: still 'No module named RPi' - so _GpiodGpio() raised too,
  silently, and the factory fell through to _RpiGpio. gpiod 1.5.4 is
  INSTALLED and imports FINE as the service user (the AttributeError
  on __version__ was a probe artifact, not a failure). The REAL
  failure is the chip open: hand probe as meshtech ->
  `PermissionError [Errno 13] Permission denied: '/dev/gpiochip4'`.
  Note it names chip FOUR though the code opens chip ZERO
  (hardcoded in _GpiodGpio.__init__) - gpiod's lookup resolved the
  path to a different chip device, OR the Pi's header lives on
  gpiochip4 (newer kernel layouts). Either way: wrong/forbidden chip
  -> factory falls to RPi.GPIO -> missing -> crash loop. NEXT:
  capture chip list + board model + meshtech's groups to pick the
  fix (likely an RPi.GPIO-compatible shim install to match the
  bench, plus repo fix making the chip path/backend configurable).
  group may not apply to /dev/gpiochip0's owning group, or the node
  names the chip differently). NEXT: run the constructor AS the
  service user by hand.

## Preconditions

- Bench passed and rolled back (bench Steps 5-7 done; live stack up).
- Bench unit removed: `/etc/systemd/system/cleanmodem-bench.service`
  gone (`sudo rm ... && sudo systemctl daemon-reload` if not).
- The pin test `test_lbt_retry_delays_are_continuous` passed locally
  (13 passed) - the jitter property is already proven; the box work
  here verifies the WIRING, not the timing math.

## Step 0 - update the box to v0.0.153+ (DONE 2026-09-14)

`manage.sh update clean-modem` -> v0.0.153 @ 82147f3, config
validation OK, bot restarted. (Dashboard note from the validator,
unrelated to today: web.host is not loopback - plaintext HTTP on the
LAN; consider a TLS reverse proxy someday.)

## Step 1 - freeze the live stack (DONE 2026-09-14)

`config.yaml.pre-modem` backup made. Grep showed: mcp enabled
(radio_mode absent = spi), modem_host/port pre-set to 127.0.0.1:5055,
modem_token_file absent, modem_feed enabled (inert in modem mode).

## Step 2 - repeater credential recon (DONE 2026-09-14, branch (b))

Repeater unit reads `/etc/openhop_repeater/config.yaml`. It reaches
the old modem with an EMPTY token - cleanmodem refuses empty
credentials, so a fresh observer token was minted (Step 3) and the
repeater's `modem_tcp.token` will be set at the reconnect step.

## Step 3 - permanent config + tokens (DONE 2026-09-14, with findings)

- `/etc/cleanmodem/` holds observer.token + controller.token (mode
  600, meshtech-owned).
- LESSON: `sudo cp deploy/cleanmodem.conf.example` lands root-owned
  (the service could not read it -> PermissionError), AND the example
  is in the WRONG FORMAT for the parser (colon vs `=`). Fixed in the
  moment with a minimal `=`-style conf:

```bash
sudo bash -c 'cat > /etc/cleanmodem/modem.conf <<EOF
token_file=/etc/cleanmodem/observer.token
controller_file=/etc/cleanmodem/controller.token
EOF
chown meshtech:meshtech /etc/cleanmodem/modem.conf'
```

- Token files must be mode 600 or the loader refuses them (by
  design).

## Step 4 - the permanent unit + the handover (DONE 2026-09-14, IN FAILURE)

Unit installed as drafted below; bot + old modem stopped; cleanmodem
currently crash-looping on `No module named 'RPi'` (see PROGRESS).

The unit as installed (`/etc/systemd/system/cleanmodem.service`):

```ini
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
```

Pass bar when it finally starts: `active (running)`, `Radio up`, no
SPI errors, listening on port 5055.

## Step 5 - the bot flips to radio_mode: "modem" (PENDING)

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

## Step 6 - TX verification (on air, with the new jitter) (PENDING)

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

## Step 7 - repeater reconnect + one-hour soak (PENDING)

👁️ READ - The repeater's client must be pointed at the new observer
token (branch (b)): edit `/etc/openhop_repeater/config.yaml`,
`modem_tcp.token:` <- the observer token's value (read it once with
`sudo cat /etc/cleanmodem/observer.token`), restart the repeater, and
it reconnects to port 5055 (now cleanmodem). Then the box runs
unattended for an hour: the metrics line every minute is the verdict -
RX parity with the bench numbers (~8-12 unique packets/min on this
mesh), crc_err at background (~one per couple of minutes), and no auth
failures.

▶️ DO - After at least 60 minutes of real traffic:

```bash
systemctl list-units --type=service --state=running | grep -Ei "meshtech|modem|repeater|cleanmodem"
sudo journalctl -u cleanmodem.service --since "-70 min" --no-pager | grep metrics | tail -5
```

📋 PASTE - the units list (cleanmodem + meshtech-bot + repeater
running, meshtech-modem ABSENT) and the last 5 metrics lines.

## Step 8 - make it permanent (PENDING)

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

Branch-(b) rollback note: the repeater reached the OLD modem with an
empty token - restoring that means setting `modem_tcp.token: ''`
back in `/etc/openhop_repeater/config.yaml` and restarting the
repeater. That is the only extra file.

## Follow-ups owed after the air is back (repo fixes)

0. URGENT: core/config.py's McpCfg builder drops radio_mode,
   modem_host, modem_port, modem_token_file from YAML (always
   defaults) - the bot can never enter modem mode. Parse the four
   keys + validation (radio_mode in {spi, modem}; modem mode
   requires modem_token_file) + tests. Ship before/with the
   switchover rerun.

1. `deploy/cleanmodem.conf.example` is colon-style but the parser
   splits on `=`: colon lines are silently skipped (defaults apply,
   tokens never load) and any colon line whose comment contains `=`
   raises `bad key`. Fix the example to `=` style (or teach the
   parser colon style) + a test that the example parses.
2. The GPIO dependency (gpiod v1 API or RPi.GPIO) is not in
   requirements.txt; the venv lost it (prime suspect: the deploy
   dependency refresh). Pin it and re-add.
3. Bench auth story: the bench conf was colon-style, so token files
   never loaded - work out what actually authenticated during the
   bench and fix the runbook note if needed.

## Pass bar summary

- Box on v0.0.153+ before any TX (Step 0) - DONE
- cleanmodem permanent unit `active (running)`, `Radio up` (Step 4) -
  IN PROGRESS (GPIO dep)
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
