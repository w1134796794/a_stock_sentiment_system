"""Normalize sector names at JSON/CSV boundaries without substring matching."""
from __future__ import annotations

import ast
import json
import re
from typing import Any


def normalize_sector_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = value
    elif isinstance(value, str):
        text = value.strip()
        if text.lower() in {'', 'nan', 'none', 'null', '<na>'}:
            return []
        if text.startswith(('[', '(')):
            try:
                values = json.loads(text)
            except (ValueError, TypeError):
                try:
                    values = ast.literal_eval(text)
                except (ValueError, SyntaxError):
                    return []
            if not isinstance(values, (list, tuple)):
                return []
        else:
            values = re.split(r'[,，;；]', text)
    else:
        return []
    return list(dict.fromkeys(item.strip() for item in values
                              if isinstance(item, str) and item.strip()))
