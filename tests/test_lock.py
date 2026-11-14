import os
from pathlib import Path

import pytest

from boss import labels, watch
from boss.agents import ConfigError, Settings
from boss.git import HostRepo
from boss.lock import Locked, issue_lock
from boss.run import Outcome


def test_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    with issue_lock(tmp_path, "o/r", 1):
        with pytest.raises(Locked, match=f"pid {os.getpid()}"), issue_lock(tmp_path, "o/r", 1):
            pass
        with issue_lock(tmp_path, "o/r", 2):  # other issues are independent
            pass
    with issue_lock(tmp_path, "o/r", 1):
        pass


class FakeGh:
    repo = "o/r"

    def __init__(self, running=()):
        self.running = list(running)
        self.edits = []

    def issues_with_label(self, label):
        return [{"number": n, "labels": []} for n in self.running]

    def edit_labels(self, number, add=(), remove=()):
        self.edits.append((number, tuple(add), tuple(remove)))

    def apply(self, number, transition):
        self.edit_labels(number, transition.add, transition.remove)

    def identity(self):
        return ("the-boss-bot", "1+the-boss-bot@users.noreply.github.com")

    def label_events(self, number):
        return [("do:implement", "o")]


def test_requeue_skips_issues_a_live_process_holds(tmp_path):
    gh = FakeGh(running=[1, 2])
    with issue_lock(tmp_path, "o/r", 1):
        watch.requeue_stale(Settings(home=tmp_path), gh, log=lambda _: None)
    assert gh.edits == [(2, (labels.QUEUED,), (labels.RUNNING,))]


def test_watch_runs_the_first_issue_it_can_lock(tmp_path, monkeypatch):
    ran = []

    def fake_run(settings, gh, number, queued_only, log):  # the real one takes the lock
        with issue_lock(settings.home, gh.repo, number):
            ran.append(number)
        return Outcome("done", "")

    monkeypatch.setattr(watch, "run_issue", fake_run)
    queue = [{"number": 1}, {"number": 2}, {"number": 3}]
    settings = Settings(home=tmp_path)
    with issue_lock(tmp_path, "o/r", 1):
        assert watch.run_next(settings, FakeGh(), queue, log=lambda _: None)
    assert ran == [2]


def test_watch_reports_nothing_to_run_when_all_are_held(tmp_path, monkeypatch):
    def held(s, gh, n, queued_only, log):
        raise Locked("held")

    monkeypatch.setattr(watch, "run_issue", held)
    assert not watch.run_next(Settings(home=tmp_path), FakeGh(), [{"number": 1}], lambda _: None)


def test_watch_skips_an_issue_the_listing_still_shows_as_queued(tmp_path, monkeypatch):
    class StaleGh(FakeGh):
        comments = []

        def issue(self, number):  # #1 was just failed; only #2 is really still queued
            queued = [{"name": labels.QUEUED}, {"name": "do:implement"}]
            return {"number": number, "labels": queued if number == 2 else []}

        def comment(self, number, body):
            self.comments.append(number)

    def no_token(settings, name):
        raise ConfigError("no token")

    monkeypatch.setattr("boss.run.claude_agent", lambda s: no_token(s, "claude"))
    gh, settings = StaleGh(), Settings(home=tmp_path)
    assert watch.run_next(settings, gh, [{"number": 1}, {"number": 2}], log=lambda _: None)
    assert gh.comments == [2]
    assert not watch.run_next(settings, gh, [{"number": 1}], log=lambda _: None)


@pytest.mark.parametrize(
    "args",
    [
        ("push", "--force", "origin", "x"),
        ("push", "-f", "origin", "x"),
        ("push", "--force-with-lease", "origin", "x"),
        ("push", "--force-with-lease=x:abc", "origin", "x"),
        ("push", "origin", "+HEAD:refs/heads/x"),
        ("push", "--mirror", "origin"),
    ],
)
def test_host_git_refuses_every_kind_of_force_push(args):
    with pytest.raises(ValueError, match="never force-pushes"):
        HostRepo(Path("/nonexistent"), Path("/nonexistent"))(*args)
