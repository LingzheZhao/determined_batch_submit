"""Shared models and errors for the persistent compute service."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from determined_compute.core.api_client import APIError, SubmissionUncertainError

ComputeError = APIError


class _CodedComputeError(APIError):
    code = "compute_error"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message, code=code or self.code, retryable=False)


class ValidationError(_CodedComputeError):
    code = "invalid_request"


class NotFoundError(_CodedComputeError):
    code = "not_found"


class ConflictError(_CodedComputeError):
    code = "conflict"


@dataclass(frozen=True)
class TaskRecord:
    """Task identity and operator-facing display metadata persisted locally.

    Names and descriptions are stored and must not contain credentials. Full
    request bodies, generated configs, API responses, logs, and exception
    messages are not stored in this record.
    """

    task_id: str
    request_id: str
    owner: str
    payload_hash: str
    profile_hash: str
    kind: str
    state: str
    remote_id: Optional[str]
    remote_state: Optional[str]
    code_revision: Optional[str]
    name: Optional[str]
    description: Optional[str]
    workdir: Optional[str]
    output_dir: Optional[str]
    cluster_identity: Optional[str]
    submission_marker: str
    error_code: Optional[str]
    created_at: str
    updated_at: str
    origin: str = "submitted"
    remote_user_id: Optional[str] = None
    remote_cluster_id: Optional[str] = None

    def public_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        # These are internal consistency/identity values, not part of the public API.
        result.pop("payload_hash", None)
        result.pop("profile_hash", None)
        result.pop("submission_marker", None)
        return result


__all__ = [
    "APIError",
    "ComputeError",
    "ConflictError",
    "NotFoundError",
    "SubmissionUncertainError",
    "TaskRecord",
    "ValidationError",
]
