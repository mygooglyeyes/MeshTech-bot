# Contributing

Thanks for helping with MeshTech-Bot. This project is run by a small
group, so keep things simple and be patient.

## Reporting a bug

Open an issue on GitHub. Say:

1. **What you were doing** — the command you ran or the setting you changed.
2. **What happened** — paste the error text or the bot log lines.
3. **What you expected** — one sentence is fine.
4. **Your setup** — Linux or Docker, and the bot version (shown in the
   dashboard header and in the startup log, e.g. `v0.0.2`).

Include the config section that matters (a password or key is never
needed — leave those out).

## Asking for a feature

Open an issue and describe what you want the bot to do and why. A short
example of the message you'd type on the radio helps a lot. If a
keyword command is all you need, you can often do it with the `replies:`
section in `config.yaml` — no code required.

## Proposing a change

1. **Fork the repo** and create a branch for your change.
2. **Keep the change small** — one fix or feature per pull request.
3. **Add or update a test** if you changed behavior (tests live in
   `tests/`, run with `python -m pytest`).
4. **Open a pull request** describing what changed and why.

## Things to know

- The bot runs on Python 3.10+ with no build step — a change is one or
  more `.py` files, plus the dashboard files in `web/static/` if the UI
  changed.
- Follow the existing style: short functions, plain comments, no new
  dependencies unless they're really needed.
- By contributing, you agree your work is released under the project's
  MIT license (see `LICENSE`).