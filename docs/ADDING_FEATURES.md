# Adding features

Three levels, depending on how much you want to do:

0. **Dashboard** — switch on a ready-made **module** (weather, alerts,
   quake) in the web console's Modules card and fill in its settings.
   No code, no restart. Check this first — it may already do what you
   want.
1. **No code** — add a keyword reply in `config.yaml`. Good for simple answers.
2. **Python handler** — a new command like `!joke`. For people
   comfortable writing code, or working with a developer. To build a
   data-pulling add-on like the weather module itself, copy
   `handlers/quake.py` — it shows the module pattern: menu entry,
   dashboard-editable settings, optional channel pushes, and a mesh
   command in one file.

---

## 1. No code: keyword replies

Add an entry to the `replies:` list in `config.yaml`:

```yaml
replies:
  - keywords: ["call", "frequency"]
    text: "Weekly net: Sundays 20:00 local on #net."
```

Whenever one of the keywords appears in a message, the bot answers with
the text. If you list several texts, one is chosen at random. The bot
stays silent on plain words until you add them here.

---

## 2. Python handler: a new command

A **handler** is a small file that defines one feature the bot can do —
everything about it (what it's called, who can use it, what it replies)
lives in that one file. The bot finds new handlers automatically; there
is no other wiring.

### Step 1 — copy the template

```bash
cp handlers/_template.py handlers/joke.py
```

Files starting with an underscore (like `_template.py`) are templates and
are never loaded as real features — only your renamed copy is.

### Step 2 — what a "class" is

Open `handlers/joke.py`. It contains a **class** — a named block that
bundles the feature's settings and behavior together. Think of it as the
feature's file folder: name, trigger words, permissions, and the logic all
live inside it.

### Step 3 — the three names that must match

```python
class ExampleTemplateHandler(Handler):   # <- rename me!
    name = "example"                     # unique id for this handler
    keywords = ["example"]               # what users type: !example
    description = "Short description shown by !help"
    scope = "both"                       # "both" | "channel" | "dm"
    access = "public"                    # "public" | "admin"
```

For a `!joke` command:

- **File name** → `handlers/joke.py` (already done in step 1)
- **Class name** → change `ExampleTemplateHandler` to `JokeHandler`
- **Keywords** → change `keywords = ["example"]` to `keywords = ["joke"]`

With `keywords = ["joke"]`, a message `!joke` on a channel (or in a
DM) triggers the feature.

### Step 4 — fill in the behavior

The `handle()` method is where the feature does its work — the part
between `async def handle(self, ctx):` and `return HandlerResult(...)`.

What you have to work with:

| What | Meaning |
|---|---|
| `ctx.command` | the keyword the user typed |
| `ctx.args` | extra words after the command |
| `ctx.verbosity` | `"brief"` or `"full"` (the `x` modifier) |
| `ctx.store` | the bot's database (nodes, messages, packets…) |
| `ctx.service` | bot state (settings, uptime, connection…) |
| `ctx.sender_display()` | friendly sender name |

A minimal example:

```python
async def handle(self, ctx):
    answer = f"You asked '{ctx.command}' with {len(ctx.args)} extra word(s)."
    return HandlerResult(kind="text", data=answer)
```

Return `None` to stay silent for a particular message.

> **Where does the data come from?** The bot can't guess — the feature
> must fetch or compute its answer. For example, a real weather handler
> would call a weather API (with a key from `config.yaml`) inside
> `handle()`, then format the result into a short reply. Reading the
> existing handlers (`handlers/meshinfo.py`, `handlers/two_byte.py`) is
> the fastest way to learn the patterns — they query the database and
> format replies.

### Step 5 — settings

| Setting | Effect |
|---|---|
| `scope = "both"` | works on channels and in DMs |
| `scope = "channel"` | channels only |
| `scope = "dm"` | DMs only (use for personal/verbose replies) |
| `access = "public"` | anyone can use it |
| `access = "admin"` | only nodes in `dm.admin_pubkey_prefixes` |
| `require_prefix = True` | needs the `!` (e.g. `!weather`) |
| `priority` | lower number answers first if two handlers match |

### Step 6 — save, bump the version, reload

Save the file, then open `core/version.py` and raise the version number
by one (for example `0.0.005` becomes `0.0.006`). Every commit carries
its own version number — see "Proposing a change" in `CONTRIBUTING.md`.

Then restart the bot (`sudo systemctl restart meshtech-bot`) or send
`!reload` from an admin node. The bot picks up the new handler — no
other steps needed. The new number appears in the dashboard header and
the startup log.

### Rules of thumb

- Default to the **compact** reply; use `ctx.verbosity` for the extended
  version when the user appends `x`.
- Keep lines short — long replies are split into `[1/2]` chunks.
- Set `scope = "dm"` when the reply is personal or verbose by nature.