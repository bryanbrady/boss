"""What an agent is told, and the checks that decide whether it is done."""

import re

from boss.labels import Route

# Comments from people outside the repo never reach an agent's prompt.
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}

TESTS_HEADING = re.compile(r"^#{1,6}\s+tests?\b", re.IGNORECASE)
LIST_ITEM_COMMAND = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?`([^`]+)`")

KIND_INSTRUCTIONS = {
    "implement": "Implement what the issue asks for.",
    "test": (
        "Write or fix the tests this issue describes. Change other code only if a test "
        "exposes a bug."
    ),
    "debug": (
        "Find the root cause of the problem, fix it, and add a test that would have caught "
        "it. Earlier attempts and why they failed may be in the discussion above."
    ),
}


DOCKER_RULE = (
    "Docker and `docker compose` work here, with a daemon of your own. Images come from "
    "Docker Hub. Containers you start reach the internet only over HTTPS to the same "
    "registries, and their published ports are on localhost. The checks run in a fresh "
    "Docker environment, in the order listed, so leave nothing they need running."
)


def checks(body: str) -> list[str]:
    """Commands from the issue's tests section: list items that start with a code span, and
    fenced blocks. They run in a fresh container after the agent finishes."""
    found, fence, in_section, in_fence = [], [], False, False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence and in_section:
                found.append("\n".join(fence))
            fence, in_fence = [], not in_fence
            continue
        if in_fence:
            fence.append(line)
            continue
        if stripped.startswith("#"):
            in_section = bool(TESTS_HEADING.match(stripped))
            continue
        if in_section and (match := LIST_ITEM_COMMAND.match(line)):
            found.append(match.group(1).strip())
    return found


def build(
    repo: str,
    issue: dict,
    route: Route,
    commands: list[str],
    *,
    continuing: bool = False,
    docker: bool = False,
) -> str:
    parts = [
        f"You are working on issue #{issue['number']} in {repo}. The repository is checked "
        "out at /work on a new branch.",
        f"# {issue['title']}\n\n{(issue['body'] or '').strip()}",
    ]
    discussion = [
        f"**@{c['author']['login']}**:\n{c['body'].strip()}"
        for c in issue.get("comments", [])
        if c.get("authorAssociation") in TRUSTED
    ]
    if discussion:
        parts.append("## Discussion\n\n" + "\n\n---\n\n".join(discussion))

    rules = [KIND_INSTRUCTIONS[route.kind], "Keep the change focused on this issue."]
    if continuing:
        rules.insert(
            0,
            "This branch already has earlier commits for this issue. Read them first "
            "(`git log`, `git diff origin/HEAD...HEAD`) and build on them.",
        )
    if commands:
        rules.append(
            "These checks decide whether you are done. Run them and make them pass:\n"
            + "\n".join(f"  - `{c}`" for c in commands)
        )
    rules += [
        "Leave your changes in the working tree. You don't need to commit, and you can't "
        "push: there is no GitHub access here.",
        "The network is limited to package registries, through the proxy in HTTPS_PROXY.",
        *([DOCKER_RULE] if docker else []),
        "Finish with a short summary of what you changed and why.",
    ]
    parts.append("## Instructions\n\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts) + "\n"
