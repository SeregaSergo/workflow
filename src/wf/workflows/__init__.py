from __future__ import annotations

from wf.workflow import Workflow
from wf.workflows.coding import coding

WORKFLOWS: dict[str, Workflow] = {
    coding.name: coding,
}

__all__ = ["WORKFLOWS"]
