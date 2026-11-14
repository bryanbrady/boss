"""The labels boss reads and writes, and every change it makes to them.

Labels are boss's only state. A `do:*` label is a command: put one on an issue to have boss
do that kind of work. boss accepts a command only from the repo's owner or from itself
(see `trusted`), by setting `boss:queued`. The `do:*` label stays on while the work is
queued and running, so a restart still knows the role. When the run ends, boss removes it
and sets the outcome. Adding a `do:*` label again is how to retry, or give an issue more
work; accepting it clears the old outcome.

`boss:*` labels are boss's own record and never trigger anything. Each transition below is
a set of labels to add and a set to remove, so applying one twice changes nothing: label
edits aren't atomic, and a step may be retried.
"""

from dataclasses import dataclass

DRAFT = "boss:draft"
QUEUED = "boss:queued"
RUNNING = "boss:running"
DONE = "boss:done"
FAILED = "boss:failed"
BLOCKED = "boss:blocked"

KINDS = ("implement", "test", "debug", "review")
COMMANDS = tuple(f"do:{kind}" for kind in KINDS)
PRIORITIES = ("p0", "p1", "p2", "p3")
DEFAULT_PRIORITY = "p2"

# name: (color, description)
ALL = {
    "do:implement": ("1d76db", "boss: implement the issue"),
    "do:test": ("1d76db", "boss: write or fix tests"),
    "do:debug": ("1d76db", "boss: find and fix the root cause"),
    "do:review": ("1d76db", "boss: review the code"),
    DRAFT: ("ededed", "boss: written, not approved to run"),
    QUEUED: ("fbca04", "boss: accepted, waiting for the agent"),
    RUNNING: ("f9d0c4", "boss: the agent is working on it"),
    DONE: ("c2e0c6", "boss: finished, see the linked PR"),
    FAILED: ("d93f0b", "boss: the agent could not finish it, needs a human"),
    BLOCKED: ("b60205", "boss: a limit or conflict needs a human"),
    "priority:p0": ("b60205", "boss: do first"),
    "priority:p1": ("d93f0b", "boss: high priority"),
    "priority:p2": ("fbca04", "boss: normal priority"),
    "priority:p3": ("c5def5", "boss: low priority"),
}

OUTCOMES = (DONE, FAILED, BLOCKED)


@dataclass(frozen=True)
class Route:
    kind: str  # one of KINDS
    priority: str  # one of PRIORITIES


@dataclass(frozen=True)
class Transition:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()


ACCEPT = Transition(add=(QUEUED,), remove=(DRAFT, *OUTCOMES))
START = Transition(add=(RUNNING,), remove=(QUEUED, *OUTCOMES))
REQUEUE = Transition(add=(QUEUED,), remove=(RUNNING,))  # a run that a crash left behind
FINISH_DONE = Transition(add=(DONE,), remove=(QUEUED, RUNNING, *COMMANDS))
FINISH_FAILED = Transition(add=(FAILED,), remove=(QUEUED, RUNNING, *COMMANDS))
DROP_COMMAND = Transition(remove=(QUEUED, *COMMANDS))  # a command boss won't take


def names(issue: dict) -> set[str]:
    return {label["name"] for label in issue["labels"]}


def command(labels: set[str]) -> str | None:
    """The `do:*` label on the issue, if any."""
    return next((c for c in COMMANDS if c in labels), None)


def waiting_to_be_accepted(labels: set[str]) -> bool:
    return command(labels) is not None and not labels & {QUEUED, RUNNING}


def route(labels: set[str]) -> Route:
    found = command(labels)
    kind = found.removeprefix("do:") if found else "implement"
    priority = next((p for p in PRIORITIES if f"priority:{p}" in labels), DEFAULT_PRIORITY)
    return Route(kind, priority)


def queue_order(issues: list[dict]) -> list[dict]:
    """Highest priority first, then oldest issue first."""
    return sorted(issues, key=lambda i: (PRIORITIES.index(route(names(i)).priority), i["number"]))


def trusted(repo: str, boss_login: str) -> frozenset[str]:
    """Who may give boss commands: the repo's owner, and boss itself (for chains)."""
    return frozenset({repo.split("/")[0], boss_login})


def applied_by(events: list[tuple[str, str]], label: str) -> str | None:
    """Who put `label` on most recently, from (label, login) labeled events, oldest first."""
    return next((login for name, login in reversed(events) if name == label), None)
