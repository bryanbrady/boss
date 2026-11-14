# boss: plan for the next version

Today boss watches one repo, runs one issue at a time in a locked-down container, and opens a PR
when the issue's checks pass. This plan takes it to several repos, parallel runs, roles that hand
work to each other, a dashboard, and a control repo where new work and new repos start.

GitHub stays the source of truth: issues, labels, PRs and comments hold the design and the
project history. Anything boss keeps locally is either a cache or a run's logs, and losing it
loses no state.

## Threat model

- The repos are private. Code comes from me or from Claude. Other agents may come later.
- The risks are the agent's own mistakes, instructions injected through content it reads
  (package READMEs, fetched pages, issue text), and malicious install scripts in dependencies.
- The box is the security boundary. Host-side checks (path rules, token scans) are a second
  layer behind it, not a replacement.

## Decisions to settle first

These shape everything after them and are expensive to change later.

1. **boss's GitHub identity.** Today `gh` runs with my token, so boss's comments, labels and PRs
   look like mine. boss gets a free machine account, invited as a collaborator on my personal
   repos, with a classic token (`repo` scope), because fine-grained tokens can't contribute as a
   repo collaborator. That token can't create a repo under my account, so Phase 4 needs its own
   answer: creating the repo with my token on the host, or transferring it from the machine account.
2. **Who can trigger work.** `do:*` labels are commands to boss. boss acts on `do:*` only when I
   applied the label, or when boss applied it as part of a chain. `do:create-repo` counts only
   when I applied it; boss never applies it, so applying it is the approval. boss checks who
   applied a label in the issue's timeline events. This only works once boss has its own identity.
   `boss:*` state labels are boss's own record and never trigger anything.
3. **Repo config is read from the default branch only.** Per-repo settings (`.boss.toml`: image,
   checks, extra hosts, role overrides) are read from the default branch, never from the run's
   branch. The host refuses to push an agent's diff that touches `.boss*`, `.github/` or role
   files.
4. **Loop limits.** Maximum attempts per issue, maximum chain depth (implement → review →
   implement ...), and a daily spend cap that stops dispatch. Hitting any of them sets
   `boss:blocked` with a comment saying which.
5. **One host.** The per-issue `flock` and the requeue-on-startup logic assume a single machine.
   This is stated, not solved.
6. **Conflicting PRs.** boss never force-pushes. A PR that conflicts with the default branch gets
   `boss:blocked`. A `do:rebase` role (merge the default branch in, fast-forward push) can come
   later.

## Label state machine

All transitions live in one module (`labels.py`), with tests. Every transition is safe to repeat,
because label edits aren't atomic.

A `do:*` label is a command. boss accepts it by setting `boss:queued`, and the `do:*` label stays
on while the work is queued or running, so a restart still knows the role. When the run ends, boss
removes the `do:*` label and sets the outcome. Adding a `do:*` label again is how to retry or give
an issue more work; boss clears the old outcome when it accepts the new command. An issue or PR
runs one command at a time.

| Label | On | Set by | Meaning |
|---|---|---|---|
| `boss:draft` | issue | anyone | written, not approved to run |
| `boss:queued` | issue, PR | boss | a `do:*` command was accepted and is waiting for a slot |
| `boss:running` | issue, PR | boss | a run holds it |
| `boss:done` | issue, PR | boss | checks passed, PR open / review posted |
| `boss:failed` | issue, PR | boss | the run failed; the comment says why |
| `boss:blocked` | issue, PR | boss | a limit or conflict needs me |
| `do:implement` `do:test` `do:debug` | issue | me | command: run this role |
| `do:review` | PR | me or boss | command: review this PR |
| `do:create-repo` | boss-hq issue | me only | command: create the repo the issue describes |
| `priority:p0` … `p3` | issue, PR | me | queue order |

## Phase 1: many repos, parallel runs

- One search per poll finds commands in every repo, on issues and PRs:
  `gh search issues --owner <me> --include-prs --state open --json repository,number,labels,createdAt,isPullRequest -- 'label:"do:implement","do:test","do:debug","do:review"'`.
  boss sets `boss:queued` on any result that has no state label yet.
  The search API allows about 30 requests a minute and lags up to a minute; a 60 s poll is fine.
- A repo is enrolled by `boss labels owner/repo`. No list of repos to maintain.
- A worker pool with a global cap and a per-repo cap. The per-issue `flock` still prevents
  double runs. Size the pool for Claude's rate limits on one token, not only for CPU.
- `watch` requeues stale `boss:running` issues across all enrolled repos on startup.
- Each run writes `run.json` from the start (see Phase 2), so the dashboard has history from
  day one.

Done when: two issues in two repos run at the same time, each opens its own PR, and a third
queued issue waits for a free slot.

## Phase 2: dashboard

- A small read-only page served on `127.0.0.1`, exposed on the tailnet with
  `tailscale serve --bg <port>`. The `Tailscale-User-Login` header identifies the viewer.
- Data comes from the run directories and a GitHub search. No other state.
- `run.json` per run: repo, issue or PR, role, attempt, chain parent, state, start, end, exit
  code, timed out, cost, PR or review link.
- Pages:
  - the queue across repos, in priority order
  - what is running, with elapsed time and a live tail of `agent.log`
  - per run: check results, `prompt.md`, and `gate.log` (every connection, allowed or denied)
  - recent outcomes, linked to the issue and PR
  - today's spend against the cap
- Everything rendered from logs or issue text is escaped: agents write that content.
- Buttons (requeue, cancel) come later, and only ever change labels.

Done when: from my phone on the tailnet I can see what is queued, what is running with its live
log, and why the last run failed, without opening a terminal.

## Phase 3: roles

Each role is a file, `roles/<role>.md`, with a per-repo override in `.boss/roles/<role>.md` (read
from the default branch). A role is a prompt, a sandbox profile and an output.

| Role | Trigger | Sandbox | Output | Host-side check before push |
|---|---|---|---|---|
| implement | issue `do:implement` | read-write clone | PR | token scan; no `.boss*` / `.github/` changes |
| test | issue `do:test` | read-write clone | PR | diff stays inside test paths |
| debug | issue `do:debug` | read-write clone | root-cause comment, then a PR if the fix is clear | as implement |
| review | PR `do:review` | read-only clone, no push | a review comment | token redaction on the comment |

- Chains are labels: when implement opens a PR, boss adds `do:review` to it. The review
  conversation stays on the PR. Chain depth and attempts count against the loop limits.
- The agent ends with a closing declaration: `done` with evidence, or `blocked` with a reason.
  A missing or invalid declaration is a failure, after one corrective turn.

Done when: an implement run opens a PR, a review run picks it up on its own and posts a review,
and a test run that edits a file outside the test paths is refused before the push.

## Phase 4: boss-hq, the control repo

- boss-hq holds cross-repo design, epics, and boss's own to-do list.
- `do:create-repo` issue: the body is the repo's design (name, purpose, stack, first tasks). The
  poll's search gains this label in this phase. A design still being written stays `boss:draft`.
  Once I apply `do:create-repo`, host-side boss (never an agent) runs `gh repo create`, seeds a
  README and CI, runs `boss labels` on it, opens the first issues there linked back to the
  boss-hq issue, and comments the new repo's URL on it.
- Designs that need discussion go in as PRs adding `designs/<name>.md`. Merging is approval. A
  `do:plan` role can later turn a merged design into linked issues.
- For now I add the Markdown myself. Creating drafts from a Claude conversation (a one-tool
  connector that can only open `boss:draft` issues in boss-hq) waits until this has run for a
  while.

Done when: a `do:create-repo` issue I labelled produces a private repo with labels, a README, CI
and its first issues, all linked to the boss-hq issue.

## Isolation work

The container and gate already hold up: on an internal network, external DNS doesn't resolve and
direct HTTPS is blocked; the agent runs with no capabilities, `no-new-privileges` and Docker's
seccomp filter. What's left, in order:

1. **Keep the Claude token out of the box.** The gate holds `CLAUDE_CODE_OAUTH_TOKEN` and is a
   reverse proxy for the Anthropic API. The agent gets `ANTHROPIC_BASE_URL=http://gate:8080` and a
   random key good only for that run; the gate checks it, adds the real credential and forwards
   the message endpoints over TLS. Direct CONNECT to `api.anthropic.com` is closed. Anything in
   the box can still spend through the gate during the run, but can't take the token away. If an
   OAuth token stops working this way, use an API key for boss. The same pattern holds each future
   agent's key.
2. **Drop `github.com` from the default allowlist.** It's there for uv's Python downloads. Bake
   the Python versions into the image instead, and add GitHub per repo only where needed.
3. **Harden `--docker` mode.** Inside a Sysbox box the agent is in the docker group (root in the
   box) on a writable filesystem. Consider wrapping the agent process in Landlock (for example
   with nono) inside the box.
4. **Method and path rules** in the gate for the few hosts where writes matter. Only worth it
   once the gate terminates TLS for item 1.

Not adopting OpenShell (it duplicates the container and gate, needs K3s and root, and is 0.1.x)
or nono as the main boundary (it shares the host filesystem view and PID namespace and has no
resource limits).

## Order

1. Decisions 1 and 2 (identity, trigger rule), then the label state machine.
2. Phase 1, writing `run.json` from the start.
3. Isolation items 1 and 2: parallel runs multiply the exposure of the token.
4. Phase 2.
5. Phase 3.
6. Phase 4.
7. Isolation items 3 and 4.

## Not in this plan

- Webhooks (polling with search is enough, and webhooks would need a public endpoint).
- Running on more than one host.
- Dashboard actions beyond changing labels.
- Retry policy beyond the attempt cap.
- Creating issues from a Claude conversation.
