"""Argument-safe SSH authentication for shared-storage operations.

The context manager returns environment overrides and OpenSSH options.  Callers
must merge the environment with their own, pass the options as separate argv
items, and wait for the SSH/rsync process before leaving the context.
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple
import unicodedata

from determined_compute.utils.secrets import default_secrets_path, load_secrets


_ASKPASS_MODE = "DETERMINED_COMPUTE_SSH_ASKPASS_MODE"
_ASKPASS_SECRETS = "DETERMINED_COMPUTE_SSH_ASKPASS_SECRETS"
_ASKPASS_SERVICE = "DETERMINED_COMPUTE_SSH_ASKPASS_SERVICE"
_ASKPASS_USERNAME = "DETERMINED_COMPUTE_SSH_ASKPASS_USERNAME"
_ASKPASS_STATE = "DETERMINED_COMPUTE_SSH_ASKPASS_STATE"


class SSHAuthError(ValueError):
    """A sanitized SSH authentication configuration error."""

    code = "ssh_auth_error"
    retryable = False


def _field(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    return getattr(config, name, default)


def _user(config: Any) -> Any:
    value = _field(config, "user")
    return value if value is not None else _field(config, "username")


def _safe_text(value: Any, field_name: str, *, required: bool = False) -> Optional[str]:
    if value is None:
        if required:
            raise SSHAuthError(f"SSH {field_name} is required.")
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise SSHAuthError(f"SSH {field_name} must be text.")
    text = os.fspath(value)
    if not text:
        if required:
            raise SSHAuthError(f"SSH {field_name} is required.")
        return None
    if text.startswith("-"):
        raise SSHAuthError(f"SSH {field_name} cannot start with '-'.")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise SSHAuthError(f"SSH {field_name} contains control characters.")
    return text


def _safe_port(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise SSHAuthError("SSH port must be an integer from 1 to 65535.")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SSHAuthError("SSH port must be an integer from 1 to 65535.") from exc
    if str(value).strip() != str(port) or not 1 <= port <= 65535:
        raise SSHAuthError("SSH port must be an integer from 1 to 65535.")
    return str(port)


def _configured_options(config: Any, user: Optional[str]) -> list[str]:
    # Validate the destination here even though the caller appends it separately.
    _safe_text(_field(config, "host"), "host", required=True)
    port = _safe_port(_field(config, "port"))
    identity_file = _safe_text(_field(config, "identity_file"), "identity_file")
    config_file = _safe_text(_field(config, "config_file"), "config_file")

    options: list[str] = []
    if config_file:
        options.extend(("-F", config_file))
    if user:
        options.extend(("-l", user))
    if port:
        options.extend(("-p", port))
    if identity_file:
        options.extend(("-i", identity_file))
    options.extend(("-o", "StrictHostKeyChecking=yes"))
    return options


def _askpass_launcher(directory: Path) -> Path:
    launcher = directory / "ssh-askpass"
    executable = shlex.quote(sys.executable)
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {executable} -m determined_compute.storage.askpass \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return launcher


def _askpass_environment(
    launcher: Path,
    state_path: Path,
    *,
    mode: str,
    username: str,
    secrets_path: Optional[Path] = None,
    keyring_service: Optional[str] = None,
) -> Dict[str, str]:
    environment = {
        "SSH_ASKPASS": str(launcher),
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": os.environ.get("DISPLAY") or ":0",
        _ASKPASS_MODE: mode,
        _ASKPASS_USERNAME: username,
        _ASKPASS_STATE: str(state_path),
    }
    if secrets_path is not None:
        environment[_ASKPASS_SECRETS] = str(secrets_path)
    if keyring_service is not None:
        environment[_ASKPASS_SERVICE] = keyring_service
    return environment


@contextmanager
def ssh_auth(
    ssh_cfg: Any, secrets_path: Optional[Path] = None
) -> Iterator[Tuple[Dict[str, str], Tuple[str, ...]]]:
    """Yield ``(environment_overrides, ssh_argv_options)`` for one operation.

    ``openssh`` delegates to the user's existing OpenSSH configuration and
    agent.  ``password`` and ``keyring`` use a short-lived executable askpass
    launcher; password material is never placed in argv or the launcher file.
    """

    auth_mode = _safe_text(_field(ssh_cfg, "auth", "openssh"), "auth", required=True)
    assert auth_mode is not None
    auth_mode = auth_mode.lower()
    configured_user = _safe_text(_user(ssh_cfg), "user")

    if auth_mode == "openssh":
        options = _configured_options(ssh_cfg, configured_user)
        options.extend(("-o", "BatchMode=yes"))
        environment: Dict[str, str] = {}
        agent_socket = os.environ.get("SSH_AUTH_SOCK")
        if agent_socket:
            environment["SSH_AUTH_SOCK"] = agent_socket
        yield environment, tuple(options)
        return

    if auth_mode not in ("password", "keyring"):
        raise SSHAuthError("SSH auth must be openssh, password, or keyring.")

    credential_path: Optional[Path] = None
    keyring_service: Optional[str] = None
    if auth_mode == "password":
        credential_path = Path(secrets_path) if secrets_path else default_secrets_path()
        credential_path = credential_path.expanduser().resolve()
        try:
            secrets = load_secrets(credential_path)
        except Exception:
            raise SSHAuthError("SSH password credentials are unavailable.") from None
        secret_user = _safe_text(secrets.get("SSH_USERNAME"), "username")
        password_available = bool(secrets.get("SSH_PASSWORD"))
        del secrets
        if not password_available:
            raise SSHAuthError("SSH password credentials are unavailable.")
        if (
            configured_user is not None
            and secret_user is not None
            and configured_user != secret_user
        ):
            raise SSHAuthError("SSH username does not match the storage profile.")
        username = secret_user or configured_user
        if not username:
            raise SSHAuthError("SSH username is unavailable.")
    else:
        username = configured_user
        keyring_service = _safe_text(
            _field(ssh_cfg, "keyring_service"), "keyring_service", required=True
        )
        if not username:
            raise SSHAuthError("SSH user is required for keyring authentication.")
        try:
            keyring_available = importlib.util.find_spec("keyring") is not None
        except (ImportError, ValueError):
            keyring_available = False
        if not keyring_available:
            raise SSHAuthError("Python keyring support is unavailable.")

    options = _configured_options(ssh_cfg, username)
    options.extend(
        (
            "-o",
            "BatchMode=no",
            "-o",
            "NumberOfPasswordPrompts=1",
            "-o",
            "PreferredAuthentications=keyboard-interactive,password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=yes",
            "-o",
            "PasswordAuthentication=yes",
        )
    )

    with tempfile.TemporaryDirectory(prefix="determined-ssh-auth-") as temp_dir:
        directory = Path(temp_dir)
        try:
            launcher = _askpass_launcher(directory)
        except OSError:
            raise SSHAuthError("SSH askpass helper could not be prepared.") from None
        environment = _askpass_environment(
            launcher,
            directory / "prompt-used",
            mode=auth_mode,
            username=username,
            secrets_path=credential_path,
            keyring_service=keyring_service,
        )
        yield environment, tuple(options)


__all__ = ["SSHAuthError", "ssh_auth"]
