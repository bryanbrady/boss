"""Host-side git for a run.

The real git directory stays outside the work tree, and every command names it
explicitly, so nothing an agent writes into /work (a .git file, hooks, config such as
core.fsmonitor) is ever read or run on the host. The agent gets its own copy of the
history at /work/.git to use as it likes; boss ignores that copy.

boss never force-pushes. `push` is fast-forward only, and `__call__` refuses any push
that could overwrite a branch.
"""

import shutil
import subprocess
from pathlib import Path

HARDENED = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
FORCE_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror"}


class PushRejected(RuntimeError):
    pass


class HostRepo:
    def __init__(self, git_dir: Path, work_tree: Path):
        self.git_dir = git_dir
        self.work_tree = work_tree

    def __call__(self, *args: str) -> str:
        if (
            args
            and args[0] == "push"
            and any(a in FORCE_FLAGS or a.startswith(("--force", "+")) for a in args[1:])
        ):
            raise ValueError(f"boss never force-pushes: git {' '.join(args)}")
        result = subprocess.run(
            ["git", *HARDENED, f"--git-dir={self.git_dir}", f"--work-tree={self.work_tree}", *args],
            cwd=self.git_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])}: {result.stderr.strip()}")
        return result.stdout

    def give_agent_a_copy(self) -> None:
        """Replace the .git file `clone --separate-git-dir` left in the work tree with a
        throwaway copy of the history."""
        dot_git = self.work_tree / ".git"
        if dot_git.is_dir() and not dot_git.is_symlink():
            shutil.rmtree(dot_git)
        else:
            dot_git.unlink(missing_ok=True)
        shutil.copytree(self.git_dir, dot_git, symlinks=True)

    def branch(self) -> str:
        return self("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head(self) -> str:
        return self("rev-parse", "HEAD").strip()

    def start_branch(self, branch: str) -> bool:
        """Check out `branch`, building on it if it's already on the remote. Returns True
        if it was, so earlier work (an earlier run's, or a person's) is kept. The clone has
        only the default branch, so this is the one other branch a run fetches."""
        if self("ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
            self("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
            self("checkout", "--quiet", "-b", branch, f"origin/{branch}")
            return True
        self("checkout", "--quiet", "-b", branch)
        return False

    def changed(self) -> bool:
        return bool(self("status", "--porcelain").strip())

    def commit_all(self, message: str) -> None:
        self("add", "--all")
        self("commit", "--quiet", "--message", message)

    def diff(self, since: str) -> str:
        return self("diff", since, "HEAD")

    def push(self, branch: str) -> None:
        """Fast-forward only. If the branch moved on the remote, this fails rather than
        overwriting what's there."""
        try:
            self("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        except RuntimeError as error:
            if "rejected" in str(error) or "non-fast-forward" in str(error):
                raise PushRejected(
                    f"{branch} changed on the remote during the run, so boss didn't push. "
                    "Queue the issue again to build on the new commits."
                ) from error
            raise
