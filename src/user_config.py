"""User-level configuration loader for the mirror app.

An .app launched from Finder does NOT inherit the shell environment, so
exporting ``WDA_BUNDLE_ID`` in ``~/.zshrc`` is invisible to a
double-clicked app. This module reads the same values from a stable
config file (or the env var, if set) so both launch paths work.

Config location: ``~/.config/iphone-mirror/config.json``

Recognised keys:
    - ``wda_bundle_id``  (string)  — full runner bundle ID incl. ``.xctrunner``
    - ``tap_y_scale``    (number)  — vertical tap compensation, default 0.95
    - ``tap_x_scale``    (number)  — horizontal tap compensation, default 1.0

The file is auto-created with a template on first launch if missing.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "iphone-mirror"
CONFIG_PATH = CONFIG_DIR / "config.json"

_TEMPLATE = {
    "wda_bundle_id": "com.example.WebDriverAgentRunner.xctrunner",
    "tap_y_scale": 0.95,
    "tap_x_scale": 1.0,
    "_comment": (
        "Set wda_bundle_id to the runner bundle ID you signed in Xcode, "
        "including the '.xctrunner' suffix (e.g. "
        "com.jdoe.WebDriverAgentRunner.xctrunner). See README.md."
    ),
}


def _read_json() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _write_template()
        logger.info("Created default config at %s — please edit before use", CONFIG_PATH)
        return dict(_TEMPLATE)
    except Exception as e:
        logger.warning("Config read failed (%s), using defaults", e)
        return dict(_TEMPLATE)


def _write_template() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(_TEMPLATE, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not create config template: %s", e)


def get(key: str, default=None):
    """Look up ``key`` first in the environment (uppercase form), then in
    the JSON config, then fall back to ``default``.

    Environment overrides let power-users set things temporarily from a
    Terminal launch without touching the config file.
    """
    env_key = key.upper()
    if env_key in os.environ:
        return os.environ[env_key]
    cfg = _read_json()
    return cfg.get(key, default)


def get_wda_bundle_id() -> str:
    return str(get("wda_bundle_id", _TEMPLATE["wda_bundle_id"]))


def get_tap_y_scale() -> float:
    return float(get("tap_y_scale", _TEMPLATE["tap_y_scale"]))


def get_tap_x_scale() -> float:
    return float(get("tap_x_scale", _TEMPLATE["tap_x_scale"]))
