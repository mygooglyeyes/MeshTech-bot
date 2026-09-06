# Changelog

Every change gets its own version number - the version doubles as a
commit counter, so you can always tell exactly which build a bot is
running (dashboard header chip and startup log). This file records what
each change does, in plain language, newest first. Milestone tags
(`v0.0.004`, `v0.0.006`, `v0.0.012`, `v0.0.016`, ...) mark releases
worth highlighting; ordinary commits just move the counter.

Going forward: every commit that bumps the version adds its line here.

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
