# Contributing

Thanks for helping with MeshTech-Bot. This project is run by a small
group, so keep things simple and be patient.

## Reporting a bug

Open an issue on GitHub. Say:

1. **What you were doing** — the command you ran or the setting you changed.
2. **What happened** — paste the error text or the bot log lines.
3. **What you expected** — one sentence is fine.
4. **Your setup** — Linux or Docker, and the bot version (shown in the
   dashboard header and in the startup log, e.g. `v0.0.005`).

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
3. **Check your change by hand** — run the bot once with your change
   and confirm your feature answers the way it should (and that
   nothing else broke).
4. **Bump the version** — open `core/version.py` and raise the number by
   one (for example `0.0.005` becomes `0.0.006`). Every commit gets its
   own version number, like a counter: it makes it obvious which changes
   a running bot has. Git tags (`v0.0.004` and so on) are reserved for
   milestone releases and are not needed for ordinary commits.
5. **Open a pull request** describing what changed and why.

## Things to know

- The bot runs on Python 3.10+ with no build step — a change is one or
  more `.py` files, plus the dashboard files in `web/static/` if the UI
  changed.
- Follow the existing style: short functions, plain comments, no new
  dependencies unless they're really needed.
- By contributing, you agree your work is released under the project's
  MIT license (see `LICENSE`).