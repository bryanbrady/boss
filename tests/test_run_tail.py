from boss.run import LOG_TAIL_CHARS, tail


def test_tail_strips_color_codes_and_fences(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("\x1b[0m> build\x1b[0m\n```\ndone\n")
    assert tail(log, []) == "> build\n'''\ndone"


def test_secret_on_the_cut_is_fully_redacted(tmp_path):
    log = tmp_path / "agent.log"
    secret = "sk-0123456789abcdef"
    # The secret straddles the point where the tail is cut.
    log.write_text(secret + "x" * (LOG_TAIL_CHARS - 5))
    # The old order (cut, then redact) left the last 5 characters, "bcdef", behind.
    assert secret[-5:] not in tail(log, [secret])
