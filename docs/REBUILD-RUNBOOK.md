# Hilltop rebuild runbook — a fresh install without re-learning the traps

Checklist for standing the box up from a wiped SD card / fresh OS.
It exists because past rebuilds lost time to steps that are invisible
in a normal install: the web-update clone path, the web-update sudoers
rule, and the GPIO/SPI access the radio needs.

Everything here is what install.sh + manage.sh actually do — this file
only records the ORDER and the steps git cannot carry across machines
(config, data, the sudoers rule, OS groups). Facts verified against
the code on 2026-09-13 (v0.0.141); fix this file if the code changes.

---

## 0. Before you flash

Collect from the OLD card if it still boots (else from backups):

- [ ] `/opt/meshtech-bot/config.yaml` — THE config (or rebuild via
      `manage.sh` menu 8, which walks you through it; a copy lives in
      chat history / CONVERSATIONS.md notes too)
- [ ] `/opt/meshtech-bot/data/` — bot.db (nodes, messages, stats),
      bot_radio_identity.txt (THE radio identity — losing it changes
      the bot's public key on the whole mesh), .dashboard_password,
      .modem_feed_token (only if modem_feed is enabled)
- [ ] The git remote is public, so the CODE needs no backup
      (github.com/mygooglyeyes/MeshTech-bot)

Without the identity file the bot arrives as a "new" node — never
delete it casually. (The loader refuses to swap identities silently;
that is a feature, not an obstacle.)

## 1. OS + radio prerequisites (fresh OS only)

```bash
sudo raspi-config nonint do_spi 0        # enable SPI
curl -fsSL https://get.docker.com | sh   # optional; only for Docker installs
```

- SPI must be on before the bot starts: the radio is /dev/spidev*.
- Do NOT add i2c/serial overlays — the bot uses SPI + gpiochip only.
- Groups come later (step 3) — install.sh prints the command.

## 2. Code: clone + install (two-location layout)

```bash
git clone https://github.com/mygooglyeyes/MeshTech-bot ~/meshtech-bot
cd ~/meshtech-bot
git checkout DEV                          # or the branch you test on
sudo ./install.sh                         # runtime -> /opt/meshtech-bot
```

install.sh creates the `meshtech` user, venv, unit file, and copies
code to /opt — but NOT your config/data (step 4).

## 3. The three traps (the reason this runbook exists)

a) **GPIO/SPI groups for the service account** — the unit template
   (`deploy/meshtech-bot.service`) already has `SupplementaryGroups=gpio spi`
   and `PrivateDevices=false`. If the radio still gets permission
   errors after a FRESH OS, verify the groups exist and re-run
   install.sh (idempotent) so it re-applies them:

```bash
sudo groups meshtech        # expect: meshtech sudo gpio spi (etc.)
sudo systemctl restart meshtech-bot
```

b) **updates.clone_path — ships COMMENTED OUT** in config.yaml, and
   `deploy.sh`'s config-sync will NEVER add it (by design: it is
   machine-specific). Without it the dashboard's update button can't
   find the clone. Set it to YOUR clone path:

```yaml
updates:
  clone_path: "/home/k6bps/meshtech-bot"
```

c) **The webupdate sudoers rule — not deployable from git.** After
   any fresh install, run once:

```bash
sudo ./manage.sh webupdates     # writes /etc/sudoers.d/meshtech-bot-update
```

That is the ONE rule letting the bot run scripts/update-trigger.sh
via sudo (the dashboard's "switch branch and update" flow). The unit
deliberately does NOT set NoNewPrivileges or ProtectHome — both were
proven on hilltop (2026-09-12) to break exactly this flow. If a
future rebuild "loses" web updates, this rule is step one to check,
clone_path (b) is step two.

## 4. Restore config + data, set ownership

```bash
sudo cp /backup/path/config.yaml /opt/meshtech-bot/config.yaml
sudo cp -a /backup/path/data /opt/meshtech-bot/data
sudo chown -R meshtech:meshtech /opt/meshtech-bot/data /opt/meshtech-bot/config.yaml
```

Then validate with the installer's own checker:

```bash
cd /opt/meshtech-bot && sudo ./manage.sh
```

The panel's config check prints every value it found — eyeball
connection host/port, channels, dm admin prefix, web host/port.

## 5. Start + verify

```bash
sudo systemctl enable --now meshtech-bot
journalctl -u meshtech-bot -n 40        # expect: identity self-check OK
```

Verification checklist (all should pass):

- [ ] journal shows the radio up (MCP: "radio identity self-check"
      with the SAME pubkey as before the rebuild)
- [ ] `sudo ./manage.sh` header shows the version you expect
- [ ] dashboard loads (http://<box>:8081) and its pubkey line matches
      the pre-rebuild key (identity survived)
- [ ] `!help` answered on a configured channel from your handset
- [ ] dashboard "switch branch and update" completes (proves b + c)

## 6. Known-harmless warnings on every update

- `WARNING: found files ... not owned by k6bps (a past run as root?)`
  → the updater self-heals clone ownership; informational.
- `web.host is not loopback` → plaintext-HTTP note; someday TLS item.
- Branch-switch "DOWNGRADE ... type yes" → version counters are
  per-branch, so switching branches always looks like one; confirm.

## 7. Docker variant (only if not native)

Docs/INSTALL.md Option C covers it; the deltas from this runbook:
no GPIO group needed (container maps devices itself), config mounts
writable since v0.0.139, and images publish to
ghcr.io/mygooglyeyes/meshtech-bot on every `v*` tag (CI workflow,
v0.0.140) — `docker pull` instead of building is the fast path.
