"""Isolated containers for agents and checks.

Each run gets its own internal Docker network, which has no route out, and a gate
container that is the only thing on both that network and the default bridge. Agent and
check containers join only the internal network, so the gate's allowlist is all they can
reach. They also run as the host user with no capabilities, a read-only root filesystem,
resource limits and a wall-clock timeout. The only host paths they see are the ones a run
mounts: the work tree, and the run's read-only input directory.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROXY = "http://gate:3128"


@dataclass(frozen=True)
class Limits:
    memory: str = "8g"
    cpus: str = "4"
    pids: int = 1024
    tmp_size: str = "4g"


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    read_only: bool = False

    def arg(self) -> str:
        suffix = ",readonly" if self.read_only else ""
        return f"type=bind,source={self.host},target={self.container}{suffix}"


@dataclass(frozen=True)
class Result:
    exit_code: int | None  # None if the container was killed at the timeout
    log: Path

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


@dataclass
class Sandbox:
    run_id: str
    image: str
    log_dir: Path
    allow: list[str] = field(default_factory=list)  # host[:port] the gate admits over HTTPS
    forwards: list[str] = field(default_factory=list)  # port:host:port the gate forwards
    limits: Limits = field(default_factory=Limits)

    @property
    def network(self) -> str:
        return f"boss-{self.run_id}"

    def __enter__(self) -> "Sandbox":
        self.log_dir.mkdir(parents=True, exist_ok=True)
        docker("network", "create", "--internal", "--label", f"boss.run={self.run_id}",
               self.network)  # fmt: skip
        try:
            docker(*gate_args(self))
            docker("network", "connect", "bridge", f"{self.network}-gate")
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        gate_log = subprocess.run(
            ["docker", "logs", f"{self.network}-gate"], capture_output=True, text=True
        )
        (self.log_dir / "gate.log").write_text(gate_log.stdout + gate_log.stderr)
        ids = docker("ps", "-aq", "--filter", f"label=boss.run={self.run_id}").split()
        if ids:
            docker("rm", "-f", *ids)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True)

    def run(
        self,
        name: str,
        command: list[str],
        *,
        mounts: list[Mount],
        timeout: int,
        env: dict[str, str] | None = None,
        inherit_env: list[str] | None = None,
    ) -> Result:
        container = f"{self.network}-{name}"
        log = self.log_dir / f"{name}.log"
        args = run_args(self, container, command, mounts, env or {}, inherit_env or [])
        with log.open("w") as out:
            process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT)
            try:
                return Result(process.wait(timeout=timeout), log)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "kill", container], capture_output=True)
                process.wait()
                with log.open("a") as tail:
                    tail.write(f"\n[boss] killed after {timeout}s\n")
                return Result(None, log)


def hardening(limits: Limits) -> list[str]:
    return [
        "--read-only",
        "--tmpfs", f"/tmp:rw,exec,size={limits.tmp_size},mode=1777",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(limits.pids),
        "--memory", limits.memory,
        "--memory-swap", limits.memory,
        "--cpus", limits.cpus,
    ]  # fmt: skip


def gate_args(sandbox: Sandbox) -> list[str]:
    args = [
        "run", "-d", "--name", f"{sandbox.network}-gate",
        "--label", f"boss.run={sandbox.run_id}",
        "--network", sandbox.network, "--network-alias", "gate",
        "--user", "65534:65534", "--entrypoint", "python3",
        *hardening(Limits(memory="256m", cpus="1", pids=256, tmp_size="16m")),
        sandbox.image, "/usr/local/bin/boss-gate",
    ]  # fmt: skip
    for host in sandbox.allow:
        args += ["--allow", host]
    for spec in sandbox.forwards:
        args += ["--forward", spec]
    return args


def run_args(
    sandbox: Sandbox,
    container: str,
    command: list[str],
    mounts: list[Mount],
    env: dict[str, str],
    inherit_env: list[str],
) -> list[str]:
    proxy_env = {
        "HTTPS_PROXY": PROXY, "HTTP_PROXY": PROXY, "https_proxy": PROXY, "http_proxy": PROXY,
        "NO_PROXY": "gate,localhost,127.0.0.1", "no_proxy": "gate,localhost,127.0.0.1",
    }  # fmt: skip
    args = [
        "run", "--rm", "--init", "--name", container,
        "--label", f"boss.run={sandbox.run_id}",
        "--network", sandbox.network,
        "--user", f"{os.getuid()}:{os.getgid()}",
        *hardening(sandbox.limits),
        "--workdir", "/work",
    ]  # fmt: skip
    for mount in mounts:
        args += ["--mount", mount.arg()]
    for key, value in {**proxy_env, **env}.items():
        args += ["--env", f"{key}={value}"]
    # Secrets pass by name, so their values never appear in an argument list.
    for key in inherit_env:
        args += ["--env", key]
    return ["docker", *args, sandbox.image, *command]


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def image_exists(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return result.returncode == 0
