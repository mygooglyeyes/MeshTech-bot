# Changelog

Every change gets its own version number - the version doubles as a
commit counter, so you can always tell exactly which build a bot is
running (dashboard header chip and startup log). This file records what
each change does, in plain language, newest first. Milestone tags
(`v0.0.004`, `v0.0.006`, `v0.0.012`, `v0.0.016`, ...) mark releases
worth highlighting; ordinary commits just move the counter.

Going forward: every commit that bumps the version adds its line here.

## 0.0.047 - 2026-09-06

Module cards fixed up after first real use:

- **Quake apply works** - numbers with decimals (min magnitude 2.5) were
  rejected by the console's validation, and the failure was shown nowhere;
  now decimals are accepted and every module card shows save errors right
  next to its Apply button.
- **`!quake` without a zip** uses the quake card's default zip, falling
  back to the weather module's zip when the quake card has none.
- **Quake lines show the time** - each quake carries its local clock time
  (24-hour): `M3.1 ... (475km) - 14:32 - 4h ago`.
- **`#` convention documented** - the modules config section and every
  channel field on the console now say that `novato` and `#novato` are
  the same channel.

## 0.0.046 - 2026-09-06
- Changed: alert and quake pushes can go to several channels - the
  module card's "Push channels" takes a comma-separated list
  (e.g. #novato, #alert), validated against your configured channels.
- Changed: how often the modules look for new alerts/quakes is now a
  card setting ("Check every N minutes"; alerts default 5, quake 10,
  minimum 2). Polling continues as long as push channels are set.
- Backward compatible: an older single-channel 'channel' setting still
  works; weather's daily post gains the same multi-channel support.

## 0.0.045 - 2026-09-06
- New: two more modules on the module menu. **alerts** - active NWS
  weather alerts per zip (!alerts [zip], !alertsx for more) with an
  optional push to a channel when new Severe/Extreme alerts appear
  (deduped, severity floor configurable). **quake** - recent USGS
  earthquakes near a zip (!quake [zip], !quakex) with radius and
  magnitude settings plus optional push of new quakes. Both share the
  weather module's geocoder and were verified against the live APIs.
- Removed: the NIXLE menu placeholder - it offers no open API, so the
  entry could never become real.

## 0.0.044 - 2026-09-06
- Fixed: long message/packet text no longer spills past the card edge.
  The Messages and Packets tables use a fixed layout now - text wraps
  inside the card (long words and hex included) while the meta columns
  keep set widths. The tables scale with the text-size control.

## 0.0.043 - 2026-09-06
- Changed: brief !help opens with "My Commands - " followed by the
  command list (80 bytes, still one packet).

## 0.0.042 - 2026-09-06
- Fixed: brief !help now starts with the commands themselves
  ("!2byte !help ..."), not a "cmds:" label. Some mesh console
  clients (Waev Outpost) render a leading key:value as their own
  stats widget instead of the text - leading with content avoids
  that everywhere.

## 0.0.041 - 2026-09-06
- Condensed: !help (brief) is now a single compact line that always
  fits one LoRa packet - commands, the 'x' hint, plain words, and the
  admin hint joined with |. If space runs out the plain-word list
  shrinks first, then drops; the reply never exceeds one packet.
  Extended help (DM) keeps the full table.

## 0.0.040 - 2026-09-06
- Fixed: reply splitting now measures UTF-8 bytes, not characters. The
  radio carries bytes, so a 133-character reply full of bar glyphs or
  emoji node names could previously exceed the 133-byte on-air limit.
  Cuts still fall between characters, so no glyph is ever split in
  half. Pure-ASCII replies are unchanged. Command-by-command audit:
  every standard command now fits a single packet; only extended ('x')
  replies still split, by design.

## 0.0.039 - 2026-09-06
- Faster (for real this time): message sends no longer wait 3 s for a
  companion ack that never comes - the wait is now 0.2 s, since the
  bytes are already on the wire before the wait begins. The OUT log
  line, the Messages card, and the live feed now show outgoing replies
  the moment they are transmitted. Sends are serialized so multi-part
  replies stay in order; other commands keep the 3 s budget.

## 0.0.038 - 2026-09-06
- Faster: !2byte now reuses its answer for 5 minutes instead of querying
  the database on every ask - node stats barely change, so repeated
  questions cost almost nothing. (The "no data yet" notice is never
  cached, so early asks stay accurate while capture builds.)

## 0.0.037 - 2026-09-06
- Moved: the Modules card now sits on the right side, directly under
  Channels & Controls, so module settings live with the other controls.

## 0.0.036 - 2026-09-06
- Removed: the dead "refresh" button in Channels & Controls. It had no
  handler - status already auto-refreshes every few seconds, so the
  button only ever confused.

## 0.0.035 - 2026-09-06
- Fixed: slow keyword replies. The mesh library waits up to 15 s for a
  companion ack that openHop never sends - every reply parked the full
  timeout and, because inbound events are processed one at a time, held
  up later messages too. The wait is now 3 s, and message sends that
  get no ack count as delivered (the radio transmission itself was
  never delayed). Bot replies also appear in the Messages card again
  (they were being discarded after the timeout).
- Changed: command handlers run off the inbound processing chain with
  bounded concurrency (2), so a slow lookup (weather) no longer delays
  other mesh messages; reply pacing is enforced under a lock so rapid
  commands can't double-reply.
- Changed: database writes no longer fsync on every message on the Pi's
  SD card (SQLite synchronous=NORMAL with WAL) - same crash safety for
  our use, much less write latency and card wear.
- Added: uvloop event loop on Linux for lower CPU on the dashboard and
  mesh I/O (optional dependency; standard loop used if absent).
- Added: bot.sync_device_time config (default off) - the startup
  clock-sync command is skipped unless a companion actually needs it,
  which also removes the harmless 'set device time' warning.

## 0.0.034 - 2026-09-06
- Added: logout button in the dashboard header (left of the connection
  chip) - revokes this browser's session and returns to the login
  screen; the bot itself is untouched.
- Added: the openHop companion's name now shows next to MeshTech-Bot in
  the title bar ("MeshTech-Bot · LoganBot🤖").
- Added: text-size − / + buttons under the main bar; the choice is
  remembered per browser and rescales the whole console.
- Changed: the dashboard's two columns are now independent - opening or
  collapsing a card on one side no longer moves or resizes cards on the
  other side.

## 0.0.033 - 2026-09-06
- Fixed: station observations were silently unreachable (wrong API
  endpoint), so `!wx` always fell back to the forecast-derived line. The
  station list now comes from the gridpoint's observationStations link,
  the correct identifier field is read, and the fix was verified against
  the live API before release - `!wx 94945` now reports
  "(obs KDVO): 55F wind 5mph Clear".

## 0.0.032 - 2026-09-06
- Changed: `!wx` now reports measured conditions when possible. The bot
  reads the nearest reporting weather station's latest observation (NWS,
  still free, no key), converts it to F/mph, and marks the reply
  `(obs KLGU)`; readings older than 90 minutes are skipped (next-nearest
  station tried, then the forecast block). The daily forecast post is
  unchanged - it remains a forecast.

## 0.0.031 - 2026-09-06
- Added: automatic cache-busting for the dashboard. The page loads its
  app.js and style.css with the running version in the URL, so every
  deploy is picked up by browsers on the next normal refresh - no more
  Ctrl+F5 after updating the bot.

## 0.0.030 - 2026-09-06
- Changed: the weather "Forecast time" setting is now a 12-hour picker -
  an hour box, a minute box (both with sample text), and a stacked AM/PM
  toggle - so "6 am" no longer has to be typed as "06:00". It saves as
  24-hour HH:MM in config.yaml either way.

## 0.0.029 - 2026-09-06
- Changed: Weather gets two feeds. `!wx [zip]` (alias of `!weather`)
  answers with current conditions on demand; the scheduled push is now a
  **daily forecast post** at a time you choose (`post_time`, e.g.
  "07:00") instead of change-based checks. Set the channel and time in
  the Modules card.
- Changed: the pulse scheduler now lets modules pick their next check
  each cycle, so wall-clock schedules never drift.
- Fixed: the add-features tutorial no longer tells users to create
  `handlers/weather.py`, which is now a real module (example renamed to
  `!joke`).

## 0.0.028 - 2026-09-06
- Changed: Modules card polish from first box testing. Each module is
  now a collapsed row - click the module name to open its settings
  editor (the toggle button stays visible on the row). Applying the
  settings also switches the module on, so one save does the whole job.
- Changed: the Weather module's reply is labelled "now" and forecast
  filler words (Sunny, Mostly Clear, Chance...) are stripped, because
  the NWS feed carries forecasts rather than live observations.

## 0.0.027 - 2026-09-06
- Added: the modules menu (on the feature/modules branch, not yet in
  DEV). A menu of optional add-on features, managed from a new Modules
  card in the web console: enable a module, edit its settings (zip
  code, push channel, poll interval), save - no restart. First module:
  Weather (`!weather [zip]` via the National Weather Service; `x` adds
  a 3-day outlook; optional scheduled push of condition changes to a
  channel, off until you enable it). NIXLE appears grayed out as a
  roadmap entry. New dependency: aiohttp.

## 0.0.026 - 2026-09-06
- Docs: companion creation order is now a stated requirement - create
  the everyday-use companion FIRST and the bot's companion second, and
  the repeater web console will leave the bot's companion free (it
  attaches to the first one). The troubleshooting table also gained a
  row for the connect-loop symptom this causes.

## 0.0.025 - 2026-09-06
- Changed: the startup clock-sync is no longer logged as a warning when
  the companion declines it (error 6, illegal argument). Firmware that
  already keeps time itself - like openHop on a Linux repeater - does
  this on purpose; it is harmless, and the log now says so quietly.

## 0.0.024 - 2026-09-06
- Added: a friendly log hint when the companion link flaps. If the
  connection is accepted then dropped repeatedly within seconds, the
  bot now says the likely cause once - another client holding the
  companion (e.g. the repeater's web console auto-connects to it at
  startup) - instead of leaving a wall of reconnect lines to decode.

## 0.0.023 - 2026-09-06
- Docs: branch policy and release rhythm spelled out in the
  contributing guide — small work commits straight to DEV, branches
  only for big/risky/experimental work, and `main` moves when a tested
  batch is ready (or an important fix can't wait), tagged as a
  milestone.

## 0.0.022 - 2026-09-06
- Added: this changelog. Full history backfilled below; from now on
  every version bump records its fix here.
- Docs: the contributing guide now says a change = version bump + one
  changelog line.

## 0.0.021 - 2026-09-06
- Changed: `packets.jsonl` now rotates to `packets.jsonl.old` when it
  passes a size cap (default 64 MB, `storage.packet_jsonl_max_bytes`),
  so the analysis file can never grow forever. (The database was
  already row-capped; this was the one unbounded file.)

## 0.0.020 - 2026-09-06
- Docs: the contributing guide documents the branch model - development
  happens on `DEV`, `main` is the stable branch users install from.

## 0.0.019 - 2026-09-06
- Changed: the test suite and dev-only requirements were removed from
  the repository (kept on each developer's machine, recoverable from
  git history). A fresh clone now contains only end-user files.

## 0.0.018 - 2026-09-06
- Fixed: the bot no longer captures companion bookkeeping frames
  (`NEXT_CONTACT` contact-list dumps, `CONTACT`, `NO_MORE_MSGS`,
  `CURRENT_TIME`) - they made up ~88% of stored packets while carrying
  no mesh traffic, evicting real history from the packet database.
- Added: `scripts/purge_frames.py` - one command to back up the
  database and delete bookkeeping rows recorded by older versions
  (`--dry-run` supported).

## 0.0.017 - 2026-09-05
- Fixed: the dashboard config view shows the bot's effective name and
  where it came from (companion / config fallback), instead of the
  misleading raw `display_name` key.

## 0.0.016 - 2026-09-05
- Fixed: deploys accept both spellings of the version stamp inside the
  update package, so an update started on an older version can't be
  refused mid-transition (and can't deploy half-blind either).

## 0.0.015 - 2026-09-05
- Fixed: deploys bake a fresh version stamp correctly - the dashboard
  version chip had been frozen at an old build - and abort loudly if a
  package arrives without one.

## 0.0.014 - 2026-09-05
- Changed: the documented update command is `sudo ./manage.sh update`,
  run from your home clone - and it prefers the clone's own freshest
  update script.

## 0.0.013 - 2026-09-05
- Changed: `sudo ./manage.sh update` works from anywhere - it finds
  (or creates) your home clone automatically. Docs now lead with the
  single command instead of deploy.sh.

## 0.0.012 - 2026-09-05
- Fixed: update validation runs from the runtime directory. The old
  false warnings ("dashboard has NO password", throwaway database
  checks) during deploys are gone.

## 0.0.011 - 2026-09-05
- Added: `./deploy.sh --dry-run` previews exactly which files would
  change in /opt before anything is applied. No sudo, no mutation.

## 0.0.010 - 2026-09-05
- Fixed: `manage.sh` always manages the /opt runtime, never your
  clone - safe to start the panel from the home clone or elsewhere.

## 0.0.009 - 2026-09-05
- Changed: two-location layout adopted. Git happens in your home clone
  (`~/meshtech-bot`); `deploy.sh` applies updates to /opt. Your config,
  data, and .venv are never touched by git. Validation gates a bad
  update before the restart; old code keeps running on failure.
- Fixed: deploy.sh executable bit.

## 0.0.006 - 0.0.008 - 2026-09-05
- Added: the installer offers to add your user to the `meshtech` group
  so you can browse /opt/meshtech-bot without sudo.
- Docs: the every-commit version bump convention documented in the
  contributor guide and update docs.

## 0.0.005 - 2026-09-05
- Docs: version-bump-per-commit convention recorded (the version
  number doubles as a commit counter).

## 0.0.004 (milestone tag) - 2026-09-05
- Added: `manage.sh` - an SSH-friendly ASCII control panel: configure
  the bot interactively (connection, channels, keyword replies,
  admins), update, uninstall, restart.

## 0.0.003 - 2026-09-05
- Changed: version format switched to zero-padded `0.0.0NN` so the
  counter sorts and reads cleanly.

## 0.0.001 - 0.0.002 (first-day builds) - 2026-09-04/05
- Added: `!path` / `!pathx` relay-chain reports mined from RF logs;
  `!dm` command; runtime raw-capture toggle with CSV download;
  per-node notes in the node table; About dialog (license, notices,
  commit link); `set-password.sh` helper; per-channel reply cadence;
  MIT license and third-party notices; end-to-end verification
  checklist for fresh installs.
- Fixed: dashboard uptime chip polling after login; live feed
  re-pasting history on WebSocket reconnect; `!status` moved to
  DM-only; connect crash on `self_info`; executable bits for
  bot.py/set-password.sh.
- Security: login throttle, WebSocket first-frame token handshake,
  Content-Security-Policy, CSV-injection-safe exports, dashboard
  password moved out of config.yaml into a root-only file, systemd
  service hardening.
- Docs: plain-language rewrite of README and install guide; config
  view shown as a flat settings list; fixed two-column dashboard
  layout.
