import pytest

from boss import gh


@pytest.fixture
def boss_token(monkeypatch):
    monkeypatch.setenv(gh.BOSS_GH_TOKEN, "boss-bot-token")


def test_gh_env_turns_off_forced_color(monkeypatch, boss_token, tmp_path):
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    monkeypatch.setenv("GH_FORCE_TTY", "1")
    env = gh.gh_env(tmp_path / "gh")
    assert "CLICOLOR_FORCE" not in env
    assert "GH_FORCE_TTY" not in env
    assert env["NO_COLOR"] == "1"


def test_gh_runs_as_boss_with_its_own_config(monkeypatch, boss_token, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "owner-token")
    monkeypatch.setenv("GITHUB_TOKEN", "owner-token")
    env = gh.gh_env(tmp_path / "gh")
    assert env["GH_TOKEN"] == "boss-bot-token"
    assert "GITHUB_TOKEN" not in env
    # The owner's gh config (login, aliases, git_protocol) never applies.
    assert env["GH_CONFIG_DIR"] == str(tmp_path / "gh")
    assert (tmp_path / "gh").is_dir()


def test_gh_refuses_to_run_without_boss_token(monkeypatch, tmp_path):
    monkeypatch.delenv(gh.BOSS_GH_TOKEN, raising=False)
    monkeypatch.setenv("GH_TOKEN", "owner-token")
    with pytest.raises(gh.GhError, match=gh.BOSS_GH_TOKEN):
        gh.gh_env(tmp_path / "gh")
