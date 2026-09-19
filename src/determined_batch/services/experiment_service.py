"""Higher level helpers for interacting with Determined experiments."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from determined_batch.core.api_client import DeterminedAPIClient
from determined_batch.domain.experiment import Experiment, ExperimentState


class ExperimentService:
    def __init__(self, api_client: Optional[DeterminedAPIClient] = None) -> None:
        self.api_client = api_client or DeterminedAPIClient()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get_experiments(
        self,
        states: Optional[Sequence[ExperimentState]] = None,
        state_names: Optional[Sequence[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Experiment]:
        api_states = None
        if states:
            api_states = [f"STATE_{s.value}" for s in states]
        elif state_names:
            api_states = [
                f"STATE_{'QUEUED' if name.upper() == 'QUEUE' else name.upper()}"
                for name in state_names
            ]

        experiments_data = self.api_client.get_experiments(limit=limit, offset=offset, states=api_states)
        return [Experiment.from_api_data(exp_data) for exp_data in experiments_data]

    def get_experiment(self, experiment_id: int) -> Experiment:
        response = self.api_client.get_experiment(str(experiment_id))
        data = response.get("experiment") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            from determined_batch.core.api_client import APIError

            raise APIError(
                "Experiment response had no experiment object",
                code="invalid_response",
                details=response,
            )
        return Experiment.from_api_data(data)

    def get_experiment_logs(self, experiment_id: int, tail: int = 100) -> Optional[str]:
        return self.api_client.get_experiment_logs(str(experiment_id), tail=tail)

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------
    def get_failed_experiments(self) -> List[Experiment]:
        return self.get_experiments(states=[ExperimentState.ERROR, ExperimentState.CANCELED, ExperimentState.DELETE_FAILED])

    def get_completed_experiments(self) -> List[Experiment]:
        return self.get_experiments(states=[ExperimentState.COMPLETED])

    def get_active_experiments(self) -> List[Experiment]:
        return self.get_experiments(
            states=[
                ExperimentState.ACTIVE,
                ExperimentState.RUNNING,
                ExperimentState.STARTING,
                ExperimentState.PULLING,
                ExperimentState.QUEUED,
            ]
        )

    def get_old_completed_experiments(self, days: int = 30) -> List[Experiment]:
        cutoff = datetime.now() - timedelta(days=days)
        return [exp for exp in self.get_completed_experiments() if exp.ended_at and exp.ended_at < cutoff]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def delete_experiment(self, experiment_id: int) -> bool:
        self.api_client.delete_experiment(experiment_id)
        return True

    def kill_experiment(self, experiment_id: int) -> bool:
        self.api_client.kill_experiment(experiment_id)
        return True

    def cancel_experiment(self, experiment_id: int) -> bool:
        self.api_client.cancel_experiment(experiment_id)
        return True

    def delete_experiments(self, experiment_ids: List[int], project_id: Optional[int] = None) -> Dict[int, bool]:
        results: Dict[int, bool] = {}
        payload = self.api_client.delete_experiments(experiment_ids, project_id=project_id)
        payload_results = payload.get("results")
        if not isinstance(payload_results, list):
            from determined_batch.core.api_client import APIError

            raise APIError("Delete response had no results array", code="invalid_response")
        for result in payload_results:
            exp_id = result.get("id")
            if exp_id is not None:
                results[int(exp_id)] = result.get("error") is None
        return results


__all__ = ["ExperimentService"]
