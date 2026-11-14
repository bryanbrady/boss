"""One attempt at one issue: clone, run the agent in the sandbox, check, open a PR.

The issue's labels record the outcome. A failed attempt is left for a human.

Each issue has one branch, boss/issue-N. If it's already on GitHub, a run builds on it,
and the push is fast-forward only, so boss never overwrites commits.
"""

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from boss import labels, prompt
from boss.agents import DOCKER_HUB, Agent, Settings, claude_agent
from boss.gh import BOSS_GH_TOKEN, Gh
from boss.git import HostRepo, PushRejected
from boss.labels import Route
from boss.lock import issue_lock
from boss.sandbox import CLAUDE_TOKEN, Mount, Sandbox

LOG_TAIL_LINES = 60
LOG_TAIL_CHARS = 6000
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True)
class Outcome:
    status: str  # "done", "failed" or "skipped"
    summary: str
    pr_url: str | None = None


class Failure(Exception):
    def __init__(self, reason: str, log: Path | None = None):
        super().__init__(reason)
        self.reason = reason
        self.log = log


def run_issue(
    settings: Settings,
    gh: Gh,
    number: int,
    *,
    agent: Agent | None = None,
    queued_only: bool = False,
    log=print,
) -> Outcome:
    """Raises Locked if another boss process on this machine is running the issue.

    With queued_only, an issue that is no longer boss:queued is skipped. Label listings
    lag behind edits by a few seconds, so the poller can be handed an issue it just ran.
    """
    with issue_lock(settings.home, gh.repo, number):
        return _run_issue(settings, gh, number, agent, queued_only, log)


def _run_issue(
    settings: Settings,
    gh: Gh,
    number: int,
    agent: Agent | None,
    queued_only: bool,
    log,
) -> Outcome:
    issue = gh.issue(number)
    if queued_only and labels.QUEUED not in labels.names(issue):
        return Outcome("skipped", f"#{number} is no longer {labels.QUEUED}")
    route = labels.route(labels.names(issue))
    if route.kind == "review":
        raise NotImplementedError("do:review isn't supported yet")
    agent = agent or claude_agent(settings)
    names = [BOSS_GH_TOKEN, *([CLAUDE_TOKEN] if agent.uses_claude_api else [])]
    secrets = [value for name in names if (value := os.environ.get(name))]

    gh.edit_labels(number, add=[labels.RUNNING],
                   remove=[labels.QUEUED, labels.DONE, labels.FAILED])  # fmt: skip
    run_dir = settings.home / "runs" / f"{gh.repo.replace('/', '-')}-{number}-{_stamp()}"
    log(f"#{number}: {route.kind} with {agent.description}, in {run_dir}")
    try:
        pr_url, summary = attempt(settings, gh, issue, route, agent, run_dir, secrets, log)
    except Failure as failure:
        return fail(gh, number, agent, failure, secrets, log)
    except PushRejected as error:
        return fail(gh, number, agent, Failure(str(error)), secrets, log)
    except Exception as error:
        return fail(gh, number, agent, Failure(f"boss error: {error}"), secrets, log)

    gh.edit_labels(number, add=[labels.DONE], remove=[labels.RUNNING])
    gh.comment(number, f"boss ({agent.description}) pushed `boss/issue-{number}`: {pr_url}")
    log(f"#{number}: done, {pr_url}")
    return Outcome("done", summary, pr_url)


def attempt(
    settings: Settings,
    gh: Gh,
    issue: dict,
    route: Route,
    agent: Agent,
    run_dir: Path,
    secrets: list[str],
    log,
) -> tuple[str, str]:
    number = issue["number"]
    git_dir, work, inputs, logs = (run_dir / d for d in ("git", "work", "boss", "logs"))
    for directory in (inputs, logs):
        directory.mkdir(parents=True)

    gh.clone(work, git_dir)
    repo = HostRepo(git_dir, work, author=gh.identity())
    base = repo.branch()
    branch = f"boss/issue-{number}"
    continuing = repo.start_branch(branch)
    start = repo.head()
    repo.give_agent_a_copy()

    commands = prompt.checks(issue["body"])
    text = prompt.build(gh.repo, issue, route, commands, continuing=continuing,
                        docker=settings.nested_docker)  # fmt: skip
    (inputs / "prompt.md").write_text(text)
    for name, content in agent.files.items():
        (inputs / name).write_text(content)

    mounts = [Mount(work, "/work"), Mount(inputs, "/boss", read_only=True)]
    allow = [*agent.allow, *(DOCKER_HUB if settings.nested_docker else [])]
    sandbox = Sandbox(run_dir.name.lower(), settings.image, logs, allow,
                      nested_docker=settings.nested_docker,
                      claude_api=agent.uses_claude_api)  # fmt: skip
    with sandbox:
        log(f"#{number}: agent running (timeout {settings.agent_timeout}s)")
        with sandbox.session("agent", mounts) as session:
            result = session.run(
                "agent",
                agent.command,
                env={**agent.env, **sandbox.agent_env},
                timeout=settings.agent_timeout,
            )
        if result.timed_out:
            raise Failure(f"the agent timed out after {settings.agent_timeout}s", result.log)
        if not result.ok:
            raise Failure(f"the agent exited with code {result.exit_code}", result.log)
        repo.absorb_submodules()  # before any host git reads the work tree; see git.py
        if not repo.changed():
            raise Failure("the agent made no changes", result.log)

        message = f"{issue['title']} (#{number})\n\nboss: {route.kind} by {agent.description}"
        repo.commit_all(message)
        if any(secret in repo.diff(start) for secret in secrets):
            raise Failure("the change contains one of boss's tokens")

        check_lines = []
        with sandbox.session("checks", mounts) as session:
            for i, command in enumerate(commands, 1):
                log(f"#{number}: check {i}/{len(commands)}: {command}")
                check = session.run(f"check-{i}", ["bash", "-lc", command],
                                    timeout=settings.check_timeout)  # fmt: skip
                if not check.ok:
                    why = "timed out" if check.timed_out else f"exited with code {check.exit_code}"
                    raise Failure(f"check `{command}` {why}", check.log)
                check_lines.append(f"- ✅ `{command}`")

    repo.push(branch)
    summary = tail(result.log, secrets)
    checks_text = "\n".join(check_lines) or "- ⚠️ the issue lists no runnable checks"
    body = (
        f"Closes #{number}\n\nboss ran **{route.kind}** with {agent.description}.\n\n"
        f"### Checks\n{checks_text}\n\n"
        f"<details><summary>Agent output (tail)</summary>\n\n```\n{summary}\n```\n</details>\n"
    )
    pr_url = gh.open_pr(branch) or gh.create_pr(branch, base, issue["title"], body)
    return pr_url, summary


def fail(gh: Gh, number: int, agent: Agent, failure: Failure, secrets: list[str], log) -> Outcome:
    body = (f"boss ({agent.description}) failed: {failure.reason}. "
            "Leaving it for a human (`boss:failed`).")  # fmt: skip
    if failure.log and failure.log.exists():
        body += f"\n\n<details><summary>{failure.log.name}</summary>\n\n```\n"
        body += tail(failure.log, secrets) + "\n```\n</details>\n"
    gh.comment(number, body)
    gh.edit_labels(number, add=[labels.FAILED], remove=[labels.RUNNING])
    log(f"#{number}: failed, {failure.reason}. Leaving it for a human.")
    return Outcome("failed", failure.reason)


def tail(path: Path, secrets: list[str]) -> str:
    """The end of a log, fit to post on GitHub. Secrets go before anything is cut, so a
    cut can't leave part of one behind."""
    text = path.read_text(errors="replace")
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    text = ANSI_ESCAPE.sub("", text)
    text = "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
    return text[-LOG_TAIL_CHARS:].replace("```", "'''")


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
