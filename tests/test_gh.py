from boss import gh


def test_gh_env_turns_off_forced_color(monkeypatch):
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    monkeypatch.setenv("GH_FORCE_TTY", "1")
    env = gh.gh_env()
    assert "CLICOLOR_FORCE" not in env
    assert "GH_FORCE_TTY" not in env
    assert env["NO_COLOR"] == "1"
