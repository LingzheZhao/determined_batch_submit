"""Helpers for loading Determined credentials from a simple ``KEY=VALUE`` file.

The default location can be overridden via the ``DETERMINED_BATCH_SECRETS``
environment variable. If it is not set, ``.determined_batch.env`` in the current
working directory is used.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

DEFAULT_SECRET_ENV = "DETERMINED_BATCH_SECRETS"


def default_secrets_path() -> Path:
    """Return the default secrets file path."""
    env_path = os.environ.get(DEFAULT_SECRET_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.cwd() / ".determined_batch.env"


def load_secrets(secrets_path: Optional[Path] = None) -> Dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a secrets file.

    Missing files are ignored so callers can rely on environment variables
    without having to create a secrets file.
    """
    path = Path(secrets_path) if secrets_path else default_secrets_path()
    secrets: Dict[str, str] = {}
    if not path.exists():
        return secrets

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        secrets[key.strip()] = value.strip()
    return secrets


__all__ = ["load_secrets", "default_secrets_path", "DEFAULT_SECRET_ENV"]
