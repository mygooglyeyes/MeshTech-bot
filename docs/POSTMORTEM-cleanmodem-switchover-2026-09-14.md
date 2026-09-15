# Post-mortem: cleanmodem switchover outage - hilltop, 2026-09-14

Companion documents: `SWITCHOVER-RUNBOOK-cleanmodem.md` (what happened,
step by step, now marked DONE) and `BENCH-RUNBOOK-cleanmodem.md` (the
bench this switchover followed). This document is the analysis: the root
cause, every fix shipped that day, and the debugging method that worked.

---

## 1. Summary

The bot's radio was moved out of the bot process into a standalone
modem process (`cleanmodem`) as part of a planned switchover. What was
planned as a short handover became a ~9.5-hour radio outage, because
**cleanmodem's SX126x opcode table was wrong in four places** - most
critically `SetDIO3AsTcxoCtrl`, sent as a nonexistent command (0xD4
instead of 0x97), so the chip's 32 MHz TCXO clock never armed. The chip
sat in STANDBY_RC all day: it answered every SPI register write
("bring-up succeeded"), while every operation that needs the radio
clock - Calibrate, SetRx, SetCad, SetTx - was silently rejected with an
EXEC_FAIL status the driver did not check.

The radio was never broken. The E22-900M30S module, the wiring, the
GPIO, and the SPI bus were fine from the start; a driver written from
datasheet notes had a shifted command table, and every layer above it
reported success.

Resolution came from **diffing the opcode table against LoRaRF-Python**,
a proven driver for the same silicon that was already sitting on the
same box (vendored by openhop_core, running the identical radio
hardware). Four wrong entries were found in minutes after hours of
first-principles probing. Twenty versions (v0.0.154 - v0.0.173) shipped
the same day; the stack ended boot-proven with all three consumers on
one radio.

## 2. Impact

- **Bot off the air 07:33 - ~16:42** (first TX over the air: flood +
  direct adverts). First successful DM answer ~17:05. Total ~9.5 h.
- The openHop repeater's mesh visibility was lost with the old modem
  feed and restored at ~17:29 as a cleanmodem observer.
- No data loss beyond the outage window (bot DB intact; the mesh
  carried on without the bot).
- The old stack (`meshtech-modem`) was deliberately held back as a
  rollback path all day; it was never needed, and was disabled only
  after the new stack proved itself.

## 3. Timeline (all times PDT, 2026-09-14)

| Time | Event |
|---|---|
| 07:33 | Handover: bot + old modem stopped. cleanmodem fails: `modem.conf` is root-owned 640 -> PermissionError. **Outage begins.** |
| 07:35 | Second config failure: shipped example conf is colon-style (`key: value`) but the parser splits on `=`; colon lines silently skipped. Only an inline comment containing `=` surfaced it, as a `bad key` crash. |
| 07:39-08:08 | Environment chain, one link at a time: `No module named 'RPi'` -> gpiod 1.6 does not exist on PyPI -> 1.5.4 installed -> ABI-broken on Debian 13 -> `PermissionError: /dev/gpiochip4` (user not in `gpio`) -> rpi-lgpio wheel build fails (`cannot find -llgpio`) -> `liblgpio-dev` installed. **08:08: cleanmodem starts and logs `SX1262 up` - the radio is actually deaf.** |
| 08:13-08:22 | Bot connects as controller; duplicate client fight observed (two clients displacing each other from the single controller slot; `auth_fail` climbing). |
| ~08:24 | v0.0.154 deployed. The bot's config parser had never read `radio_mode`/`modem_*` - it silently stayed in SPI mode and crash-looped ("GPIO Pin 6 is already in use") against cleanmodem. Fix makes modem mode actually take effect. |
| 09:08 | Bot up in modem mode; startup adverts dropped (`Radio not up - dropping TX`). |
| 09:16-09:22 | Controller link flaps every ~32 s: the server's ~30 s idle recycler vs. a controller that only speaks when it transmits. |
| 10:13-10:27 | v0.0.155/156 diagnostics land: `irq: polls=2388 edges=0 flags=0xAA00 poll_mode=True` - polls advance, DIO1 edges never fire, flags are the 0xAA status-byte-garbage signature. Deafness is now *visible* but not yet explained. |
| 10:55-11:58 | gpiod v1/v2 API saga (v0.0.156-159) plus a clean-venv rebuild of every dependency against requirements.txt. |
| 12:00-12:28 | Lifecycle fixes (v0.0.160: orphaned clients; v0.0.161: modem-mode TX guard). |
| 12:33-14:30 | CAD and read-window fixes (v0.0.162-165): SetCAD is STANDBY-only (RX->STDBY->CAD->RX dance), SetCadParams was truncated to 4 of 7 bytes, and the command-read window was misaligned by one byte - then *mis-corrected* by one byte in the other direction (v0.0.165) before reverting to the datasheet layout (v0.0.166). |
| 15:33 | **The chip names the disease.** A per-command status trace shows every clocked command returning EXEC_FAIL from STANDBY_RC while config/register writes succeed. The `en` power-enable pin (26) is also added (v0.0.166) - real bug, not the root cause. |
| ~16:30 | **LoRaRF-Python cross-check.** The proven driver for this exact module is vendored on this very box. Opcode diff: TCXO 0x97 not 0xD4, TxParams 0x8E not 0x8D, BufBase 0x8F not 0x8E, no SetSyncWord command at all (register 0x0740), CalibrateImage pairs wrong for the band. |
| 16:41 | v0.0.167 deployed with the corrected table. |
| 16:42-16:43 | `rx=8 tx=2` on the first metrics line, adverts on the air. **The radio works for the first time.** |
| 16:44-17:05 | `rx=392 tx=9`; live mesh adverts decoded; a DM is answered end-to-end. |
| 17:15-17:29 | Repeater onboarding (v0.0.169/170): observer SET_CONFIG / SET_CAD_PARAMS echo handshake - openhop_core's driver treats a config rejection as a dead link and reconnect-loops. |
| 17:48-17:57 | v0.0.171: observers exempt from the idle recycler. Nine minutes, `clients=2`, zero drops. |
| 18:10 | **Reboot proof:** `SX1262 up` at T+0, both clients authed by T+3, repeater initialized with zero reconnects, old modem stays off. |
| 18:35 | v0.0.172: bot pushes `config.yaml` radio settings to the modem (18 dBm applied; config authority moves to one file). |
| later | v0.0.173: modem pushes observer state to the controller; the dashboard TCP Push chip reports real liveness, verified green -> red -> green. |

## 4. Root cause

### 4.1 The bug

The SX126x command opcodes in `cleanmodem/sx126x.py` were wrong in four
places (and the CalibrateImage band table in a fifth):

| Command | We sent | Truth | Effect |
|---|---|---|---|
| SetDIO3AsTcxoCtrl | **0xD4** | **0x97** | Unknown opcode -> rejected -> 32 MHz TCXO never armed |
| SetTxParams | **0x8D** | **0x8E** | TX power never configured |
| SetBufferBaseAddress | **0x8E** | **0x8F** | Buffer base never set |
| "SetSyncWord" | **0x8F** (as a command) | **does not exist** - sync word is a register write to 0x0740 | Sync word was never set; the "command" was a stray buffer-pointer write |
| CalibrateImage | `(0x7B, 0x81)` | `(0xE1, 0xE9)` for 902-928 | Invalid pair -> rejected |

The 0x8D/0x8E/0x8F block is shifted by one - the classic signature of a
table typed from memory, where a gap in the datasheet's opcode sequence
(0x8D is unassigned) was silently consumed.

### 4.2 Why it presented the way it did

With no TCXO there is no 32 MHz radio clock, and the chip splits
cleanly into two command classes:

- **Succeed:** anything that is pure configuration/standby work -
  SetStandby, SetPacketType, SetRfFrequency, SetModulationParams,
  SetPacketParams, and every register write. These do not need the
  clock.
- **EXEC_FAIL:** anything clocked - Calibrate, CalibImage, SetRx,
  SetCad, SetTx.

So bring-up *half-succeeded*: the driver's init wrote frequency,
modulation, and packet params correctly (those opcodes were right),
logged `SX1262 up`, and started an RX loop that would never see a
packet and a CAD that would never complete. The chip answered every
transaction with a valid status byte. Nothing threw.

The EXEC_FAIL signature was there all along - bit flags in the status
byte - but the driver never checked per-command status, so the
rejections were invisible in normal operation for roughly seven hours.

### 4.3 The decisive evidence

The probe output that ended the hunt (v0.0.166-era trace, excerpt):

```
SetStandby RC            status=22 mode=STBY_RC  cmd=-
DIO3 TCXO 1.8V           status=2a mode=STBY_RC  cmd=EXEC_FAIL
DIO2 RF switch           status=22 mode=STBY_RC  cmd=-
Calibrate 0x7F           status=2a mode=STBY_RC  cmd=EXEC_FAIL
CalibImage 915           status=2a mode=STBY_RC  cmd=EXEC_FAIL
SetPacketType            status=22 mode=STBY_RC  cmd=-
SetRfFrequency           status=22 mode=STBY_RC  cmd=-
...
SetCad                   status=2a mode=STBY_RC  cmd=EXEC_FAIL
```

Two further probes pinned it:

- **The 1 ms rejection.** `TcxoCtrl` at 1.8 V/3.3 V/any timeout all
  held BUSY for exactly `1.0 ms` before EXEC_FAIL - an unknown opcode
  is rejected before the chip does any work, whereas a real TCXO
  command holds BUSY for its full settle time.
- **The cross-check.** `grep` in
  `/opt/openhop_repeater/venv/.../openhop_core/hardware/lora/LoRaRF/SX126x.py`
  - a driver with years of field use on this exact E22 module - showed
  `0x97`, `0x8E`, `0x8F`, and the register-write sync word. Four
  diffs, four answers.

### 4.4 Why the bench missed it

The bench runbook had PASSED, and that needs an honest explanation:

1. **The bench exercised the wrong command class.** Its RX-parity check
   "worked" at the register/protocol level; a bench that verifies
   bring-up by reading registers cannot distinguish a live radio from
   a standby chip answering politely. The v0.0.166 rule - bring-up
   must end with a real mode dance (STANDBY -> RX, IRQ flags read in
   RX mode) - exists because of this.
2. **The bench config diverged from the live config.** It ran on
   defaults with token paths unset (the colon-style conf bug meant
   tokens never loaded), so it also never exercised the exact runtime
   path the switchover used.
3. **The environment changed between bench and switchover.** The
   dependency refresh before the flip silently removed unpinned
   libraries (v0.0.154 notes this) and swapped the GPIO stack; the
   bench's passing state was not the state that got deployed.

## 5. The fix chain: v0.0.154 - v0.0.173

Twenty releases in one day, grouped by layer. Each row: the symptom it
killed.

### Environment / packaging (the 07:33-08:08 chain)

| Ver | Fix |
|---|---|
| 0.0.154 | **The bot's config parser never read modem-mode settings** - `radio_mode`, `modem_host`, `modem_port`, `modem_token_file` were documented but silently dropped, so the bot stayed in SPI mode and crash-looped on "GPIO pin already in use" against cleanmodem. Also: `=`-style conf example shipped (the colon-style one is silently skipped line-by-line), and `rpi-lgpio` pinned with its `liblgpio-dev` build note after the dependency refresh silently removed the unpinned library. |
| (box) | `modem.conf` ownership, `gpio`/`spi` group membership (`SupplementaryGroups=`), `liblgpio-dev` for the lgpio wheel, gpiod 2.5.0 in the venv. |

### GPIO backend (the silent-fallback swamp)

| Ver | Fix |
|---|---|
| 0.0.155 | `irq_poll` flag + irq counters in the metrics line - make a deaf receiver *visible* (`polls=` advancing, `edges=0`, `flags=0xAA00`). |
| 0.0.156 | `gpio_backend` config (auto/gpiod/rpi); a forced backend **fails loud** instead of silently falling back - the silent fallback had hidden the real failure for hours. |
| 0.0.157 | The mixed-API bug: `gpiod.Chip` (v2 name) constructed, v1 calls used elsewhere - with 1.5.4 installed the constructor always raised, the factory silently fell back, and **the gpiod backend had never actually run**. |
| 0.0.158 | Full gpiod v2 rewrite (`Chip` + `request_lines` + `LineSettings`); `gpiod>=2.0,<3` pinned (1.x Python over the v2 C library is ABI-broken on Debian 13). |
| 0.0.159 | gpiod 2.x enums live in `gpiod.line`, not top-level - caught by the clean-venv API probe *before* deploy. |

### Modem process lifecycle

| Ver | Fix |
|---|---|
| 0.0.160 | A failed init now tears down its client (the orphan kept retrying, then a second client joined and the two displaced each other from the single controller slot every 2 s); IRQ polls gated on bring-up. |
| 0.0.161 | The modem-mode TX guard required `self.radio`, which is `None` by design in modem mode - every TX dropped with "Radio not up". |

### Radio driver (the deepest layer)

| Ver | Fix |
|---|---|
| 0.0.162 | SetCAD is STANDBY-only on the SX126x; issued during continuous RX it is silently ignored. RX -> STDBY -> CAD -> RX dance. |
| 0.0.163 | **Read-window misalignment:** command responses are `[garbage, status, data...]`; slicing `[1:1+size]` returned the status byte as data and dropped the last data byte. RX_DONE/CAD_DONE/TX_DONE were invisible - `rx=0` since day one. Fixed to `[2:2+size]`. |
| 0.0.164 | SetCadParams takes seven parameter bytes; the driver sent four. Truncated commands are rejected. |
| 0.0.165 | (Detour) read window shifted to byte 3 based on a misdecoded raw capture; reverted in 166. See 6.4. |
| 0.0.166 | SetDIO3AsTcxoCtrl takes four bytes (voltage + 3-byte timeout); the `en` power-enable pin 26 driven HIGH before reset (PiMesh-1W v2 map); read window back to datasheet layout; the watchdog probe now does a real mode dance so "answers SPI but never runs" cannot hide. |
| **0.0.167** | **The opcode table** (section 4) + LoRaRF's TCXO settle constant + a parity test pinning the whole table to the proven values. |

### Protocol / role contract (the consumers)

| Ver | Fix |
|---|---|
| 0.0.168 | Controller keepalive: PING every 15 s so the ~30 s idle recycler stops dropping an idle controller every ~32 s (TX landing in the 2 s reconnect gap failed). |
| 0.0.169 | Observer SET_CONFIG answered with an echo of the **live** config, never applied - openhop_core's driver requires the echo or treats the link as dead and reconnect-loops every 10 s. Chip parameters stay controller-only. |
| 0.0.170 | Observer SET_CAD_PARAMS echoed (the repeater restores cached CAD settings right after its config handshake). |
| 0.0.171 | Authenticated observers exempt from the idle read timeout - openhop_core sends nothing after its handshake, so a live repeater was recycled every ~60 s. Dead observers still reaped by TCP keepalive + slow-client guards. |
| 0.0.172 | config.yaml controls the radio: the bot (controller) pushes the `mcp` radio block via SET_CONFIG at link-up; `tx_power_dbm` becomes the single source of truth and mismatch logs loudly. |
| 0.0.173 | OBSERVER_STATE push (0x72) to the controller on auth and on observer join/leave - the dashboard TCP Push chip reports real liveness instead of a legacy guess. |

Test count went 590 -> 596 across the day; every behavioral fix landed
with a pinning test, including the opcode-parity test that makes this
bug class structurally impossible to reintroduce.

## 6. The probe-driven debugging method

The hunt was not brute force. It followed one discipline all day: **at
every moment, know which layer you are in, and probe that layer with
its own native interface before blaming the one below.** Each probe
answer either cleared a layer or produced a testable claim.

### 6.1 The layer ladder

```
application   bot replies, dashboard chips      <- cleared by the DM test
protocol      modem TCP feed, auth, counters    <- cleared by probe_modem.py
process       systemd, services, ports, logs    <- cleared by journalctl
kernel iface  /dev/gpiochip*, /dev/spidev*      <- cleared by hand probes as the service user
drivers       gpiod / RPi.GPIO shim / spidev    <- cleared by the clean-venv API probe
silicon       SX126x command set                <- cracked by the raw SPI trace + LoRaRF diff
```

The day's two longest detours were both ladder mistakes: hours spent in
the silicon layer while the real problem was one floor down (gpiod ABI,
GPIO groups), and the v0.0.165 read-window detour, which re-entered the
silicon layer on a misread.

### 6.2 The probes, and what each one bought

1. **`probe_modem.py` (protocol probe).** Raw-socket NOISE_REQ +
   STATUS_REQ against the modem port. Result: `noise floor: -105.0 dBm,
   uptime, rx=0 tx=0`. Cleared the entire protocol/process stack in one
   shot and localized deafness to the radio driver. Cheap first probe:
   the top of the ladder is a socket.
2. **IRQ diagnostics in the metrics line (v0.0.155).**
   `irq: polls=2388 edges=0 flags=0xAA00` - three numbers that said:
   the poll loop runs, DIO1 never fires, and the "flags" are the 0xAA
   status byte repeating. Turned silence into data.
3. **Forced backend A/B (v0.0.156).** `gpio_backend: gpiod` vs the
   rpi-lgpio shim, failing loud. This is how the mixed-API bug was
   finally caught - by making the fallback illegal.
4. **Clean-venv API probe.** A one-liner per library
   (`gpiod.Chip`, `request_lines`, `LineSettings`, enum locations) run
   against the exact venv the service uses, *before* deploy. Caught
   the v0.0.159 enum move with zero crash-loop cycles.
5. **Raw SPI per-command trace (v0.0.166).** Issue each init command
   by hand; record `(status, mode, cmd_status)` for every one. The
   EXEC_FAIL split across command classes (section 4.3) was the moment
   the failure stopped being "deaf" and became "clocked commands are
   rejected."
6. **Full-transfer MISO dumps.** `GetIrqStatus MISO=aaaa000003` - raw
   hex of every byte, not driver-decoded values. Caught the
   read-window bug, and later exposed the decode artifact (6.4).
7. **The BUSY-timing probe.** `busy_held=1.0 ms` on every TCXO variant:
   an unknown opcode is refused before any work begins. A real TCXO
   command holds BUSY for its settle time.
8. **The proven-driver diff (v0.0.167).** LoRaRF-Python, vendored on
   the same machine, for the same radio. The single highest-value move
   of the day.

### 6.3 The principles (the reusable part)

- **Distinguish "answers" from "executes."** A chip that answers every
  SPI transaction is not a chip that runs. On this silicon, the
  difference hides in per-command status bits, so status checking is
  not optional - it is the only error channel.
- **One command class can lie for another.** Success on register
  writes proves the SPI bus, nothing more. Bring-up is not done until
  a clocked operation (a mode dance with an IRQ-flags read) succeeds.
  This rule is now enforced by the watchdog probe.
- **No silent fallbacks.** Any backend/config selection that "falls
  back" on failure will one day hide the real bug. Forced selections
  fail loud (v0.0.156); the auto factory logs every candidate it
  rejects and why.
- **Instrument until silence is impossible.** The `irq:` metrics line
  converted a silent failure into three diagnostic numbers that were
  already in every log paste.
- **Raw evidence over decoded interpretation.** Every decode is a
  hypothesis; keep the raw bytes so a wrong decode can be re-run
  against the evidence.
- **Diff against a proven implementation before re-deriving from the
  datasheet.** The datasheet is not wrong, but a table transcribed
  from it by hand is, and the ecosystem already contains a correct
  table for the exact part. The LoRaRF diff took minutes and ended
  the hunt; the datasheet-only path had cost hours.
- **Pin every discovery with a test.** The opcode parity test is the
  permanent form of the day's most expensive lesson: the table can no
  longer drift without a red test.

### 6.4 The detour, honored honestly (v0.0.165)

The raw capture `aaaa000003` was decoded as "flags 00 03 at bytes 3-4
- the radio hears RF." That read drove a one-byte read-window shift
that was wrong, cost about an hour, and was reverted the next version.
The truth: the chip was still in standby answering with its byte-hold
pattern, and `00 03` was an artifact of a chip that never left
standby, not RF evidence. Lesson recorded: **a probe result consistent
with your hypothesis is not confirmation** - and the corroborating
trace should have been run *before* shipping the shift, not after.

## 7. What went well / what went poorly

### Went well

- **The deploy's config-validation gate** refused to restart on the
  broken config (v0.0.151's gate, proven again today).
- **Rollback discipline:** the old modem stayed installed-but-disabled
  until the new stack had proven itself on air; it was never needed,
  and disabling it was the *last* step.
- **Tests before restarts:** 590 -> 596 passing; every behavioral
  claim pinned, so the day's fixes cannot regress silently.
- **The debugging ladder itself:** probes were cheap, layered, and
  mostly one-shot; no step required guesswork about which layer was
  talking.
- **The cross-check reflex:** when first-principles probing stalled,
  looking for a working implementation of the same part in the local
  ecosystem ended the hunt in minutes.

### Went poorly

- **The opcode table was hand-transcribed** from datasheet notes, with
  no test pinning it, and the driver never checked per-command status
  - so a fatal, silent, at-first-boot bug shipped in the bench build.
- **Bring-up success was declared on the wrong evidence** (register
  writes and config commands, i.e. the command class that cannot fail
  from this bug).
- **The bench passed while production was doomed** (section 4.4): the
  bench exercised the wrong command class, a divergent config, and a
  dependency state that the deploy then changed.
- **The venv had drifted** from requirements.txt; the 07:39-08:08
  environment chain was mostly self-inflicted dependency rot.
- **The silent fallback** (v0.0.156/157) converted a loud, trivially
  diagnosable AttributeError into hours of "why is the radio deaf."
- **A misdecoded probe** sent the hunt sideways (6.4).

## 8. Lessons and action items

| # | Lesson | Action | Status |
|---|---|---|---|
| 1 | Opcode/command tables come from proven implementations, never by hand | Parity test vs LoRaRF values | Shipped (0.0.167) |
| 2 | Bring-up = a real clocked exercise, not register writes | Watchdog probe does STANDBY->RX mode dance | Shipped (0.0.166) |
| 3 | Per-command status is the only error channel - check it | Driver checks CmdStatus; EXEC_FAIL surfaced in traces | Shipped (0.0.166) |
| 4 | No silent fallbacks, ever | Forced backends fail loud; auto logs every rejection | Shipped (0.0.156) |
| 5 | A deaf receiver must be visible | irq counters in the per-minute metrics line | Shipped (0.0.155) |
| 6 | Tests pin behavior or it regresses | 596 tests incl. opcode parity, v2 call shape, jitter spread | Ongoing |
| 7 | The venv is part of the deployed artifact | Clean-venv rebuild + API probe before deploys; deps pinned | Shipped (0.0.158/159) |
| 8 | Config authority must be singular | config.yaml pushes radio config to the modem; drift logs loudly | Shipped (0.0.172) |
| 9 | Liveness must be reported, not guessed | OBSERVER_STATE push -> TCP Push chip | Shipped (0.0.173) |

## 9. Open questions

- **Why the bench heard the mesh.** The bench's RX-parity pass with the
  same latent opcode bug is not fully explained in the record. Leading
  candidates: the dependency refresh between bench and switchover
  changed the GPIO/SPI stack (the unpinned-library removal), and the
  bench ran a divergent, token-less config. Unresolved; treat any
  "bench passed" as scoped to the exact code + env the bench ran.
- **The tx_power bookkeeping on the repeater.** The repeater proposes
  18 dBm and now the modem really runs 18 dBm (v0.0.172 push), but the
  repeater's own airtime math still assumes its proposal is authoritative.
  Harmless today; worth revisiting if the repeater ever gets TX rights.
