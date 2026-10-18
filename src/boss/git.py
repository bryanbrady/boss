"""Host-side git for a run.

The real git directory stays outside the work tree, and every command names it
explicitly, so nothing an agent writes into /work (a .git file, hooks, config such as
core.fsmonitor) is ever read or run on the host. The agent gets its own copy of the
history at /work/.git to use as it likes; boss ignores that copy.

Submodules are the exception that still has to be handled by hand. A submodule the agent
checked out has its own git directory, with its own config and attributes, inside the
work tree. Host git descends into a submodule's working directory for `status`, `add` and
the like, and runs there with that agent-written config, so a planted `filter.*.clean` or
`core.fsmonitor` would execute on the host. `absorb_submodules` closes that off: it reads
each initialized submodule's pinned commit straight from its ref files (no git process in
the agent's git directory), deletes every nested `.git` in the work tree so host git can't
step into one, and records the pinned commits as gitlinks with `update-index`.

boss never force-pushes. `push` is fast-forward only, and `__call__` refuses any push
that could overwrite a branch.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

HARDENED = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
FORCE_FLAGS = {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror"}
OBJECT_ID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")  # sha-1, or sha-256


class PushRejected(RuntimeError):
    pass


class HostRepo:
    def __init__(self, git_dir: Path, work_tree: Path):
        self.git_dir = git_dir
        self.work_tree = work_tree
        self.gitlinks: dict[str, str] = {}  # submodule path -> pinned commit, set by absorb

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

    def absorb_submodules(self) -> None:
        """Make the agent's work tree safe for host git to read, then stage any submodules
        as gitlinks. Call once, after the agent finishes and before any other host git
        command touches the work tree (status, add, ...). See the module docstring."""
        self.gitlinks = self._pinned_submodules()
        strip_nested_git(self.work_tree)
        for path, commit in self.gitlinks.items():
            self("update-index", "--add", "--cacheinfo", f"160000,{commit},{path}")

    def _pinned_submodules(self) -> dict[str, str]:
        """Each initialized submodule's checked-out commit, read from its ref files. Nothing
        here runs git in a submodule's git directory or trusts its config: a `.git` file or
        gitdir that points somewhere odd can at worst have us read a file as text, and the
        value is used only if it is an object id."""
        pinned = {}
        for path in submodule_paths(self.work_tree):
            module = module_dir(self.work_tree, path)
            commit = resolve_head(module) if module else None
            if commit:
                pinned[path] = commit
        return pinned

    def changed(self) -> bool:
        return bool(self("status", "--porcelain").strip())

    def commit_all(self, message: str) -> None:
        if self.gitlinks:  # add everything but the submodule paths; they are gitlinks already
            self("add", "--all", "--", ".", *(f":(exclude){p}" for p in self.gitlinks))
        else:
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


def submodule_paths(work_tree: Path) -> list[str]:
    """The submodule working-tree paths declared in .gitmodules, read as plain text (no git
    config machinery, so no `include` directive can pull in another file). A path that is
    absolute, escapes the work tree, or runs through a symlink is dropped."""
    gitmodules = work_tree / ".gitmodules"
    if not gitmodules.is_file():
        return []
    paths = []
    for line in gitmodules.read_text(errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "path":
            path = value.strip()
            if path and contained(work_tree, path):
                paths.append(path)
    return paths


def contained(work_tree: Path, path: str) -> bool:
    if os.path.isabs(path) or ".." in Path(path).parts:
        return False
    target = work_tree / path
    try:
        resolved = target.resolve()
    except OSError:
        return False
    return resolved.is_dir() and work_tree.resolve() in resolved.parents


def module_dir(work_tree: Path, path: str) -> Path | None:
    """The git directory of an initialized submodule at `path`, or None if it isn't
    checked out. A `.git` file points to it; an embedded `.git` directory is it."""
    dot_git = work_tree / path / ".git"
    if dot_git.is_dir() and not dot_git.is_symlink():
        return dot_git
    if dot_git.is_file():
        _, _, pointer = dot_git.read_text(errors="replace").partition("gitdir:")
        if pointer.strip():
            return (work_tree / path / pointer.strip()).resolve()
    return None


def resolve_head(module: Path) -> str | None:
    """A submodule's checked-out commit, by reading HEAD and following one ref if needed.
    Pure file reads; the result is returned only when it is an object id."""
    try:
        head = (module / "HEAD").read_text(errors="replace").strip()
    except OSError:
        return None
    _, sep, ref = head.partition("ref:")
    if sep:
        head = read_ref(module, ref.strip()) or ""
    return head if OBJECT_ID.fullmatch(head) else None


def read_ref(module: Path, ref: str) -> str | None:
    loose = module / ref
    if loose.is_file() and module.resolve() in loose.resolve().parents:
        return loose.read_text(errors="replace").strip()
    packed = module / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(errors="replace").splitlines():
            commit, sep, name = line.partition(" ")
            if sep and name.strip() == ref:
                return commit.strip()
    return None


def strip_nested_git(work_tree: Path) -> None:
    """Delete every nested `.git` in the work tree except the top-level one (which host git
    doesn't use anyway). After this, a submodule or an embedded repo is just a directory, so
    host git never descends into it or reads its config. Symlinks are not followed."""
    top = (work_tree / ".git").resolve()
    for root, dirs, files in os.walk(work_tree):
        for name in list(dirs):
            full = Path(root) / name
            if name != ".git":
                continue
            dirs.remove(name)  # don't descend into any .git
            if full.resolve() != top:
                shutil.rmtree(full) if not full.is_symlink() else full.unlink()
        if ".git" in files:
            (Path(root) / ".git").unlink()  # a submodule's gitlink file
