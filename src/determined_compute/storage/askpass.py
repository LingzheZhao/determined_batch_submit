"""Minimal SSH_ASKPASS program for storage authentication.

This module intentionally emits only the requested credential on stdout.  All
failure paths use a fixed message and never include backend exceptions.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import sys
from typing import Mapping, Optional, Sequence

from determined_compute.utils.secrets import load_secrets


_MODE = "DETERMINED_COMPUTE_SSH_ASKPASS_MODE"
_SECRETS = "DETERMINED_COMPUTE_SSH_ASKPASS_SECRETS"
_SERVICE = "DETERMINED_COMPUTE_SSH_ASKPASS_SERVICE"
_USERNAME = "DETERMINED_COMPUTE_SSH_ASKPASS_USERNAME"
_STATE = "DETERMINED_COMPUTE_SSH_ASKPASS_STATE"

_REJECTED_PROMPT_PARTS = (
    "authenticity of host",
    "are you sure you want to continue connecting",
    "fingerprint",
    "host key",
    "passphrase",
    "password expired",
    "current password",
    "new password",
    "one-time",
    "verification code",
    "passcode",
    "otp",
)
_PASSWORD_PROMPT = re.compile(r"(?:^|[\s'@])password(?:\s+for\s+[^:]+)?:\s*$", re.I)


class _AskpassFailure(Exception):
    pass


def _is_password_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    if any(part in lowered for part in _REJECTED_PROMPT_PARTS):
        return False
    return bool(_PASSWORD_PROMPT.search(prompt))


def _claim_prompt(state_path: str) -> None:
    if not state_path:
        raise _AskpassFailure
    try:
        descriptor = os.open(state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise _AskpassFailure from exc
    os.close(descriptor)


def resolve_password(prompt: str, environment: Optional[Mapping[str, str]] = None) -> str:
    """Resolve one accepted password prompt from the configured backend."""

    env = os.environ if environment is None else environment
    if not _is_password_prompt(prompt):
        raise _AskpassFailure
    _claim_prompt(env.get(_STATE, ""))

    mode = env.get(_MODE)
    username = env.get(_USERNAME)
    if not username:
        raise _AskpassFailure
    if mode == "password":
        secrets_path = env.get(_SECRETS)
        if not secrets_path:
            raise _AskpassFailure
        secrets = load_secrets(Path(secrets_path))
        if secrets.get("SSH_USERNAME") not in (None, "", username):
            raise _AskpassFailure
        password = secrets.get("SSH_PASSWORD")
    elif mode == "keyring":
        service = env.get(_SERVICE)
        if not service:
            raise _AskpassFailure
        try:
            keyring = importlib.import_module("keyring")
            password = keyring.get_password(service, username)
        except Exception as exc:
            raise _AskpassFailure from exc
    else:
        raise _AskpassFailure

    if not isinstance(password, str) or not password:
        raise _AskpassFailure
    return password


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    prompt = arguments[0] if len(arguments) == 1 else ""
    try:
        password = resolve_password(prompt)
    except Exception:
        print("SSH askpass credential unavailable.", file=sys.stderr)
        return 1
    print(password)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "resolve_password"]
