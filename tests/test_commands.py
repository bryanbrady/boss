"""The label state machine, driven through the poller against a fake GitHub."""

from boss import labels, watch
from boss.labels import applied_by, command, route, trusted
from boss.run import vet_command


class FakeGh:
    """Issues as label sets, with the labeled-event history GitHub would keep."""

    repo = "owner/r"

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.events: dict[int, list[tuple[str, str]]] = {}
        self.comments: dict[int, list[str]] = {}

    def label(self, number, name, by):
        self.labels.setdefault(number, set()).add(name)
        self.events.setdefault(number, []).append((name, by))

    def issue(self, number):
        return {"number": number, "labels": [{"name": n} for n in sorted(self.labels[number])]}

    def issues_with_any_label(self, names):
        return [self.issue(n) for n, have in self.labels.items() if have & set(names)]

    def edit_labels(self, number, add=(), remove=()):
        for name in add:
            if name not in self.labels[number]:
                self.label(number, name, "the-boss-bot")
        self.labels[number] -= set(remove)

    def apply(self, number, transition):
        self.edit_labels(number, transition.add, transition.remove)

    def identity(self):
        return ("the-boss-bot", "1+the-boss-bot@users.noreply.github.com")

    def label_events(self, number):
        return list(self.events.get(number, []))

    def comment(self, number, body):
        self.comments.setdefault(number, []).append(body)


def quiet(_):
    pass


def test_the_owner_s_command_is_queued_and_its_old_outcome_cleared():
    gh = FakeGh()
    gh.label(1, labels.FAILED, "the-boss-bot")
    gh.label(1, "do:debug", "owner")
    watch.accept_commands(gh, quiet)
    assert gh.labels[1] == {"do:debug", labels.QUEUED}
    assert route(gh.labels[1]).kind == "debug"


def test_accepting_twice_changes_nothing():
    gh = FakeGh()
    gh.label(1, "do:implement", "owner")
    watch.accept_commands(gh, quiet)
    watch.accept_commands(gh, quiet)
    assert gh.labels[1] == {"do:implement", labels.QUEUED}
    assert 1 not in gh.comments


def test_a_command_from_anyone_else_is_dropped_with_a_reason():
    gh = FakeGh()
    gh.label(1, "do:implement", "someone")
    watch.accept_commands(gh, quiet)
    assert gh.labels[1] == set()
    assert "applied by `someone`" in gh.comments[1][0]


def test_the_latest_applier_decides():
    # The owner applied it once, but it was removed and someone else put it back.
    events = [("do:implement", "owner"), ("do:implement", "someone")]
    assert applied_by(events, "do:implement") == "someone"
    assert applied_by(events, "do:test") is None


def test_boss_may_command_itself_for_chains():
    assert trusted("owner/r", "the-boss-bot") == {"owner", "the-boss-bot"}


def test_a_hand_added_queued_label_runs_nothing():
    gh = FakeGh()
    gh.label(1, labels.QUEUED, "someone")
    assert not vet_command(gh, 1, gh.labels[1], quiet)
    assert gh.labels[1] == set()
    assert "no `do:*` command" in gh.comments[1][0]


def test_a_finished_run_drops_its_command_so_adding_it_again_retries():
    gh = FakeGh()
    gh.label(1, "do:implement", "owner")
    watch.accept_commands(gh, quiet)
    gh.apply(1, labels.START)
    assert gh.labels[1] == {"do:implement", labels.RUNNING}  # a restart still knows the role
    gh.apply(1, labels.FINISH_FAILED)
    assert gh.labels[1] == {labels.FAILED}
    watch.accept_commands(gh, quiet)
    assert gh.labels[1] == {labels.FAILED}  # boss:* labels never trigger anything
    gh.label(1, "do:implement", "owner")
    watch.accept_commands(gh, quiet)
    assert gh.labels[1] == {"do:implement", labels.QUEUED}


def test_no_command_means_implement_for_a_manual_run():
    assert command(set()) is None
    assert route(set()).kind == "implement"
