"""End-to-end runs in real containers, with a fake GitHub and a scripted agent.

Needs Docker and the boss-agent image: `pytest --docker`.
"""

import copy
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from boss import labels
from boss.agents import REGISTRIES, Agent, Settings
from boss.gh import Gh
from boss.git import HostRepo
from boss.run import run_issue
from boss.sandbox import Sandbox, has_sysbox

pytestmark = pytest.mark.docker


class FakeGh(Gh):
    def __init__(self, origin: Path, issue: dict, cwd: Path):
        super().__init__("o/r", cwd)
        self.origin = origin
        self._issue = issue
        self.comments: list[str] = []
        self.prs: list[dict] = []

    def issue(self, number):
        return copy.deepcopy(self._issue)

    def edit_labels(self, number, add=(), remove=()):
        names = (labels.names(self._issue) - set(remove)) | set(add)
        self._issue["labels"] = [{"name": n} for n in sorted(names)]

    def comment(self, number, body):
        self.comments.append(body)

    def clone(self, work_tree, git_dir):
        subprocess.run(["git", "clone", "--quiet", "--single-branch",
                        f"--separate-git-dir={git_dir}", str(self.origin), str(work_tree)],
                       check=True)  # fmt: skip

    def open_pr(self, branch):
        return None

    def create_pr(self, branch, base, title, body):
        self.prs.append({"branch": branch, "base": base, "title": title, "body": body})
        return "https://example.invalid/pr/1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "boss test")
        monkeypatch.setenv(f"{key}_EMAIL", "boss@example.invalid")
    seed, origin = tmp_path / "seed", tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "--quiet", "-m", "seed"], check=True)
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(origin)], check=True)
    settings = Settings(home=tmp_path / "home", agent_timeout=120, check_timeout=60)
    return tmp_path, origin, settings


def make_issue(body, *names):
    return {"number": 1, "title": "Say hello", "body": body, "comments": [],
            "labels": [{"name": n} for n in (labels.QUEUED, *names)]}  # fmt: skip


def script_agent(script, name="local", **kwargs):
    return Agent(name=name, description="script", command=["sh", "-c", script], **kwargs)


def branch_files(origin, branch):
    out = subprocess.run(["git", "-C", str(origin), "ls-tree", "--name-only", branch],
                         capture_output=True, text=True, check=True)  # fmt: skip
    return out.stdout.split()


def test_success_pushes_a_branch_and_opens_a_pr(world):
    tmp_path, origin, settings = world
    gh = FakeGh(origin, make_issue("## Tests\n- [ ] `test -f hello.txt`\n"), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent("echo hi > hello.txt"))
    assert outcome.status == "done", gh.comments
    assert "hello.txt" in branch_files(origin, "boss/issue-1")
    assert labels.DONE in labels.names(gh._issue)
    assert gh.prs[0]["base"] == "main"
    assert "Closes #1" in gh.prs[0]["body"]


def push_to_origin(tmp_path, origin, branch, name):
    """Someone else pushes a commit to `branch`."""
    other = tmp_path / f"other-{name}"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "checkout", "--quiet", "-B", branch], check=True)
    (other / name).write_text("from a person\n")
    subprocess.run(["git", "-C", str(other), "add", name], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "--quiet", "-m", name], check=True)
    subprocess.run(["git", "-C", str(other), "push", "--quiet", "origin", branch], check=True)
    return rev(origin, branch)


def rev(origin, ref):
    out = subprocess.run(["git", "-C", str(origin), "rev-parse", ref],
                         capture_output=True, text=True, check=True)  # fmt: skip
    return out.stdout.strip()


def test_existing_branch_is_built_on_not_replaced(world):
    tmp_path, origin, settings = world
    human = push_to_origin(tmp_path, origin, "boss/issue-1", "human.txt")
    gh = FakeGh(origin, make_issue(""), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent("echo hi > hello.txt"))
    assert outcome.status == "done", gh.comments
    assert {"human.txt", "hello.txt"} <= set(branch_files(origin, "boss/issue-1"))
    assert rev(origin, "boss/issue-1~1") == human
    prompt = next((tmp_path / "home" / "runs").iterdir()) / "boss" / "prompt.md"
    assert "already has earlier commits" in prompt.read_text()


def test_branch_that_moves_during_the_run_is_not_overwritten(world, monkeypatch):
    tmp_path, origin, settings = world
    give_agent_a_copy, theirs = HostRepo.give_agent_a_copy, []

    def someone_pushes_while_the_agent_works(repo):
        give_agent_a_copy(repo)
        theirs.append(push_to_origin(tmp_path, origin, "boss/issue-1", "theirs.txt"))

    monkeypatch.setattr(HostRepo, "give_agent_a_copy", someone_pushes_while_the_agent_works)
    gh = FakeGh(origin, make_issue("", labels.LOCAL), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent("echo hi > hello.txt"))
    assert outcome.status == "failed"  # not escalated: Claude can't fix a moved branch
    assert "changed on the remote during the run" in gh.comments[-1]
    assert rev(origin, "boss/issue-1") == theirs[0]
    assert labels.FAILED in labels.names(gh._issue)
    assert not gh.prs


def test_failed_check_on_local_escalates_to_claude(world):
    tmp_path, origin, settings = world
    gh = FakeGh(origin, make_issue("## Tests\n- `test -f nope.txt`\n", labels.LOCAL), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent("echo hi > hello.txt"))
    assert outcome.status == "escalated"
    names = labels.names(gh._issue)
    assert {labels.CLAUDE, labels.QUEUED} <= names
    assert labels.LOCAL not in names and labels.RUNNING not in names
    assert "check `test -f nope.txt` exited with code 1" in gh.comments[-1]
    assert subprocess.run(["git", "-C", str(origin), "rev-parse", "--verify", "boss/issue-1"],
                          capture_output=True).returncode != 0  # fmt: skip


def test_claude_failure_is_left_for_a_human(world):
    tmp_path, origin, settings = world
    gh = FakeGh(origin, make_issue("", labels.CLAUDE), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent("true", name="claude"))
    assert outcome.status == "failed"
    assert labels.FAILED in labels.names(gh._issue)
    assert "no changes" in gh.comments[-1]


def test_secrets_are_not_pushed_or_posted(world, monkeypatch):
    tmp_path, origin, settings = world
    monkeypatch.setenv("BOSS_TEST_SECRET", "s3cr3t-value")
    gh = FakeGh(origin, make_issue(""), tmp_path)
    agent = script_agent('echo "$BOSS_TEST_SECRET" | tee leak.txt', name="claude",
                         inherit_env=["BOSS_TEST_SECRET"])  # fmt: skip
    outcome = run_issue(settings, gh, 1, agent=agent)
    assert outcome.status == "failed"
    assert "secret" in outcome.summary
    assert all("s3cr3t-value" not in c for c in gh.comments)


def test_host_git_ignores_hooks_and_config_the_agent_plants(world):
    tmp_path, origin, settings = world
    mark = tmp_path / "pwned"
    script = (
        "printf '#!/bin/sh\\ntouch $MARK\\n' > .git/hooks/pre-commit"
        " && chmod +x .git/hooks/pre-commit"
        ' && git config core.fsmonitor "touch $MARK"'
        " && git config core.hooksPath .git/hooks"
        " && echo hi > hello.txt"
    )
    gh = FakeGh(origin, make_issue(""), tmp_path)
    outcome = run_issue(settings, gh, 1, agent=script_agent(script, env={"MARK": str(mark)}))
    assert outcome.status == "done", gh.comments
    assert not mark.exists()


def test_sandbox_reaches_only_the_allowlist(tmp_path):
    with Sandbox("egress-test", "boss-agent", tmp_path, ["pypi.org"]) as sandbox:

        def curl(url, *flags):
            command = ["curl", "-sS", "-m", "10", "-o", "/dev/null", *flags, url]
            return sandbox.run("curl", command, mounts=[], timeout=60).ok

        assert curl("https://pypi.org/simple/")
        assert not curl("https://example.com/")
        assert not curl("https://example.com/", "--noproxy", "*")
        assert not curl("https://1.1.1.1/", "--noproxy", "*")
    assert "deny  example.com:443" in (tmp_path / "gate.log").read_text()


needs_sysbox = pytest.mark.skipif(not has_sysbox(), reason="needs the Sysbox runtime")

COMPOSE = """services:
  redis:
    image: redis:7-alpine
    healthcheck: {test: ["CMD", "redis-cli", "ping"], interval: 1s, retries: 30}
"""


@needs_sysbox
def test_nested_docker_runs_a_stack_the_checks_can_reach(world):
    tmp_path, origin, settings = world
    settings = replace(settings, nested_docker=True, agent_timeout=300, check_timeout=180)
    body = ("## Tests\n- `docker compose up -d --wait`\n"
            "- `docker compose exec -T redis redis-cli ping | grep PONG`\n")  # fmt: skip
    gh = FakeGh(origin, make_issue(body), tmp_path)
    # The agent proves it has Docker by pulling through the gate and starting the stack.
    script = f"cat > compose.yml <<'EOF'\n{COMPOSE}EOF\ndocker compose up -d --wait"
    outcome = run_issue(settings, gh, 1, agent=script_agent(script))
    assert outcome.status == "done", gh.comments
    assert "compose.yml" in branch_files(origin, "boss/issue-1")


@needs_sysbox
def test_files_from_a_box_come_back_owned_by_the_host_user(world):
    tmp_path, origin, settings = world
    settings = replace(settings, nested_docker=True, agent_timeout=300)
    gh = FakeGh(origin, make_issue(""), tmp_path)
    # Root in a container inside the box writes a setuid binary and deletes a file.
    script = ("docker run --rm -v /work:/w redis:7-alpine sh -c"
              " 'cp /bin/busybox /w/suid && chmod 4755 /w/suid && rm /w/README.md'")  # fmt: skip
    outcome = run_issue(settings, gh, 1, agent=script_agent(script))
    assert outcome.status == "done", gh.comments
    work = next((tmp_path / "home" / "runs").iterdir()) / "work"
    mode = (work / "suid").stat()
    assert mode.st_uid == os.getuid()
    assert not mode.st_mode & stat.S_ISUID
    assert not (work / "README.md").exists()
    assert "README.md" not in branch_files(origin, "boss/issue-1")


@needs_sysbox
def test_builds_in_a_box_install_packages_through_the_gate(world):
    tmp_path, origin, settings = world
    settings = replace(settings, nested_docker=True, agent_timeout=300, check_timeout=180)
    body = "## Tests\n- `docker build -q -t t . && docker run --rm t ls /tmp | grep six`\n"
    gh = FakeGh(origin, make_issue(body), tmp_path)
    dockerfile = "FROM python:3.13-slim\nRUN pip download --no-deps -d /tmp six\n"
    script = f"cat > Dockerfile <<'EOF'\n{dockerfile}EOF"
    outcome = run_issue(settings, gh, 1, agent=script_agent(script, allow=REGISTRIES))
    assert outcome.status == "done", gh.comments
