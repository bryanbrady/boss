import subprocess

from boss.gh import Gh
from boss.git import CREDENTIALS, HostRepo


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


def make_lib(tmp_path, monkeypatch):
    """A standalone repo to use as a submodule, with a second commit to bump to."""
    lib = tmp_path / "lib"
    git("init", "--quiet", "-b", "main", str(lib))
    commits = []
    for text in ("one\n", "two\n"):
        (lib / "data.txt").write_text(text)
        git("-C", str(lib), "add", ".")
        git("-C", str(lib), "commit", "--quiet", "-m", text.strip())
        commits.append(git_out("-C", str(lib), "rev-parse", "HEAD"))
    return lib, commits


def git_out(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def allow_file(monkeypatch):
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")  # submodule add from a local path


def test_absorb_commits_a_submodule_without_running_its_config_on_the_host(tmp_path, monkeypatch):
    """A submodule's git config lives under the work tree and the agent controls it. boss
    must stage the submodule without host git ever descending into it and running that
    config, and must still record the right pinned commit."""
    allow_file(monkeypatch)
    origin = make_origin(tmp_path, monkeypatch)
    lib, (first, _) = make_lib(tmp_path, monkeypatch)
    repo = clone(tmp_path, origin)
    repo.start_branch("boss/issue-1")
    repo.give_agent_a_copy()

    # The agent adds the submodule in its own copy of the work tree.
    work = repo.work_tree
    git("-C", str(work), "-c", "protocol.file.allow=always", "submodule", "add", "--quiet",
        str(lib), "libs/lib")  # fmt: skip
    git("-C", str(work / "libs/lib"), "checkout", "--quiet", first)

    # ...and leaves config in the submodule that would run a command when host git stages it.
    marker = tmp_path / "ran-on-host"
    module = work / ".git/modules/libs/lib"
    (module / "config").open("a").write(f'[filter "x"]\n\tclean = "touch {marker}; cat"\n')
    (module / "info").mkdir(exist_ok=True)
    (module / "info/attributes").write_text("* filter=x\n")
    (work / "libs/lib/data.txt").write_text("edited\n")  # forces host add to hash the blob

    repo.absorb_submodules()
    repo.commit_all("add submodule")

    assert not marker.exists()  # the planted filter never ran on the host
    entry = repo("ls-tree", "HEAD", "libs/lib").split()
    assert entry[:2] == ["160000", "commit"] and entry[2] == first
    assert ".gitmodules" in repo("ls-tree", "--name-only", "HEAD").split()


def test_absorb_records_a_submodule_bump(tmp_path, monkeypatch):
    allow_file(monkeypatch)
    origin = make_origin(tmp_path, monkeypatch)
    lib, (first, second) = make_lib(tmp_path, monkeypatch)
    repo = clone(tmp_path, origin)
    repo.start_branch("boss/issue-1")
    repo.give_agent_a_copy()
    work = repo.work_tree
    git("-C", str(work), "-c", "protocol.file.allow=always", "submodule", "add", "--quiet",
        str(lib), "libs/lib")  # fmt: skip
    git("-C", str(work / "libs/lib"), "checkout", "--quiet", second)

    repo.absorb_submodules()
    assert repo.gitlinks == {"libs/lib": second}


def test_absorb_strips_a_plain_nested_repo_the_agent_created(tmp_path, monkeypatch):
    """A nested repo that isn't a declared submodule is flattened to plain files, so its
    config can't run and its history isn't committed."""
    origin = make_origin(tmp_path, monkeypatch)
    repo = clone(tmp_path, origin)
    repo.start_branch("boss/issue-1")
    repo.give_agent_a_copy()
    work = repo.work_tree
    nested = work / "vendor"
    nested.mkdir()
    git("init", "--quiet", "-b", "main", str(nested))
    (nested / "thing.txt").write_text("vendored\n")

    repo.absorb_submodules()
    assert not (nested / ".git").exists()
    assert repo.gitlinks == {}
    repo.commit_all("vendor")
    tracked = repo("ls-tree", "-r", "--name-only", "HEAD").split()
    assert "vendor/thing.txt" in tracked


def test_host_git_answers_github_with_boss_token_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_BOSS_TOKEN", "boss-bot-token")
    repo = HostRepo(tmp_path, tmp_path)
    git("init", "--quiet", str(tmp_path))
    fill = subprocess.run(
        ["git", *CREDENTIALS, "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
        env=repo.env(), cwd=tmp_path, check=True,
    )  # fmt: skip
    assert "username=x-access-token" in fill.stdout
    assert "password=boss-bot-token" in fill.stdout
    assert "boss-bot-token" not in " ".join(CREDENTIALS)  # read from the environment


def test_boss_commits_as_its_own_account(tmp_path, monkeypatch):
    origin = make_origin(tmp_path, monkeypatch)
    work, git_dir = tmp_path / "work", tmp_path / "git"
    LocalGh(origin, tmp_path).clone(work, git_dir)
    bot = ("the-boss-bot", "1+the-boss-bot@users.noreply.github.com")
    repo = HostRepo(git_dir, work, author=bot)
    (work / "new.txt").write_text("x")
    repo.commit_all("change")
    who = repo("log", "-1", "--format=%an <%ae> / %cn <%ce>").strip()
    assert who == ("the-boss-bot <1+the-boss-bot@users.noreply.github.com> / "
                   "the-boss-bot <1+the-boss-bot@users.noreply.github.com>")  # fmt: skip
