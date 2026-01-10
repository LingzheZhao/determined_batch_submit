"""Higher level helpers for interacting with Determined experiments."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
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
            api_states = [f"STATE_{name.upper()}" for name in state_names]

        experiments_data = self.api_client.get_experiments(limit=limit, offset=offset, states=api_states)
        experiments: List[Experiment] = []
        for exp_data in experiments_data:
            try:
                experiments.append(Experiment.from_api_data(exp_data))
            except Exception:
                continue
        return experiments

    def get_experiment(self, experiment_id: int) -> Optional[Experiment]:
        try:
            data = self.api_client.get_experiment(str(experiment_id))
            return Experiment.from_api_data(data)
        except Exception:
            return None

    def get_experiment_logs(self, experiment_id: int, tail: int = 100) -> Optional[str]:
        try:
            return self.api_client.get_experiment_logs(str(experiment_id), tail=tail)
        except Exception:
            return None

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
                ExperimentState.QUEUE,
            ]
        )

    def get_old_completed_experiments(self, days: int = 30) -> List[Experiment]:
        cutoff = datetime.now() - timedelta(days=days)
        return [exp for exp in self.get_completed_experiments() if exp.ended_at and exp.ended_at < cutoff]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def delete_experiment(self, experiment_id: int) -> bool:
        try:
            self.api_client.delete_experiment(experiment_id)
            return True
        except Exception:
            return False

    def kill_experiment(self, experiment_id: int) -> bool:
        try:
            self.api_client.kill_experiment(experiment_id)
            return True
        except Exception:
            return False

    def cancel_experiment(self, experiment_id: int) -> bool:
        try:
            self.api_client.cancel_experiment(experiment_id)
            return True
        except Exception:
            return False

    def delete_experiments(self, experiment_ids: List[int], project_id: Optional[int] = None) -> Dict[int, bool]:
        results: Dict[int, bool] = {}
        try:
            payload = self.api_client.delete_experiments(experiment_ids, project_id=project_id)
            if "results" in payload:
                for result in payload["results"]:
                    exp_id = result.get("id")
                    if exp_id is None:
                        continue
                    results[int(exp_id)] = result.get("error") is None
                return results
        except Exception:
            pass

        # Fallback to deleting one by one
        for exp_id in experiment_ids:
            results[exp_id] = self.delete_experiment(exp_id)
        return results


__all__ = ["ExperimentService"]
