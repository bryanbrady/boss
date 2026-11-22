# boss

boss watches a GitHub repo for labelled issues and hands each one to Claude Code, running
in a locked-down container, and opens a PR when the issue's checks pass. If the agent
fails, the issue is left for you.

The poller is plain Python. It never calls a model itself.

## Labels

Labels are boss's only state, so you drive it from GitHub.

A `do:*` label is a command. Put one on an issue to have boss work on it. boss takes
commands only from the repo's owner (and from itself): it checks who applied the label,
and if it was anyone else, it removes the label and says why on the issue.

| Label | Set by | Meaning |
|---|---|---|
| `do:implement` / `do:test` / `do:debug` | you | Command: do this kind of work |
| `do:review` | you | Not supported yet |
| `priority:p0` … `priority:p3` | you | Queue order. Default: p2. Ties go to the oldest issue |
| `boss:draft` | anyone | Written, not approved to run. A command approves it |
| `boss:queued` | boss | The command was accepted and is waiting for a slot |
| `boss:running` | boss | A run holds the issue |
| `boss:done` / `boss:failed` | boss | How the run ended; a comment says why it failed |
| `boss:blocked` | boss | A limit or conflict needs you (nothing sets it yet) |

The `do:*` label stays on while the issue is queued and running. When the run ends, boss
removes it and sets `boss:done` or `boss:failed`. To retry, or to give an issue more work,
add the `do:*` label again. `boss:*` labels never trigger anything, so adding
`boss:queued` yourself does nothing.

**Done means the checks pass.** boss reads the section whose heading starts with
"Tests". In that section, each list item that starts with a code span is a check, and so
is each fenced block. They run in a fresh sandbox after the agent finishes, so they must
be runnable shell commands. The [task-to-github-issue](skills/task-to-github-issue/SKILL.md)
skill writes issues in this shape:

```markdown
## Tests required for completion
- [ ] `uv run pytest tests/test_cli.py`
- [ ] `uv run ruff check .`
```

If an issue has no checks, boss opens the PR anyway and marks it unverified.

Comments on the issue go into the agent's prompt only if they come from the repo's owner,
members or collaborators. boss's own failure comments count as well, so a re-queued issue
carries forward why the last attempt failed.

## Setup

```bash
docker build -t boss-agent images/agent    # the sandbox image
uv sync

export CLAUDE_CODE_OAUTH_TOKEN=<from `claude setup-token`>
export GH_BOSS_TOKEN=<boss's GitHub account's token>

uv run boss labels owner/repo              # create boss's labels in the repo
```

boss works on GitHub as its own account, so you can tell its comments, labels, commits and
PRs from yours. Give it a machine account, invite that account as a collaborator on each repo
boss works in, and give boss a classic token for it with the `repo` scope (a fine-grained
token can't act as a collaborator on another user's repo). boss uses only that token: it
ignores your `gh` login and config and your git credentials, and it authors commits as the
account's noreply address.

## Use

```bash
uv run boss run owner/repo 42      # run issue 42 now
uv run boss watch owner/repo       # poll every 60s: accept do:* commands, run them in priority order
```

You drive boss from GitHub with labels. [docs/gh.md](docs/gh.md) has the `gh` commands for
giving boss work, watching the queue and reviewing its PRs.

Each run's files are kept in `~/.local/state/boss/runs/<repo>-<issue>-<time>/`
(`BOSS_HOME` moves them):
- `logs/agent.log`: the agent's output
- `logs/check-N.log`: each check's output
- `logs/agent-dockerd.log`, `logs/checks-dockerd.log`: the boxes' Docker daemons (`--docker`)
- `logs/gate.log`: every network connection the run attempted
- `boss/prompt.md`: exactly what the agent was told

## Branches and locking

Each issue has one branch, `boss/issue-N`. If it's already on GitHub, from an earlier run
or from your own commits, the next run checks it out and builds on it. The agent's
prompt says so.

A run clones only the default branch, then fetches `boss/issue-N` if it exists. The agent
never sees other branches or their open PRs. To have boss build on other work, push it to
`boss/issue-N`, e.g. `git push origin my-branch:boss/issue-N`.

**boss never force-pushes.** Every push is fast-forward only, and host-side git refuses
any force flag. If the branch moves on GitHub during a run, the push is rejected. The
issue is then marked `boss:failed` with a comment, and nothing is overwritten. Add the
`do:*` label again to build on the new commits.

Only one boss process can run an issue at a time. Runs take a per-issue lock (an `flock`
under `BOSS_HOME/locks/`), so `watch` and a manual `boss run` can't both pick up the same
issue: `watch` skips an issue another process holds. The kernel releases the lock when a
process exits, so a crash never leaves a stale lock. On startup, `watch` requeues
`boss:running` issues that no live process holds. The lock covers one machine only.

## The sandbox

Agents and checks run in containers that:
- run as your user, with all capabilities dropped and `no-new-privileges`
- have a read-only root filesystem and a tmpfs `/tmp`
- have memory, CPU and process limits, and are killed at a timeout
- mount only the run's clone and the run's read-only input directory: no home directory,
  SSH keys, Docker socket or GitHub token
- sit on a per-run internal network with no route out

Each run's only way out is a gate container, which proxies HTTPS only to an allowlist: the
package registries (plus GitHub, for uv's Python downloads). Use `--allow host` to add more.

The Claude token stays out of the agent's container. The gate holds it, and the agent gets a
random key that's only good for that run and only to the gate. The gate swaps the key for the
token and forwards the agent's message requests to `api.anthropic.com`, and nothing else
there. Checks never get the key.

No container ever gets the GitHub token. boss refuses to push a diff that contains either
token, and redacts both from anything it posts.

### Nested Docker

Some projects can only be checked with Docker: `docker compose up`, then tests against the
running stack. The sandbox above has no Docker, so with `--docker` (or `BOSS_DOCKER=1`)
boss runs the agent in a [Sysbox](https://github.com/nestybox/sysbox) container instead:
a box with its own Docker daemon. When the agent finishes, the checks run in order in a
second, fresh box, so one check can start the stack and the next can `curl localhost`.

- Root in a box is an unprivileged user on the host. The box can't see the host's Docker
  daemon or containers, and it sits behind the same gate. Docker Hub is added to the
  allowlist.
- Inside the box, the daemon needs root and a writable filesystem, so the read-only and
  no-capabilities hardening doesn't apply there. Memory, CPU and process limits do.
- Boxes get no host mounts. boss copies the work tree in and out as a tar stream, so
  everything that comes back is owned by you, with setuid bits stripped. (A bind mount
  would let root in the box write files owned by real root, setuid included.)
- A run's two boxes share one volume for images, so the checks reuse what the agent
  pulled or built. The volume is deleted with the run.
- Containers inside a box reach the internet only over HTTPS to allowlisted hosts. A
  Dockerfile that runs `apt-get` against plain-HTTP mirrors won't build.
- A box shares the host's kernel, so it's weaker isolation than a VM.

Install Sysbox once (it needs the real Docker Engine, not Docker Desktop):

```bash
sudo apt-get install ./sysbox-ce_<version>.linux_amd64.deb   # from github.com/nestybox/sysbox/releases
uv run boss watch owner/repo --docker
```

The real git directory stays on the host, outside the clone. Host-side git always names
it explicitly and runs with hooks and fsmonitor disabled, so nothing an agent writes into
the clone gets run on your machine. The agent works in its own throwaway copy of the
history.

## Development

```bash
uv run pytest              # unit tests
uv run pytest --docker     # plus end-to-end runs in real containers (needs boss-agent built;
                           # the nested-Docker ones also need Sysbox)
uv run ruff check . && uv run ruff format --check .
```

## Not built yet

- `do:review`: review a PR's diff and post the review as a comment
- triage: Claude reads the open issues and sets `priority:*`
- a separate harness to benchmark candidate models on a task suite (not a boss concern)
- running more than one issue at a time
