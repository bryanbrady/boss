# Driving boss with gh

Labels are boss's only state, so `gh` is its console: you give commands, watch the queue and
review its PRs from the command line. These commands run as you, with your own `gh` login.
boss itself uses only `GH_BOSS_TOKEN` and never reads your login.

Checked against gh 2.45. Examples use `owner/repo` and issue 42. `<boss>` is the login of
boss's GitHub account.

## Enroll a repo

```bash
uv run boss labels owner/repo                       # create or update boss's labels

# invite boss's account, then accept the invitation as boss
gh api -X PUT repos/owner/repo/collaborators/<boss> -f permission=push
GH_TOKEN=$GH_BOSS_TOKEN gh api user/repository_invitations --jq '.[] | [.id, .repository.full_name] | @tsv'
GH_TOKEN=$GH_BOSS_TOKEN gh api -X PATCH user/repository_invitations/<id>

GH_TOKEN=$GH_BOSS_TOKEN gh api user --jq .login     # which account boss is
```

## Write an issue

The [task-to-github-issue](../skills/task-to-github-issue/SKILL.md) skill writes issues in the
shape boss reads, with a "Tests" section of checks. To write one by hand:

```bash
gh issue create -R owner/repo --title "..." --body-file issue.md --label boss:draft
```

`boss:draft` means written but not approved. It does nothing on its own.

## Give a command

A `do:*` label is the command, and boss takes it only from the repo's owner.

```bash
gh issue edit 42 -R owner/repo --add-label do:implement                # or do:test, do:debug
gh issue edit 42 -R owner/repo --add-label do:implement,priority:p1    # and jump the queue
```

To retry a failed issue, or give a done one more work, add the `do:*` label again. boss clears
the old `boss:done` or `boss:failed` when it accepts the command.

To withdraw a command before it runs, remove both labels:

```bash
gh issue edit 42 -R owner/repo --remove-label do:implement,boss:queued
```

Adding `boss:queued` yourself does nothing, and labels can't stop a run that has started.

## Watch the queue

```bash
gh issue list -R owner/repo --label boss:queued     # waiting
gh issue list -R owner/repo --label boss:running    # being worked on
gh issue list -R owner/repo --label boss:failed     # left for you

# the queue with each issue's priority (boss runs p0 first, then the oldest issue)
gh issue list -R owner/repo --label boss:queued --json number,title,labels --jq '.[] |
  [.number, ([.labels[].name | select(startswith("priority:"))][0] // "priority:p2"), .title] | @tsv'

# every repo you own at once (search lags edits by up to a minute)
gh search issues --owner <you> --state open -- 'label:"boss:queued","boss:running"'
```

## See why a run failed

boss comments the reason and the end of the log on the issue. The full logs are on the host,
in `~/.local/state/boss/runs/`.

```bash
gh issue view 42 -R owner/repo --comments
```

If boss ignored a command, its comment says who applied the label. boss reads that from the
issue's events, the same way as this:

```bash
gh api --paginate repos/owner/repo/issues/42/events \
  --jq '.[] | select(.event == "labeled") | [.created_at, .label.name, .actor.login] | @tsv'
```

## Review boss's PRs

Each issue has one branch, `boss/issue-42`. boss opens the PR from its own account and asks
you to review it.

```bash
gh pr list -R owner/repo --search "review-requested:@me"
gh pr view boss/issue-42 -R owner/repo              # the PR body lists the checks that passed
gh pr diff boss/issue-42 -R owner/repo
gh pr checkout boss/issue-42 -R owner/repo
gh pr merge boss/issue-42 -R owner/repo --squash --delete-branch
```

If you keep the branch, the next command on the issue builds on it. To have boss build on
your own work, push it to the issue's branch:

```bash
git push origin my-branch:boss/issue-42
```

## Scripting

`--json` output is plain JSON when piped, unless the environment forces color:
`CLICOLOR_FORCE=1` or `GH_FORCE_TTY` make gh add ANSI codes that break `jq` and parsers.
boss strips these variables (and `FORCE_COLOR`) from every gh call. In your own scripts, run
`NO_COLOR=1 CLICOLOR_FORCE= gh ...` if your shell sets them.
