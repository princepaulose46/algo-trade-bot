"""Configuration loader with PyYAML and standard library fallback."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger("config_loader")


def parse_simple_yaml(text: str) -> Dict[str, Any]:
    """Lightweight fallback YAML parser for standard key-value and nested dictionary configs."""
    lines = text.splitlines()
    root: Dict[str, Any] = {}
    stack: list[tuple[int, dict]] = [(-1, root)]

    for raw_line in lines:
        # Strip comments
        line = raw_line.split("#")[0].rstrip()
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip())
        content = line.strip()

        # Pop stack until we find our parent
        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]

        if ":" in content:
            key, val_str = content.split(":", 1)
            key = key.strip()
            val_str = val_str.strip()

            if not val_str:
                # New sub-dictionary
                new_dict: Dict[str, Any] = {}
                parent[key] = new_dict
                stack.append((indent, new_dict))
            else:
                # Value parsing
                parent[key] = _parse_val(val_str)
        elif content.startswith("- "):
            # List item
            val_str = content[2:].strip()
            # If parent has a list
            # For our config, empty lists are '[]' which gets parsed directly.
            pass

    return root


def _parse_val(val_str: str) -> Any:
    if val_str.lower() in ("true", "yes", "on"):
        return True
    if val_str.lower() in ("false", "no", "off"):
        return False
    if val_str.lower() in ("null", "none", "~", ""):
        return None
    if val_str.startswith("[") and val_str.endswith("]"):
        inner = val_str[1:-1].strip()
        if not inner:
            return []
        return [_parse_val(x.strip()) for x in inner.split(",") if x.strip()]
    if (val_str.startswith('"') and val_str.endswith('"')) or (
        val_str.startswith("'") and val_str.endswith("'")
    ):
        return val_str[1:-1]
    try:
        if "." in val_str:
            return float(val_str)
        return int(val_str)
    except ValueError:
        return val_str


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    """Load configuration from YAML file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        import yaml
        return yaml.safe_load(content)
    except ImportError:
        logger.debug("PyYAML not installed; using standard library fallback parser.")
        return parse_simple_yaml(content)

