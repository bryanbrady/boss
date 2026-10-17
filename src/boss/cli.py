"""boss command line."""

import argparse
import sys

from boss import labels
from boss.agents import ConfigError, Settings
from boss.gh import Gh, GhError
from boss.lock import Locked
from boss.run import run_issue
from boss.sandbox import has_sysbox, image_exists
from boss.watch import watch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boss", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("labels", help="create or update boss's labels in a repo").add_argument(
        "repo", help="owner/name"
    )

    run = sub.add_parser("run", help="run one issue now, routed by its labels")
    run.add_argument("repo", help="owner/name")
    run.add_argument("issue", type=int)
    run.add_argument("--no-escalate", action="store_true",
                     help="don't hand a failed local attempt to Claude")  # fmt: skip

    poll = sub.add_parser("watch", help=f"run {labels.QUEUED} issues as they appear")
    poll.add_argument("repo", help="owner/name")
    poll.add_argument("--interval", type=int, default=60, help="seconds between polls")
    poll.add_argument("--once", action="store_true", help="exit when the queue is empty")

    for command in (run, poll):
        command.add_argument("--image", help="sandbox image (BOSS_IMAGE, default boss-agent)")
        command.add_argument("--ollama-url", help="Ollama server URL (BOSS_OLLAMA_URL)")
        command.add_argument("--local-model", help="Ollama model name (BOSS_LOCAL_MODEL)")
        command.add_argument("--claude-model", help="Claude model (BOSS_CLAUDE_MODEL)")
        command.add_argument("--agent-timeout", type=int, help="seconds, default 1800")
        command.add_argument("--allow", action="append", default=[], metavar="HOST[:PORT]",
                             help="extra host the sandbox may reach over HTTPS")  # fmt: skip
        command.add_argument("--docker", action="store_true", default=None,
                             help="give agents and checks their own Docker daemon, in a "
                                  "Sysbox container (BOSS_DOCKER=1)")  # fmt: skip

    args = parser.parse_args(argv)
    if args.command == "labels":
        gh = Gh(args.repo, Settings.from_env().home)
        for name, (color, description) in labels.ALL.items():
            gh.create_label(name, color, description)
            print(f"ok: {name}")
        return 0

    settings = Settings.from_env(
        image=args.image,
        ollama_url=args.ollama_url,
        local_model=args.local_model,
        claude_model=args.claude_model,
        agent_timeout=args.agent_timeout,
        extra_allow=tuple(args.allow) or None,
        nested_docker=args.docker,
    )
    if not image_exists(settings.image):
        print(f"no image {settings.image}; build it: docker build -t boss-agent images/agent")
        return 1
    if settings.nested_docker and not has_sysbox():
        print("--docker needs the Sysbox runtime: https://github.com/nestybox/sysbox")
        return 1
    settings.home.mkdir(parents=True, exist_ok=True)
    gh = Gh(args.repo, settings.home)

    try:
        if args.command == "watch":
            watch(settings, gh, args.interval, args.once)
            return 0
        outcome = run_issue(settings, gh, args.issue, escalate=not args.no_escalate)
        if outcome.status == "escalated":
            outcome = run_issue(settings, gh, args.issue)
        return 0 if outcome.status == "done" else 1
    except (ConfigError, NotImplementedError, GhError, Locked) as error:
        print(f"boss: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
