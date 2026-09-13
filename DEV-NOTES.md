# Development notes

A running summary of what each branch is doing and has done - written as
we go, so anyone can see the state of work without reading commit logs.
Polished summaries graduate into `CHANGELOG.md` (and GitHub release
notes) when a branch merges.

Newest entries first within each branch.

---

## small-repairs (current work)

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
