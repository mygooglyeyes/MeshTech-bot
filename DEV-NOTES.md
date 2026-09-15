# Development notes

A running summary of what each branch is doing and has done - written as
we go, so anyone can see the state of work without reading commit logs.
Polished summaries graduate into `CHANGELOG.md` (and GitHub release
notes) when a branch merges.

Newest entries first within each branch.

---

## DEV (current)

- **v0.0.180** - Noise-floor card restored in modem mode: the monitor
  asks the chip's owner (ModemClient.noise(), NOISE_REQ round-trip)
  when mcp.radio is None - the switchover had orphaned it silently.
  SPI path unchanged; dead link = graph gap. Suite 583.

- **v0.0.179** - Config pipeline hardened (Brett: "whatever it takes so
  the example config.yaml is current in the repo AND on the box, and
  manage.sh config options always see the latest"). Example now ships
  modem mode ACTIVE (mcp block = hilltop's proven layout) + the three
  never-documented keys; modem conf example matches /etc/cleanmodem;
  the sync rule flipped: commented key = MISSING (re-installed active)
  - this was the root cause of hilltop's config staleness; clean
  config runs the sync after copying. Tests pin the example itself.
  Suite 579.

- **v0.0.178** - Merge: feature/mesh-health (the Sept-8 branch,
  v0.0.084-.089) resurrected into DEV - mesh health registry + card,
  `!health`, per-keyword help + grouped DM help, packet export. Merge
  integration: `dm_chunk_gap_seconds` back in the parser and the full
  editor; chunk gaps sleep between chunks only. **Every branch is now
  merged into DEV** (duplicate-packet, noise-floor, small-repairs,
  stability-fixes, feature/spi-radio, clean-modem, mesh-health). The
  cleanmodem chapter also started committing tests/ to the repo - the
  full suite now runs with no exclusions (576 tests).

## small-repairs (merged into DEV)

- **v0.0.148** - Cleanup (Brett's rule, caught in merge review):
  path-hash comment in core/config.py de-referenced + its stale
  "defaults to 1" text fixed to match the real default (2).

- **v0.0.147** - Keyword (Brett): `!repeats` - last-hour repeat count
  and share of all decoded inbound packets, one brief line. Graceful
  "No repeat tracking on this build" answer until the duplicate-packet
  chapter merges into this lineage. 4 tests; suite 503.

- **v0.0.142** - Docs (Brett): docs/REBUILD-RUNBOOK.md - fresh-box
  checklist (backups incl. the radio identity file, the three
  non-git steps: gpio/spi groups, updates.clone_path, webupdates
  sudoers rule; verify checklist). Linked from INSTALL.md.

- **v0.0.141** - Dashboard (Brett): the bot's own public key shows
  under the bot name in the web console header - 10 hex chars on
  screen, copy button copies the FULL key. /api/status carries
  own_pubkey in both radio modes (MCP: from the identity file;
  companion: from the device's SELF_INFO at connect). Hidden until a
  key is known. 4 new tests; suite 499.

- **v0.0.140** - Docker CI (Brett): .github/workflows/docker.yml
  builds + pushes the image to ghcr.io/mygooglyeyes/meshtech-bot on
  every `v*` tag (version / major.minor / latest tags, GITHUB_TOKEN
  auth, gha cache). INSTALL.md Option C mentions the published image
  as the no-build alternative. release.yml untouched.

- **v0.0.139** - Docker fix (Brett's audit): compose config mount no
  longer :ro (web console writes + !trust failed in containers);
  docs/compose header now chown config.yaml to 1001 at setup.

- **v0.0.138** - Brett: !trust replies show "on" for the trust mode
  (user-facing word), never the internal value "trust".

- **v0.0.137** - !trust replies shortened to Brett's dictated wording:
  "Trust set to <mode>" after a change; "Trust is <current>" for a
  bare or invalid !trust. Tests updated to match exactly.

- **v0.0.136** - New admin DM command `!trust on|smart|off` (Brett):
  sets mesh.channel_sender_name via the validated config splicer +
  reload; usage reply shows the current value; admin/DM only; help
  hint updated. Router bug fixed on the way: handler_args dropped
  EVERY occurrence of the command word (now only the first).

- **v0.0.135** - Brett's live-test wording round 3: mcp.enabled says
  "controls (owns) the hardware modem"; coding_rate_index shows all
  four 4/x options as a validated choice; cad_peak/cad_min renamed
  "Radio CAD sensitivity (peak)/(minimum)" with moderate/default
  anchors; path_hash_size caution reworded (higher than the repeaters
  near you = unrelayable); modem_feed.port explains the 5056 default
  (verified against the modem's own code - 5055 is its main port).

- **OpenHop-reference purge (Brett's rule, 2026-09-13, on top of
  v0.0.133, same commit):** no openHop mentions in anything I write -
  editor prompts/help, example path-hash comment, my changelog
  entries, and the example file's product mentions reworded to
  "repeater/companion". A test now enforces it for the full editor.
  (CHANGELOG's historical entries from older branches are left as
  history.)

- **v0.0.133** - BEHAVIOR CHANGE (Brett): 2-byte path hashes are the
  default (loader + example; editor asks the 0/1/2 convention and
  stores bytes). Text keys in the full editor keep their current value
  on Enter instead of offering an empty one. NEW RULE (Brett): no
  openHop references in comments/docs I write - stripped from the
  editor, example path-hash comment, and changelog entries, with a
  test enforcing it in the full editor. Suite 488 pass.

- **v0.0.132** - Two full-editor fixes from Brett's first live run:
  choice keys (logging.level et al) accepted nothing because typed
  answers were lowercased against an uppercase list - matching is now
  case-insensitive with canonical returns; path_hash_size choices now
  carry the openHop mapping (our 1/2/3 bytes = their path.hash.size
  0/1/2) plus migration guidance. 3 new tests; suite 487 pass.

- **v0.0.131 (not yet committed)** - New "Clean config" control-panel
  option (menu 8 now / `sudo ./manage.sh cleanconfig`): backs up,
  double-confirms, wipes the live config.yaml and rebuilds it from the
  latest config.example.yaml so every documented field exists at the
  current version; then offers the config editor and a restart. Also
  fixed: the plain menu header never listed the web-update option.
  Brett's follow-ups built in: (1) the essentials editor walkthrough
  now covers private-channel keys (hex-validated secret_hex per
  channel), warns about the example admin placeholder, asks dashboard
  reachability (127.0.0.1 vs 0.0.0.0), and after saving points at the
  commented-out optional features and set-password.sh; (2) NEW "Edit
  full config" menu option 2 (configurefull): walks EVERY documented
  setting with help text and bounds, commented-out keys show "default
  unused" and are uncommented in place when given a value; a guard
  test pins the field list against core/config.py. Awaiting Brett's
  OK before any commit.
## noise-floor (current work)

- **v0.0.145** - Brett (screenshot): v0.0.144 flipped the WRONG way.
  Now magnitude-oriented: further from zero = lower, closer to zero
  (noise) = higher. Corner dBm numbers removed (canvas hints + axis
  labels); "last 30 minutes" centered.

- **v0.0.144** - Brett: noise-floor graphs vertically REVERSED -
  higher dBm plots LOWER on both the live card and the hourly panel
  (SNR trend unchanged); edge hints on the live card. SUPERSEDED by
  v0.0.145 (wrong direction).

- **v0.0.143** - Live noise-floor card (Brett): graph of the last 30
  minutes in the web console. Records the driver's ALREADY-averaged
  floor (SX1262 quiet-period sampling, peak-rejected, clamped
  -150..-50 dBm) every 5 s into a ring buffer; /api/noisefloor serves
  it; canvas card in the right column, 5 s poll, hides in companion
  mode. PLUS hourly min/avg/max history for day-to-day comparison:
  noise_samples table (migration 11, 14-day retention), a "Noise
  floor (dBm per hour)" panel in the Packet analysis card reusing the
  SNR band chart. 10 new tests; suite 478 on this branch's gate.

## duplicate-packet (current work)

- **v0.0.130 (not yet committed)** - HASH+LONG (Brett's choice after
  reading openhop_repeater-dev's hide-duplicates as reference): repeats
  key on the openhop wire fingerprint (sha256 of payload type + payload
  via the reference Packet.calculate_packet_hash; migration v10 adds
  packets.pkt_hash) with a 5-minute window, so adverts/acks/no-text
  frames mark too and every row of a repeated frame marks. v0.0.129's
  sender+text packet marker retired; message-side marking unchanged.
- **v0.0.129 - Duplicate-packet inspection: relay
  copies of a frame are marked at ingest (identical sender+text within
  Brett's 10 s window; schema migration v9 adds is_repeat/repeat_of to
  packets and messages) and the Messages/Packets cards get per-card
  "hide repeats" switches (hidden by default, remembered, copies tagged
  when shown). Nothing is ever deleted; bot mesh behavior unchanged.
  Awaiting Brett's review, tests, then his OK before any commit.

## feature/mesh-health (merged into DEV)

- **v0.0.088 (in review)** - DM help slimmed 6 -> 3 chunks: per-keyword
  descriptions, shared-description grouping (one admin: line), footer
  only when needed.

- **v0.0.087 (in review)** - Export upgrade for DM-loss analysis:
  messages.csv (full message log) and summary_dms.csv (per-burst chunk
  gaps and sizes) ship with every export, so send-side timing can be
  correlated with which chunks actually arrive on the handheld.
- **v0.0.086 (in review)** - Fix: DM help descriptions leaked the
  weather handler's text onto every command line.

- **v0.0.085 (in review)** - DM delivery fix: chunk gap 0.2 s -> 1.2 s
  (limits.dm_chunk_gap_seconds) after a real-mesh test lost 5 of 6 DM
  chunks to self-collision; DM extended help now a compact list (4
  chunks, not the 6-chunk wide table).

- **v0.0.084 (in review)** - Mesh health chunk: name-only registry for
  talk-only stations (3+ msgs/24 h), flood scoring (burst / share /
  repeat, components visible), Mesh Health card with block checkbox
  (surface-only), admin !health DM command. Design confirmed with the
  user before building; commit pending user review of the diff.

## feature/branch-switcher (merged into DEV)

- **v0.0.082** - Developer mode: a checkbox in the update popup widens
  clickable branches from DEV/main/running to any branch (contributor
  workflow). Off by default; toggled live from the dashboard; stored as
  web.developer_mode in config.yaml.
- **v0.0.081** - Cosmetic: dash + spacing between the Branches title
  and its instruction.
- **v0.0.080** - Fixed the release-notes button: it read the release tag
  under the wrong dictionary key, so "View changes since ..." never
  appeared. Regression test added.

Dashboard-driven updates and node-list analysis, stacked one feature on
the next:

- **v0.0.078** - Fixed the update popup showing a stale version for a
  branch (version file is now fetched pinned to the exact commit, which
  GitHub can never serve stale).
- **v0.0.077** - Popup clarity: every branch row shows its version;
  newer rows say "newer - click it to update"; clickable rows get a
  hover hint; RUNNING box shows the version only.
- **v0.0.076** - Node-list sorting: click a column header (Name, Seen,
  SNR, Route, Msgs); new Msgs column = messages per node last 24 h.
- **v0.0.075** - Web-console updates: click a branch in the update
  popup to switch and update, with live log streaming. One narrowly
  whitelisted sudo script does the privileged part; opt-in via
  `updates: clone_path`.
- **v0.0.073-074** - Node-detail popup: traffic trends (24 h / 7 d /
  30 d) and route history per node; popup always shows the running
  branch even when it is not on the remote.
- **In progress** - The release-notes button shows the running branch's
  changes since the last published release (compare view) instead of
  only pointing at the release page.

## Deferred: one-size-modem dependency split (decision 2026-09-14)

- **The idea (rejected for now):** gate `gpiod` + `rpi-lgpio` behind an
  install extra (`[modem]` / `requirements-modem.txt`) so SPI-mode bots,
  dashboard-only installs, and the Docker image stop pulling radio-only
  GPIO drivers. Prompted by the post-merge hygiene audit of the
  cleanmodem work.
- **Why deferred:** the PiMesh install path has users disable the rpi
  GPIO overlay at the OS level on every board this project targets, so
  the shim-coexistence hazard (rpi-lgpio impersonating RPi.GPIO) cannot
  occur on supported hardware. We are not yet building for generic use.
- **Revisit when:** the project targets "one size modem fits all"
  packaging - multiple radio types, or images/installs for boxes that
  never run cleanmodem. The move is: pull the two pins out of base
  requirements into the extra, teach deploy.sh/manage.sh + INSTALL.md
  the flag, keep the `liblgpio-dev` build note attached to `rpi-lgpio`.
- **Stands regardless:** fresh boxes still need `liblgpio-dev` before
  the lgpio wheel builds (documented in requirements.txt and the
  switchover runbook gotchas).

## Convention

- Every commit bumps the version and adds its own CHANGELOG line.
- Each development branch keeps its running summary here, newest first.
- Merges to main are always published as GitHub releases, with notes
  assembled from the changelog entries the batch contains.
- Branch flow: new feature branches are cut from DEV only, stay
  isolated from other feature branches, and merge back into DEV once
  approved (DEV → feature → DEV). main receives only approved batches
  from DEV.

<!-- Older batches: see CHANGELOG.md - it is the complete record. -->
