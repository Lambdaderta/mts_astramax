"""Environment reading compatible with the organizer's example submission.

The runner may expose an unset template parameter as its own variable name.
Such values are placeholders, so aliases/defaults must remain available.
"""
from __future__ import annotations

import os


def env_value(*names: str, default: str = "") -> str:
    """Return the first stripped, nonempty, non-placeholder environment value."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value and value != name:
            return value
    return default
