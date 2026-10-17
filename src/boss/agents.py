"""The agent CLIs boss runs inside the sandbox: opencode on a local model, or Claude Code."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Package registries every container may reach through the gate, so agents and checks can
# install dependencies. uv fetches Python builds from GitHub release assets.
REGISTRIES = [
    "pypi.org", "files.pythonhosted.org",
    "registry.npmjs.org",
    "crates.io", "index.crates.io", "static.crates.io",
    "proxy.golang.org", "sum.golang.org",
    "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
]  # fmt: skip

# What a nested-Docker run's daemon pulls from: Docker Hub's API, auth and blob storage.
DOCKER_HUB = [
    "registry-1.docker.io", "auth.docker.io",
    "production.cloudflare.docker.com", "production.cloudfront.docker.com",
]  # fmt: skip

OLLAMA_PORT = 11434
CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    home: Path
    image: str = "boss-agent"
    ollama_url: str | None = None
    local_model: str | None = None
    claude_model: str | None = None
    agent_timeout: int = 1800
    check_timeout: int = 900
    extra_allow: tuple[str, ...] = ()
    nested_docker: bool = False  # agents and checks get their own Docker daemon (Sysbox)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = os.environ
        values = {
            "home": Path(env.get("BOSS_HOME", Path.home() / ".local/state/boss")),
            "image": env.get("BOSS_IMAGE", "boss-agent"),
            "ollama_url": env.get("BOSS_OLLAMA_URL"),
            "local_model": env.get("BOSS_LOCAL_MODEL"),
            "claude_model": env.get("BOSS_CLAUDE_MODEL"),
            "nested_docker": env.get("BOSS_DOCKER", "") not in ("", "0"),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass(frozen=True)
class Agent:
    name: str  # "local" or "claude", matching the agent:* label
    description: str
    command: list[str]
    files: dict[str, str] = field(default_factory=dict)  # written to /boss
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: list[str] = field(default_factory=list)  # secrets, passed by name
    allow: list[str] = field(default_factory=list)
    forwards: list[str] = field(default_factory=list)


def make_agent(settings: Settings, name: str) -> Agent:
    return local_agent(settings) if name == "local" else claude_agent(settings)


def local_agent(settings: Settings) -> Agent:
    if not settings.ollama_url:
        raise ConfigError("set BOSS_OLLAMA_URL (or --ollama-url), e.g. http://10.0.0.5:11434")
    if not settings.local_model:
        raise ConfigError("set BOSS_LOCAL_MODEL (or --local-model) to an Ollama model name")
    url = urlparse(settings.ollama_url)
    if not url.hostname:
        raise ConfigError(f"BOSS_OLLAMA_URL has no host: {settings.ollama_url}")
    model = settings.local_model
    config = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": f"ollama/{model}",
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {"baseURL": f"http://gate:{OLLAMA_PORT}/v1"},
                "models": {model: {"name": model}},
            }
        },
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
    }
    return Agent(
        name="local",
        description=f"opencode, ollama/{model}",
        command=["sh", "-c", 'exec opencode run --auto -m "$BOSS_MODEL" "$(cat /boss/prompt.md)"'],
        files={"opencode.json": json.dumps(config, indent=2)},
        env={"OPENCODE_CONFIG": "/boss/opencode.json", "BOSS_MODEL": f"ollama/{model}"},
        allow=[*REGISTRIES, *settings.extra_allow],
        forwards=[f"{OLLAMA_PORT}:{url.hostname}:{url.port or OLLAMA_PORT}"],
    )


def claude_agent(settings: Settings) -> Agent:
    if not os.environ.get(CLAUDE_TOKEN):
        raise ConfigError(f"set {CLAUDE_TOKEN}; make one with `claude setup-token`")
    model = f" --model {settings.claude_model}" if settings.claude_model else ""
    return Agent(
        name="claude",
        description=f"Claude Code{f', {settings.claude_model}' if settings.claude_model else ''}",
        command=[
            "sh",
            "-c",
            f"exec claude -p --dangerously-skip-permissions --output-format text{model}"
            " < /boss/prompt.md",
        ],  # fmt: skip
        inherit_env=[CLAUDE_TOKEN],
        allow=["api.anthropic.com", *REGISTRIES, *settings.extra_allow],
    )
