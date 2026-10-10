from pathlib import Path

import pytest

from boss.agents import CLAUDE_TOKEN, ConfigError, Settings, claude_agent, local_agent
from boss.sandbox import Mount, Sandbox, gate_args, run_args


def settings(**kwargs):
    return Settings(home=Path("/tmp/boss-test"), **kwargs)


def test_local_agent_needs_url_and_model():
    with pytest.raises(ConfigError):
        local_agent(settings(local_model="m"))
    with pytest.raises(ConfigError):
        local_agent(settings(ollama_url="http://10.0.0.5:11434"))


def test_local_agent_reaches_ollama_only_through_the_gate():
    agent = local_agent(settings(ollama_url="http://10.0.0.5:8080", local_model="qwen"))
    assert agent.forwards == ["11434:10.0.0.5:8080"]
    assert '"baseURL": "http://gate:11434/v1"' in agent.files["opencode.json"]
    assert "api.anthropic.com" not in agent.allow


def test_claude_agent_passes_its_token_by_name(monkeypatch):
    monkeypatch.delenv(CLAUDE_TOKEN, raising=False)
    with pytest.raises(ConfigError):
        claude_agent(settings())
    monkeypatch.setenv(CLAUDE_TOKEN, "sk-secret")
    agent = claude_agent(settings())
    assert agent.inherit_env == [CLAUDE_TOKEN]
    assert "api.anthropic.com" in agent.allow
    sandbox = Sandbox("r1", "img", Path("/tmp"), agent.allow)
    args = run_args(sandbox, "c", agent.command, [], agent.env, agent.inherit_env)
    assert "sk-secret" not in " ".join(args)
    at = args.index(CLAUDE_TOKEN)
    assert args[at - 1 : at + 1] == ["--env", CLAUDE_TOKEN]


def test_containers_are_locked_down():
    sandbox = Sandbox("r1", "img", Path("/tmp"))
    args = run_args(sandbox, "c", ["true"], [Mount(Path("/w"), "/work")], {}, [])
    joined = " ".join(args)
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit",
                 "--memory", "--network boss-r1", "--rm"):  # fmt: skip
        assert flag in joined
    assert "type=bind,source=/w,target=/work" in joined
    assert "/var/run/docker.sock" not in joined


def test_gate_gets_the_allowlist_and_forwards():
    sandbox = Sandbox("r1", "img", Path("/tmp"), ["pypi.org"], ["11434:h:1"])
    args = gate_args(sandbox)
    assert args[-4:] == ["--allow", "pypi.org", "--forward", "11434:h:1"]
    assert "65534:65534" in args
