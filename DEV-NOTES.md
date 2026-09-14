# Development notes

A running summary of what each branch is doing and has done - written as
we go, so anyone can see the state of work without reading commit logs.
Polished summaries graduate into `CHANGELOG.md` (and GitHub release
notes) when a branch merges.

Newest entries first within each branch.

---

## noise-floor (current work)

- **v0.0.144** - Brett: noise-floor graphs vertically REVERSED -
  higher dBm plots LOWER on both the live card and the hourly panel
  (SNR trend unchanged); edge hints on the live card.

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

## feature/branch-switcher (current work - contains the node-popup stack)

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
