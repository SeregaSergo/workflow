from __future__ import annotations

from wf.phases import PHASES
from wf.state import State
from wf.workflow import RepeatAt, Workflow, terminal


def review_next(s: State) -> str | RepeatAt | None:
    if not s.last_review_has_feedback:
        return "mr"
    if s.iteration >= s.max_iterations:
        return None
    return RepeatAt("coding")


coding = Workflow(
    name="coding",
    phases={
        n: PHASES[n]
        for n in [
            "explore",
            "generate_branch_name",
            "prepare",
            "coding",
            "review",
            "mr",
            "notify",
            "qa_notes",
            "cleanup_worktree",
        ]
    },
    start="explore",
    transitions={
        "explore": lambda _s: "generate_branch_name",
        "generate_branch_name": lambda _s: "prepare",
        "prepare": lambda _s: "coding",
        "coding": lambda _s: "review",
        "review": review_next,
        "mr": lambda _s: "notify",
        "notify": lambda _s: "qa_notes",
        "qa_notes": lambda _s: "cleanup_worktree",
        "cleanup_worktree": terminal,
    },
)
