"""Domain objects for Determined experiments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class ExperimentState(str, Enum):
    """Experiment states as exposed by the Determined API."""

    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPING_CANCELED = "STOPPING_CANCELED"
    STOPPING_KILLED = "STOPPING_KILLED"
    STOPPING_ERROR = "STOPPING_ERROR"
    STOPPING_COMPLETED = "STOPPING_COMPLETED"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    DELETED = "DELETED"
    DELETE_FAILED = "DELETE_FAILED"
    QUEUE = "QUEUE"
    PULLING = "PULLING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass
class Experiment:
    """Representation of a Determined experiment."""

    id: int
    name: str
    state: ExperimentState
    owner: str
    resource_pool: Optional[str] = None
    parent_id: Optional[int] = None
    progress: Optional[float] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    num_trials: Optional[int] = None

    @classmethod
    def from_api_data(cls, data: Dict[str, Any]) -> "Experiment":
        """Create an Experiment from the API response payload."""
        state_raw = str(data.get("state", "")).replace("STATE_", "")
        try:
            state = ExperimentState(state_raw)
        except ValueError:
            state = ExperimentState.UNSPECIFIED

        def _parse_ts(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None

        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            state=state,
            owner=data.get("username", ""),
            resource_pool=data.get("resourcePool"),
            parent_id=data.get("parentId"),
            progress=data.get("progress"),
            started_at=_parse_ts(data.get("startTime")),
            ended_at=_parse_ts(data.get("endTime")),
            description=data.get("description"),
            config=data.get("config"),
            num_trials=data.get("numTrials"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a serialisable version of the experiment."""
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "owner": self.owner,
            "resource_pool": self.resource_pool,
            "parent_id": self.parent_id,
            "progress": self.progress,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "description": self.description,
            "num_trials": self.num_trials,
        }

    def is_failed(self) -> bool:
        return self.state in {ExperimentState.ERROR, ExperimentState.CANCELED, ExperimentState.DELETE_FAILED}

    def is_completed(self) -> bool:
        return self.state is ExperimentState.COMPLETED

    def is_active(self) -> bool:
        return self.state in {
            ExperimentState.ACTIVE,
            ExperimentState.RUNNING,
            ExperimentState.STARTING,
            ExperimentState.PULLING,
            ExperimentState.QUEUE,
        }


__all__ = ["Experiment", "ExperimentState"]
