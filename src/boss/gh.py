"""GitHub access through the gh CLI.

Only the host process calls this. Containers never hold a GitHub token. gh runs from a
directory boss controls, never from a run's work tree, whose git config the agent could
have changed.
"""

import json
import os
import subprocess
from pathlib import Path

ISSUE_FIELDS = "number,title,body,labels,comments,state,url"

# gh colors its output, JSON included, when these force it, even into a pipe.
FORCE_COLOR_VARS = ("CLICOLOR_FORCE", "GH_FORCE_TTY", "FORCE_COLOR")


class GhError(RuntimeError):
    pass


class Gh:
    def __init__(self, repo: str, cwd: Path):
        self.repo = repo
        self.cwd = cwd

    def __call__(self, *args: str, stdin: str | None = None) -> str:
        result = subprocess.run(
            ["gh", *args],
            cwd=self.cwd,
            input=stdin,
            capture_output=True,
            text=True,
            env=gh_env(),
        )
        if result.returncode != 0:
            raise GhError(f"gh {' '.join(args[:3])}: {result.stderr.strip()}")
        return result.stdout

    def json(self, *args: str):
        return json.loads(self(*args))

    def issue(self, number: int) -> dict:
        return self.json("issue", "view", str(number), "--repo", self.repo, "--json", ISSUE_FIELDS)

    def issues_with_label(self, label: str) -> list[dict]:
        return self.json(
            "issue", "list", "--repo", self.repo, "--state", "open", "--label", label,
            "--limit", "200", "--json", "number,title,labels,createdAt",
        )  # fmt: skip

    def edit_labels(self, number: int, add=(), remove=()) -> None:
        current = {label["name"] for label in self.issue(number)["labels"]}
        args = ["issue", "edit", str(number), "--repo", self.repo]
        for label in add:
            if label not in current:
                args += ["--add-label", label]
        for label in remove:
            if label in current:
                args += ["--remove-label", label]
        if len(args) > 5:
            self(*args)

    def comment(self, number: int, body: str) -> None:
        self("issue", "comment", str(number), "--repo", self.repo, "--body-file", "-", stdin=body)

    def clone(self, work_tree: Path, git_dir: Path) -> None:
        """The default branch only. An agent sees no one's other branches."""
        self("repo", "clone", self.repo, str(work_tree), "--",
             f"--separate-git-dir={git_dir}", "--single-branch", "--quiet")  # fmt: skip

    def open_pr(self, branch: str) -> str | None:
        prs = self.json(
            "pr", "list", "--repo", self.repo, "--head", branch, "--state", "open",
            "--json", "url",
        )  # fmt: skip
        return prs[0]["url"] if prs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        out = self(
            "pr", "create", "--repo", self.repo, "--head", branch, "--base", base,
            "--title", title, "--body-file", "-", stdin=body,
        )  # fmt: skip
        return out.strip().splitlines()[-1]

    def create_label(self, name: str, color: str, description: str) -> None:
        self("label", "create", name, "--repo", self.repo, "--color", color,
             "--description", description, "--force")  # fmt: skip


def gh_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in FORCE_COLOR_VARS}
    env["NO_COLOR"] = "1"
    return env
