import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from boss.agents import CLAUDE_TOKEN, ConfigError, Settings, claude_agent
from boss.sandbox import Box, Mount, Sandbox, box_args, exec_args, gate_args, keep_inside, run_args


def settings(**kwargs):
    return Settings(home=Path("/tmp/boss-test"), **kwargs)


def test_claude_agent_needs_a_token_and_reaches_claude_through_the_gate(monkeypatch):
    monkeypatch.delenv(CLAUDE_TOKEN, raising=False)
    with pytest.raises(ConfigError):
        claude_agent(settings())
    monkeypatch.setenv(CLAUDE_TOKEN, "sk-secret")
    agent = claude_agent(settings())
    assert agent.uses_claude_api
    # Open CONNECT access would let anything in the box use its own key to send data out.
    assert "api.anthropic.com" not in agent.allow


def test_the_token_stays_in_the_gate(monkeypatch):
    monkeypatch.setenv(CLAUDE_TOKEN, "sk-secret")
    sandbox = Sandbox("r1", "img", Path("/tmp"), claude_api=True)
    gate = gate_args(sandbox)
    at = gate.index(CLAUDE_TOKEN)
    assert gate[at - 1 : at + 1] == ["--env", CLAUDE_TOKEN]  # by name, so never in `ps`
    assert f"BOSS_RUN_KEY={sandbox.run_key}" in gate
    assert "sk-secret" not in " ".join(gate)

    for args in (
        run_args(sandbox, "c", ["claude"], [], sandbox.agent_env),
        exec_args("box", ["claude"], sandbox.agent_env),
    ):
        assert "sk-secret" not in " ".join(args)
        assert CLAUDE_TOKEN not in args  # passing it by name would hand over the real one
        assert f"{CLAUDE_TOKEN}={sandbox.run_key}" in args
        assert "ANTHROPIC_BASE_URL=http://gate:8080" in args


def test_each_run_gets_its_own_key():
    assert Sandbox("r1", "img", Path("/tmp")).run_key != Sandbox("r1", "img", Path("/tmp")).run_key


def test_a_run_without_claude_gives_the_gate_no_token():
    sandbox = Sandbox("r1", "img", Path("/tmp"))
    assert sandbox.agent_env == {}
    assert CLAUDE_TOKEN not in gate_args(sandbox)


def test_containers_are_locked_down():
    sandbox = Sandbox("r1", "img", Path("/tmp"))
    args = run_args(sandbox, "c", ["true"], [Mount(Path("/w"), "/work")], {})
    joined = " ".join(args)
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit",
                 "--memory", "--network boss-r1", "--rm"):  # fmt: skip
        assert flag in joined
    assert "type=bind,source=/w,target=/work" in joined
    assert "/var/run/docker.sock" not in joined


def test_gate_gets_the_allowlist():
    sandbox = Sandbox("r1", "img", Path("/tmp"), ["pypi.org"])
    args = gate_args(sandbox)
    assert args[-2:] == ["--allow", "pypi.org"]
    assert "65534:65534" in args


def test_boxes_use_sysbox_and_mount_nothing_from_the_host():
    sandbox = Sandbox("r1", "img", Path("/tmp"), nested_docker=True)
    args = box_args(sandbox, "boss-r1-agent")
    joined = " ".join(args)
    assert "--runtime sysbox-runc" in joined
    assert "--network boss-r1" in joined
    assert "type=volume,source=boss-r1-docker,target=/var/lib/docker" in joined
    assert "type=bind" not in joined
    assert "/var/run/docker.sock" not in joined
    assert "--privileged" not in joined
    for flag in ("--pids-limit", "--memory", "--cpus"):
        assert flag in joined


def test_box_commands_run_as_the_host_user():
    args = exec_args("box", ["claude"], {"A": "1"})
    assert args[:3] == ["docker", "exec", "--user"]
    assert args[3] == f"{os.getuid()}:{os.getgid()}"
    assert "HTTPS_PROXY=http://gate:3128" in args
    assert args[-2:] == ["boss-entrypoint", "claude"]


def test_settings_turn_on_nested_docker_from_the_environment(monkeypatch):
    monkeypatch.setenv("BOSS_DOCKER", "1")
    assert Settings.from_env().nested_docker
    monkeypatch.setenv("BOSS_DOCKER", "0")
    assert not Settings.from_env().nested_docker


def test_copy_out_drops_links_that_leave_the_work_tree(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = tarfile.TarInfo("./app.py")
        data.size, data.mode = 3, 0o4755
        archive.addfile(data, io.BytesIO(b"hi\n"))
        for name, target in [
            ("./.venv/bin/python", "/usr/bin/python3"),
            ("./escape", "../../etc/passwd"),
            ("./main.py", "app.py"),
        ]:
            link = tarfile.TarInfo(name)
            link.type, link.linkname = tarfile.SYMTYPE, target
            archive.addfile(link)
    buffer.seek(0)
    with tarfile.open(fileobj=buffer, mode="r|") as archive:
        archive.extractall(tmp_path, filter=keep_inside)

    assert (tmp_path / "app.py").read_text() == "hi\n"
    assert not (tmp_path / "app.py").stat().st_mode & 0o4000  # setuid stripped
    assert os.readlink(tmp_path / "main.py") == "app.py"
    assert not (tmp_path / ".venv/bin/python").is_symlink()
    assert not (tmp_path / "escape").is_symlink()


def box_whose_tar_runs(monkeypatch, tmp_path, script):
    """A Box whose `docker exec ... tar -c` runs `script` on the host instead."""
    popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: popen(["sh", "-c", script], **kw))
    work = tmp_path / "work"
    work.mkdir()
    (work / "README.md").write_text("original\n")
    return Box(Sandbox("r1", "img", tmp_path), "box", [Mount(work, "/work")]), work


def test_copy_out_replaces_the_work_tree_with_the_box_copy(tmp_path, monkeypatch):
    (tmp_path / "box").mkdir()
    (tmp_path / "box" / "new.txt").write_text("from the box\n")
    box, work = box_whose_tar_runs(monkeypatch, tmp_path, f"tar -c -C {tmp_path}/box -f - .")
    box.copy_out()
    assert sorted(p.name for p in work.iterdir()) == ["new.txt"]
    assert not (tmp_path / "work.incoming").exists()


def test_failed_copy_out_leaves_the_work_tree_alone(tmp_path, monkeypatch):
    script = "echo 'Error response from daemon: container box is not running' >&2; exit 1"
    box, work = box_whose_tar_runs(monkeypatch, tmp_path, script)
    with pytest.raises(RuntimeError, match="container box is not running"):
        box.copy_out()
    assert (work / "README.md").read_text() == "original\n"
    assert not (tmp_path / "work.incoming").exists()
