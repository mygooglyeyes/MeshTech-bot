
## Completion - 2026-09-14 evening: the switchover is DONE and reboot-proven

The runbook above records the hunt; this section is the operational
truth the box now runs on. Everything below was verified LIVE on
hilltop, including a full reboot (18:10, 2026-09-14).

### Final architecture (who owns what)

- **cleanmodem.service** (enabled, boots first): owns the SX1262 radio,
  listens on 127.0.0.1:5055 with two roles - controller (the bot:
  RX feed + exclusive TX + config) and observer (the openHop
  repeater: RX feed only, TX/config refused server-side).
- **meshtech-bot.service** (enabled): `radio_mode: "modem"`, connects
  as controller with `modem_token_file: /etc/cleanmodem/controller.token`.
- **openhop-repeater.service**: `radio_type: modem_tcp` ->
  127.0.0.1:5055, `modem_tcp.token` = the observer token's value.
- **meshtech-modem.service**: DISABLED and inactive. It can never
  contest the GPIO at boot again.

### The bugs that were actually killing it (chronological)

1. GPIO permissions / gpiod 1.x ABI break / RPi.GPIO shim build
   (fixed: gpiod>=2.0 wheels + `SupplementaryGroups=gpio spi`).
2. v0.0.160 - a failed init left an orphaned modem client; the retry
   made a second one and they fought over the single controller slot
   (fixed: failed init tears its client down).
3. v0.0.161 - modem-mode TX was dropped by a guard requiring the SPI
   radio object (`Radio not up - dropping TX`).
4. v0.0.162/0.0.164 - CAD issued from continuous RX is ignored
   (SetCad is STANDBY-only; dance RX->STDBY->CAD->RX) and SetCadParams
   was sent 4 of its 7 bytes.
5. v0.0.163/0.0.165 - read-window misalignment detour; final truth is
   `[garbage, status, data...]` (slice from byte 2).
6. **v0.0.166 - `en` pin 26 was never driven** (openHop's proven
   PiMesh-1W v2 map powers the radio stage through it).
7. **v0.0.167 - THE ROOT CAUSE: the opcode table was shifted by one.**
   Cross-checking LoRaRF-Python (vendored by openhop_core for this
   exact E22-900M30S) found: SetDIO3AsTcxoCtrl is 0x97 (we sent 0xD4 -
   nonexistent, so the 32 MHz clock never armed and EVERY clocked
   command EXEC_FAILed while register writes "succeeded"), SetTxParams
   is 0x8E, SetBufferBaseAddress is 0x8F, sync word is a REGISTER
   write to 0x0740 (no such command), CalibrateImage pairs are
   (0xE1,0xE9) for 902-928. A parity test now pins the table.
8. v0.0.168 - the server's ~30 s idle recycler dropped the controller
   every ~32 s on a quiet mesh (fixed: client PINGs every 15 s).
9. v0.0.169/0.0.170 - openhop_core's TCPLoRaRadio handshakes with
   SET_CONFIG + SET_CAD_PARAMS and treats a rejection as a dead link
   (reconnect-loop every 10 s). Observers now get an ECHO: SET_CONFIG
   is answered with the modem's LIVE config (never applied), CAD
   params are echoed too. Chip parameters stay controller-only.
10. v0.0.171 - the slowloris guard also recycled the live OBSERVER
    every ~60 s (its driver sends nothing after the handshake).
    Authenticated observers are now exempt from the idle timeout;
    dead ones are still reaped by TCP keepalive + slow-client guards.
11. v0.0.172 - **config.yaml controls the radio**: after the modem
    link comes up the bot (controller) pushes the mcp radio block via
    SET_CONFIG and logs the modem's live-config echo. `tx_power_dbm`
    in config.yaml is the single source of truth; modem.conf's value
    is only a boot default. Mismatch = loud log line, never silent.
12. v0.0.173 - the dashboard's TCP Push chip tells the truth: the
    modem pushes OBSERVER_STATE (0x72, one byte: observer count) to
    the controller on auth and on every observer join/leave. Green
    `TCP Push: live (N)` / red `TCP Push: no clients`; controller-only
    so openhop_core never sees unsolicited frames.

### The proven reboot sequence (2026-09-14 18:10)

On boot: `SX1262 up` at T+0, repeater authed as observer at T+3,
bot authed as controller at T+3, `Radio up via the modem` immediately,
repeater `initialized successfully` with zero reconnects. Verified
`is-enabled`: cleanmodem+bot enabled, meshtech-modem disabled.

Post-reboot verification one-liner:

```bash
uptime -p; systemctl is-active cleanmodem meshtech-bot meshtech-modem; \
sudo journalctl -u cleanmodem.service -b --no-pager | grep -E "SX1262|auth accepted" | head -3; \
sudo journalctl -u meshtech-bot -b --no-pager | grep -E "Radio config applied|Radio up" | head -3
```

### Healthy-stack signatures (what quiet looks like)

- cleanmodem metrics: `clients=2` (bot + repeater), `auth_fail` not
  climbing, `crc_err` at background, `irq→fanout p50<1ms`.
- NO `timed out (idle)`, NO `displaced`, NO `Config rejected`,
  NO `link down - retry` flapping (a single pair around a restart is
  that restart, not a flap).
- Handshake on every (re)connect: `auth accepted ... as observer` ->
  `observer config proposal: ... (kept ...)` -> `observer CAD params
  proposal (echoed)` - and that's it; the connection then stays up.
- The proposal line doubles as a config-drift alarm: it prints the
  repeater's believed config vs the modem's live config.

### Gotchas for the next person

- The repeater believes 18 dBm; make config.yaml agree (v0.0.172 push
  makes the modem follow config.yaml within seconds of bot start).
- `/api/status` needs dashboard auth (401 to bare curl) - read the
  TCP Push state from the dashboard UI or the bot log instead.
- The dashboard "Feed Off" wording is legacy: in modem mode the chip
  is TCP Push and reflects cleanmodem's live observer count.
- cleanmodem's conf parser is `=`-style ONLY (colon lines are silently
  skipped) - keep deploy/cleanmodem.conf.example as the template.
