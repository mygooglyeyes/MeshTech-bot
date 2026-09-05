"""Handler package.

``discover_handlers()`` finds every concrete Handler subclass in this folder
(modules starting with an underscore are treated as documentation/scaffolds
and skipped), so adding a feature = adding one new .py file.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import List, Type

from .base import Handler


def discover_handlers() -> List[Type[Handler]]:
    classes: List[Type[Handler]] = []
    import handlers as package

    for module_info in pkgutil.iter_modules(package.__path__):
        if module_info.name.startswith("_"):
            continue  # _template.py and friends
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, Handler)
                    and attr is not Handler and attr.__module__ == module.__name__):
                classes.append(attr)
    return classes
