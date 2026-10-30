from __future__ import annotations

from wf.phases.base import Phase, PhaseResult, RunContext
from wf.phases.cleanup_worktree import CleanupWorktreePhase
from wf.phases.coding import CodingPhase
from wf.phases.explore import ExplorePhase
from wf.phases.generate_branch_name import GenerateBranchNamePhase
from wf.phases.mr import MrPhase
from wf.phases.notify import NotifyPhase
from wf.phases.prepare import PreparePhase
from wf.phases.review import ReviewPhase

PHASES: dict[str, type[Phase]] = {
    "explore": ExplorePhase,
    "generate_branch_name": GenerateBranchNamePhase,
    "prepare": PreparePhase,
    "coding": CodingPhase,
    "review": ReviewPhase,
    "mr": MrPhase,
    "notify": NotifyPhase,
    "cleanup_worktree": CleanupWorktreePhase,
}

__all__ = ["PHASES", "Phase", "PhaseResult", "RunContext"]
