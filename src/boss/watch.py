"""The poller: plain code that accepts commands, then runs queued issues one at a time, in
priority order."""

import time

from boss import labels
from boss.agents import ConfigError, Settings
from boss.gh import Gh, GhError
from boss.lock import Locked, issue_lock
from boss.run import run_issue, vet_command


def requeue_stale(settings: Settings, gh: Gh, log=print) -> None:
    """An issue marked running that no boss process holds was left by a crash."""
    for issue in gh.issues_with_label(labels.RUNNING):
        try:
            with issue_lock(settings.home, gh.repo, issue["number"]):
                log(f"#{issue['number']}: was left running, queueing it again")
                gh.apply(issue["number"], labels.REQUEUE)
        except Locked:
            pass


def accept_commands(gh: Gh, log=print) -> list[dict]:
    """Queue each issue with a new `do:*` command boss takes, and drop the rest. Returns
    the issues it queued, since the next listing of boss:queued may not show them yet."""
    accepted = []
    for listed in gh.issues_with_any_label(labels.COMMANDS):
        if not labels.waiting_to_be_accepted(labels.names(listed)):
            continue
        number = listed["number"]
        issue = gh.issue(number)  # listings lag behind label edits
        current = labels.names(issue)
        if labels.waiting_to_be_accepted(current) and vet_command(gh, number, current, log):
            log(f"#{number}: accepted {labels.command(current)}")
            gh.apply(number, labels.ACCEPT)
            accepted.append(issue)
    return accepted


def run_next(settings: Settings, gh: Gh, queue: list[dict], log=print) -> bool:
    """Run the first queued issue no other boss process holds. False if there was none."""
    for issue in queue:
        number = issue["number"]
        try:
            outcome = run_issue(settings, gh, number, queued_only=True, log=log)
        except Locked as error:
            log(f"{error}; skipping it")
            continue
        except (ConfigError, NotImplementedError) as error:
            log(f"#{number}: {error}; marking it {labels.FAILED}")
            gh.comment(number, f"boss can't run this: {error}")
            gh.apply(number, labels.FINISH_FAILED)
            return True
        if outcome.status == "skipped":
            continue
        return True
    return False


def watch(settings: Settings, gh: Gh, interval: int, once: bool = False, log=print) -> None:
    requeue_stale(settings, gh, log)
    log(f"watching {gh.repo} for do:* commands every {interval}s")
    while True:
        try:
            accepted = accept_commands(gh, log)
            queued = {i["number"]: i for i in [*accepted, *gh.issues_with_label(labels.QUEUED)]}
            queue = labels.queue_order(list(queued.values()))
            ran = run_next(settings, gh, queue, log)
        except GhError as error:
            log(f"GitHub call failed, will retry: {error}")
            ran = False
        if not ran:
            if once:
                return
            time.sleep(interval)
