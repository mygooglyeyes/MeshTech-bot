"""Persist console edits back into config.yaml - without reformatting it.

The dashboard never rewrites the whole file (that would shred the user's
comments and ordering). Instead it splices text, the same approach the
interactive config editor uses: find the ``modules:`` section, replace or
insert just the lines for one module, keep everything else byte-identical.
Writes are atomic (tmp file + rename).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def _yaml_scalar(value: Any) -> str:
    """Render a Python value as a YAML scalar the way a human would write it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text.startswith("#") or text.strip() == "" or \
            re.search(r"[:#\"'\[\]{}]", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _render_module_body(name: str, enabled: bool,
                        settings: Dict[str, Any], indent: str = "  ") -> str:
    lines = [f"{indent}{name}:", f"{indent}  enabled: {'true' if enabled else 'false'}"]
    for key, value in settings.items():
        if key == "enabled":
            continue
        if isinstance(value, dict):
            continue            # nested maps are not a console use case
        if isinstance(value, list):
            rendered = ", ".join(_yaml_scalar(v) for v in value)
            lines.append(f"{indent}  {key}: [{rendered}]")
        else:
            lines.append(f"{indent}  {key}: {_yaml_scalar(value)}")
    return "\n".join(lines)


def splice_module(text: str, name: str, enabled: bool,
                  settings: Dict[str, Any]) -> str:
    """Set one module's entry inside the ``modules:`` section of ``text``.

    Creates the section when absent. Everything outside the section is
    untouched; comments inside the section are preserved when the module
    already exists (its block is replaced in place) and kept untouched
    otherwise.
    """
    name = name.strip()
    section = re.search(r"(?m)^modules:[ \t]*(?:#.*)?$", text)
    if section is None:
        # append a fresh section at the end
        block = ("\n\n# Modules - optional add-on features (managed from the "
                 "web console)\nmodules:\n"
                 + _render_module_body(name, enabled, settings) + "\n")
        return text.rstrip("\n") + block

    rest = text[section.end():]
    nxt = re.search(r"(?m)^\S", rest)      # first line of the NEXT section
    body_end = section.end() + (nxt.start() if nxt else len(rest))
    body = text[section.end():body_end]

    # find the module's own block inside the section body: from its
    # '<indent>name:' line up to (not including) the next entry at the SAME
    # indent level - deeper-indented keys belong to this module
    entry = re.search(
        rf"(?m)^(?P<indent>[ \t]+){re.escape(name)}:[ \t]*(?:#.*)?$", body)
    if entry is not None:
        indent = entry.group("indent")
        after = body[entry.end():]
        # next line at the same or shallower indent (deeper keys belong
        # to this module)
        sibling = re.search(rf"(?m)^[ \t]{{0,{len(indent)}}}\S", after)
        block_end = entry.end() + (sibling.start() if sibling else len(after))
        rendered = _render_module_body(name, enabled, settings, indent=indent)
        new_body = body[:entry.start()] + rendered + "\n" + body[block_end:]
    else:
        rendered = _render_module_body(name, enabled, settings)
        new_body = body.rstrip("\n") + "\n" + rendered + "\n"

    return text[:section.end()] + new_body + text[body_end:]


def save_module_settings(config_path: str, name: str, enabled: bool,
                         settings: Dict[str, Any]) -> Dict[str, Any]:
    """Splice + write + re-validate. Returns the fresh settings for the
    module; raises ConfigError (message includes the problems) on failure,
    having restored the original file content."""
    from .config import load as load_config, ConfigError

    path = Path(config_path)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated = splice_module(original, name, enabled, dict(settings or {}))

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        # validate BEFORE replacing the live file: a bad write must never
        # leave the bot unable to reload its config
        load_config(tmp_name)
        os.replace(tmp_name, str(path))
    except ConfigError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # re-read so callers see the parsed truth (and settings are normalized)
    fresh = load_config(str(path))
    return fresh.modules.get(name)
