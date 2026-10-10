"""The poller: plain code that takes queued issues one at a time, in priority order."""

import time

from boss import labels
from boss.agents import ConfigError, Settings
from boss.gh import Gh, GhError
from boss.lock import Locked, issue_lock
from boss.run import run_issue


def requeue_stale(settings: Settings, gh: Gh, log=print) -> None:
    """An issue marked running that no boss process holds was left by a crash."""
    for issue in gh.issues_with_label(labels.RUNNING):
        try:
            with issue_lock(settings.home, gh.repo, issue["number"]):
                log(f"#{issue['number']}: was left running, queueing it again")
                gh.edit_labels(issue["number"], add=[labels.QUEUED], remove=[labels.RUNNING])
        except Locked:
            pass


def run_next(settings: Settings, gh: Gh, queue: list[dict], log=print) -> bool:
    """Run the first queued issue no other boss process holds. False if there was none."""
    for issue in queue:
        number = issue["number"]
        try:
            run_issue(settings, gh, number, log=log)
        except Locked as error:
            log(f"{error}; skipping it")
            continue
        except (ConfigError, NotImplementedError) as error:
            log(f"#{number}: {error}; marking it {labels.FAILED}")
            gh.comment(number, f"boss can't run this: {error}")
            gh.edit_labels(number, add=[labels.FAILED], remove=[labels.QUEUED, labels.RUNNING])
        return True
    return False


def watch(settings: Settings, gh: Gh, interval: int, once: bool = False, log=print) -> None:
    requeue_stale(settings, gh, log)
    log(f"watching {gh.repo} for {labels.QUEUED} every {interval}s")
    while True:
        try:
            queue = labels.queue_order(gh.issues_with_label(labels.QUEUED))
            ran = run_next(settings, gh, queue, log)
        except GhError as error:
            log(f"GitHub call failed, will retry: {error}")
            ran = False
        if not ran:
            if once:
                return
            time.sleep(interval)
