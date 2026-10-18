"""The labels boss reads and writes, and how they route an issue.

Labels are boss's only state. `do:*` says what kind of work it is, `priority:*` the order,
and `boss:*` where the issue is in boss's queue. Every issue runs on Claude.
"""

from dataclasses import dataclass

QUEUED = "boss:queued"
RUNNING = "boss:running"
DONE = "boss:done"
FAILED = "boss:failed"

KINDS = ("implement", "test", "debug", "review")
PRIORITIES = ("p0", "p1", "p2", "p3")
DEFAULT_PRIORITY = "p2"

# name: (color, description)
ALL = {
    "do:implement": ("1d76db", "boss: implement the issue"),
    "do:test": ("1d76db", "boss: write or fix tests"),
    "do:debug": ("1d76db", "boss: find and fix the root cause"),
    "do:review": ("1d76db", "boss: review the code"),
    QUEUED: ("fbca04", "boss: waiting for the agent"),
    RUNNING: ("f9d0c4", "boss: the agent is working on it"),
    DONE: ("c2e0c6", "boss: finished, see the linked PR"),
    FAILED: ("d93f0b", "boss: the agent could not finish it, needs a human"),
    "priority:p0": ("b60205", "boss: do first"),
    "priority:p1": ("d93f0b", "boss: high priority"),
    "priority:p2": ("fbca04", "boss: normal priority"),
    "priority:p3": ("c5def5", "boss: low priority"),
}


@dataclass(frozen=True)
class Route:
    kind: str  # one of KINDS
    priority: str  # one of PRIORITIES


def names(issue: dict) -> set[str]:
    return {label["name"] for label in issue["labels"]}


def route(labels: set[str]) -> Route:
    kind = next((k for k in KINDS if f"do:{k}" in labels), "implement")
    priority = next((p for p in PRIORITIES if f"priority:{p}" in labels), DEFAULT_PRIORITY)
    return Route(kind, priority)


def queue_order(issues: list[dict]) -> list[dict]:
    """Highest priority first, then oldest issue first."""
    return sorted(issues, key=lambda i: (PRIORITIES.index(route(names(i)).priority), i["number"]))
