import subprocess

from boss.gh import Gh
from boss.git import HostRepo


class LocalGh(Gh):
    """Runs Gh.clone's git arguments against a local origin."""

    def __init__(self, origin, cwd):
        super().__init__("o/r", cwd)
        self.origin = origin

    def __call__(self, *args, stdin=None):
        assert args[:2] == ("repo", "clone")
        work_tree, git_args = args[3], args[5:]
        subprocess.run(["git", "clone", *git_args, str(self.origin), work_tree], check=True)
        return ""


def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)


def make_origin(tmp_path, monkeypatch, *branches):
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "boss test")
        monkeypatch.setenv(f"{key}_EMAIL", "boss@example.invalid")
    seed, origin = tmp_path / "seed", tmp_path / "origin.git"
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "README.md").write_text("hello\n")
    git("-C", str(seed), "add", ".")
    git("-C", str(seed), "commit", "--quiet", "-m", "seed")
    for branch in branches:
        git("-C", str(seed), "checkout", "--quiet", "-b", branch, "main")
        (seed / f"{branch.replace('/', '-')}.txt").write_text(branch)
        git("-C", str(seed), "add", ".")
        git("-C", str(seed), "commit", "--quiet", "-m", branch)
    git("-C", str(seed), "checkout", "--quiet", "main")  # origin's default branch
    git("clone", "--quiet", "--bare", str(seed), str(origin))
    return origin


def clone(tmp_path, origin):
    work, git_dir = tmp_path / "work", tmp_path / "git"
    LocalGh(origin, tmp_path).clone(work, git_dir)
    return HostRepo(git_dir, work)


def remote_branches(repo):
    return set(repo("for-each-ref", "--format=%(refname)", "refs/remotes/").split())


def test_a_run_sees_only_the_default_branch_and_its_own(tmp_path, monkeypatch):
    origin = make_origin(tmp_path, monkeypatch, "issue-1", "boss/issue-1", "boss/issue-2")
    repo = clone(tmp_path, origin)
    assert repo.start_branch("boss/issue-1")
    assert (repo.work_tree / "boss-issue-1.txt").exists()
    assert remote_branches(repo) == {
        "refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/boss/issue-1",
    }  # fmt: skip
    repo.give_agent_a_copy()
    agent = HostRepo(repo.work_tree / ".git", repo.work_tree)
    assert "issue-1.txt" not in agent("log", "--all", "--name-only", "--format=").split()


def test_a_new_issue_starts_from_the_default_branch(tmp_path, monkeypatch):
    origin = make_origin(tmp_path, monkeypatch, "issue-3")
    repo = clone(tmp_path, origin)
    assert not repo.start_branch("boss/issue-3")
    assert repo.branch() == "boss/issue-3"
    assert remote_branches(repo) == {"refs/remotes/origin/HEAD", "refs/remotes/origin/main"}
