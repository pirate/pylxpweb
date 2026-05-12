"""Load EG4 credentials from .env or environment.

Usage:
    from _env import USERNAME, PASSWORD, BASE_URL, INVERTER_SN, GRIDBOSS_SN
"""

import os

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

USERNAME    = os.environ.get("EG4_USERNAME")
PASSWORD    = os.environ.get("EG4_PASSWORD")
BASE_URL    = os.environ.get("EG4_BASE_URL", "https://monitor.eg4electronics.com")
INVERTER_SN = os.environ.get("EG4_INVERTER_SN")
GRIDBOSS_SN = os.environ.get("EG4_GRIDBOSS_SN") or None

if not USERNAME or not PASSWORD:
    raise SystemExit(
        "Missing EG4 credentials. Copy .env.example to .env and fill in EG4_USERNAME "
        "and EG4_PASSWORD (or set them as environment variables)."
    )
