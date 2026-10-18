"""The agent boss runs inside the sandbox: Claude Code."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Package registries every container may reach through the gate, so the agent and checks
# can install dependencies. uv fetches Python builds from GitHub release assets.
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

CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    home: Path
    image: str = "boss-agent"
    claude_model: str | None = None
    agent_timeout: int = 1800
    check_timeout: int = 900
    extra_allow: tuple[str, ...] = ()
    nested_docker: bool = False  # the agent and checks get their own Docker daemon (Sysbox)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = os.environ
        values = {
            "home": Path(env.get("BOSS_HOME", Path.home() / ".local/state/boss")),
            "image": env.get("BOSS_IMAGE", "boss-agent"),
            "claude_model": env.get("BOSS_CLAUDE_MODEL"),
            "nested_docker": env.get("BOSS_DOCKER", "") not in ("", "0"),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


@dataclass(frozen=True)
class Agent:
    description: str
    command: list[str]
    files: dict[str, str] = field(default_factory=dict)  # written to /boss
    env: dict[str, str] = field(default_factory=dict)
    inherit_env: list[str] = field(default_factory=list)  # secrets, passed by name
    allow: list[str] = field(default_factory=list)


def claude_agent(settings: Settings) -> Agent:
    if not os.environ.get(CLAUDE_TOKEN):
        raise ConfigError(f"set {CLAUDE_TOKEN}; make one with `claude setup-token`")
    model = f" --model {settings.claude_model}" if settings.claude_model else ""
    return Agent(
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
