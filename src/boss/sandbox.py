"""Isolated containers for agents and checks.

Each run gets its own internal Docker network, which has no route out, and a gate
container that is the only thing on both that network and the default bridge. Agent and
check containers join only the internal network, so the gate's allowlist is all they can
reach. They also run as the host user with no capabilities, a read-only root filesystem,
resource limits and a wall-clock timeout. The only host paths they see are the ones a run
mounts: the work tree, and the run's read-only input directory.

With nested Docker, commands run in a Sysbox container instead: a box with its own Docker
daemon, so an agent can build images and bring up a compose stack. Root in the box is an
unprivileged user on the host, and the box can't see the host's daemon or containers. It
sits on the same internal network, behind the same gate. The hardening above doesn't
apply, because the daemon needs root, capabilities and a writable filesystem inside the
box. Resource limits still do.

A box gets no host mounts. Root in a box owns files it writes to a bind mount as real root
on the host, setuid bits included, so mounts are copied in and out as tar streams instead.
What comes back is owned by the host user, with setuid bits stripped. A run's boxes share
one volume for the daemon's storage, so the checks reuse images the agent pulled or built.
"""

import os
import shutil
import subprocess
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

PROXY = "http://gate:3128"
SYSBOX = "sysbox-runc"
DAEMON_START_TIMEOUT = 60


@dataclass(frozen=True)
class Limits:
    memory: str = "8g"
    cpus: str = "4"
    pids: int = 1024
    tmp_size: str = "4g"
    box_pids: int = 8192  # a box runs a daemon and a whole stack


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
    limits: Limits = field(default_factory=Limits)
    nested_docker: bool = False

    @property
    def network(self) -> str:
        return f"boss-{self.run_id}"

    @property
    def docker_volume(self) -> str:
        return f"{self.network}-docker"

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
            docker("rm", "-f", "-v", *ids)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True)
        subprocess.run(["docker", "volume", "rm", self.docker_volume], capture_output=True)

    @contextmanager
    def session(self, name: str, mounts: list[Mount]) -> Iterator["Plain | Box"]:
        """Somewhere to run commands one after another. Normally each command gets a fresh
        container. With nested Docker they share one box, so a check can start a stack
        and the next one can talk to it."""
        if not self.nested_docker:
            yield Plain(self, mounts)
            return
        box = Box(self, f"{self.network}-{name}", mounts)
        try:
            box.start()
            yield box
            if not box.killed:  # a timeout kills the whole box, so there's nothing to copy
                box.copy_out()
        finally:
            box.remove()

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
        args = run_args(self, container, command, mounts, env or {}, inherit_env or [])
        return supervise(args, self.log_dir / f"{name}.log", timeout, container)


@dataclass
class Plain:
    """A session of plain sandboxed containers, one per command."""

    sandbox: Sandbox
    mounts: list[Mount]

    def run(self, name: str, command: list[str], **kwargs) -> Result:
        return self.sandbox.run(name, command, mounts=self.mounts, **kwargs)


@dataclass
class Box:
    """A session in one nested-Docker container."""

    sandbox: Sandbox
    container: str
    mounts: list[Mount]
    killed: bool = False

    def start(self) -> None:
        docker(*box_args(self.sandbox, self.container))
        for _ in range(DAEMON_START_TIMEOUT):
            if self.exec_root("docker", "info").returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"Docker didn't start in {self.container}: {self.daemon_log()}")
        # Containers and volumes the agent left in the shared storage would carry its state
        # into the checks, which should start from a fresh stack. Images stay as a cache.
        self.exec_root("sh", "-c", "docker ps -aq | xargs -r docker rm -f"
                       " && docker volume prune -af && docker network prune -f")  # fmt: skip
        for mount in self.mounts:
            self.copy_in(mount)

    def run(
        self,
        name: str,
        command: list[str],
        *,
        timeout: int,
        env: dict[str, str] | None = None,
        inherit_env: list[str] | None = None,
    ) -> Result:
        args = exec_args(self.container, command, env or {}, inherit_env or [])
        result = supervise(args, self.sandbox.log_dir / f"{name}.log", timeout, self.container)
        self.killed |= result.timed_out
        return result

    def copy_in(self, mount: Mount) -> None:
        """Stream the mount in as a tar (docker cp fails on a Sysbox container with a
        volume). Read-only mounts arrive owned by root, so the agent can read but not change
        them. The rest keep the host user's ownership."""
        owner = "--no-same-owner" if mount.read_only else "--same-owner"
        script = f'mkdir -p "$1" && tar -x {owner} -C "$1" -f -'
        untar = ["docker", "exec", "-i", self.container, "sh", "-c", script, "-", mount.container]
        process = subprocess.Popen(untar, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
            archive.add(mount.host, arcname=".")
        process.stdin.close()
        finish(process, f"copying {mount.host} in")

    def copy_out(self) -> None:
        """Replace each writable mount with what's in the box, minus owners, setuid bits
        and links that point outside it (see keep_inside). The mount is only replaced once
        the whole copy has arrived, so a failed copy leaves it as it was."""
        for mount in self.mounts:
            if mount.read_only:
                continue
            incoming = mount.host.with_name(f"{mount.host.name}.incoming")
            incoming.mkdir()
            try:
                self.receive(mount.container, incoming)
            except BaseException:
                shutil.rmtree(incoming)
                raise
            shutil.rmtree(mount.host)
            incoming.rename(mount.host)

    def receive(self, path: str, dest: Path) -> None:
        what = f"copying {path} out"
        tar = ["tar", "-c", "-C", path, "-f", "-", "."]
        process = subprocess.Popen(["docker", "exec", self.container, *tar],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # fmt: skip
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                archive.extractall(dest, filter=keep_inside)
        except tarfile.TarError as error:
            process.stdout.close()
            finish(process, what)  # docker's own error, e.g. the box isn't running
            raise RuntimeError(f"{what}: {error}") from error
        finish(process, what)

    def remove(self) -> None:
        name = self.container.removeprefix(f"{self.sandbox.network}-")
        (self.sandbox.log_dir / f"{name}-dockerd.log").write_text(self.daemon_log())
        subprocess.run(["docker", "rm", "-f", "-v", self.container], capture_output=True)

    def exec_root(self, *command: str) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", self.container, *command], capture_output=True)

    def daemon_log(self) -> str:
        result = subprocess.run(["docker", "logs", self.container], capture_output=True, text=True)
        return result.stdout + result.stderr


def keep_inside(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """tarfile's "data" filter, except that a link pointing outside the directory is
    dropped rather than failing the whole copy. Tools make these as a matter of course: a
    virtualenv's bin/python links to the box's /usr/bin/python3."""
    try:
        return tarfile.data_filter(member, dest)
    except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
        return None


def finish(process: subprocess.Popen, what: str) -> None:
    if code := process.wait():
        error = process.stderr.read().decode().strip()
        raise RuntimeError(f"{what}: {error or f'exit code {code}'}")


def supervise(args: list[str], log: Path, timeout: int, container: str) -> Result:
    """Run a docker command to completion, killing `container` at the timeout."""
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
    return args


PROXY_ENV = {
    "HTTPS_PROXY": PROXY, "HTTP_PROXY": PROXY, "https_proxy": PROXY, "http_proxy": PROXY,
    "NO_PROXY": "gate,localhost,127.0.0.1", "no_proxy": "gate,localhost,127.0.0.1",
}  # fmt: skip


def run_args(
    sandbox: Sandbox,
    container: str,
    command: list[str],
    mounts: list[Mount],
    env: dict[str, str],
    inherit_env: list[str],
) -> list[str]:
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
    args += env_args({**PROXY_ENV, **env}, inherit_env)
    return ["docker", *args, sandbox.image, *command]


def box_args(sandbox: Sandbox, container: str) -> list[str]:
    limits = sandbox.limits
    args = [
        "run", "-d", "--runtime", SYSBOX, "--name", container,
        "--label", f"boss.run={sandbox.run_id}",
        "--network", sandbox.network,
        "--mount", f"type=volume,source={sandbox.docker_volume},target=/var/lib/docker",
        "--pids-limit", str(limits.box_pids),
        "--memory", limits.memory,
        "--memory-swap", limits.memory,
        "--cpus", limits.cpus,
        "--entrypoint", "boss-dockerd",
    ]  # fmt: skip
    args += env_args(PROXY_ENV, [])  # for the daemon's own pulls
    return [*args, sandbox.image, str(os.getgid())]


def exec_args(
    container: str, command: list[str], env: dict[str, str], inherit_env: list[str]
) -> list[str]:
    args = ["exec", "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/work"]
    args += env_args({**PROXY_ENV, **env}, inherit_env)
    # docker exec skips the image's entrypoint, which sets up the user's home directory.
    return ["docker", *args, container, "boss-entrypoint", *command]


def env_args(env: dict[str, str], inherit_env: list[str]) -> list[str]:
    args = []
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    # Secrets pass by name, so their values never appear in an argument list.
    for key in inherit_env:
        args += ["--env", key]
    return args


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])}: {result.stderr.strip()}")
    return result.stdout


def image_exists(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return result.returncode == 0


def has_sysbox() -> bool:
    result = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                            capture_output=True, text=True)  # fmt: skip
    return f'"{SYSBOX}"' in result.stdout
