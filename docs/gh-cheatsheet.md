# gh cheat sheet

Checked against gh 2.45. Every repo command takes `-R/--repo owner/name`; without it, gh
uses the repo of the current directory (or the one picked by `gh repo set-default`).

## Setup

```bash
gh auth login                    # sign in (choose SSH or HTTPS)
gh auth status                   # who am I, which scopes
gh repo set-default owner/name   # default repo for this clone
gh repo set-default --view       # show it
gh config set git_protocol ssh
```

## Repos

```bash
gh repo clone owner/name [dir]
gh repo create name --private --source . --push   # publish the current directory
gh repo view [owner/name] --web
gh repo list owner --limit 50
gh repo fork owner/name --clone
```

## Issues

```bash
gh issue list                                  # open issues
gh issue list --state all --limit 100
gh issue list --label boss:queued --label priority:p1    # labels are ANDed
gh issue list --assignee @me
gh issue list --search "crawler in:title sort:updated-desc"

gh issue view 12                               # add --comments for the thread, --web to open
gh issue create --title "..." --body "..." --label bug
gh issue create --title "..." --body-file notes.md
gh issue comment 12 --body "..."
gh issue edit 12 --title "..." --add-assignee @me
gh issue close 12 --comment "fixed in #34"
gh issue reopen 12
gh issue develop 12 --checkout                 # branch linked to the issue
```

## Labels

A label must exist in the repo before you can add it to an issue or PR.

```bash
gh label list
gh label create name --color fbca04 --description "..."    # --force updates an existing one
gh label edit name --name other --color d73a4a
gh label delete name --yes
gh label clone owner/source-repo                           # copy labels from another repo

gh issue edit 12 --add-label boss:queued,priority:p1       # comma-separated
gh issue edit 12 --remove-label boss:failed
gh pr edit 34 --add-label do:review
```

boss: `uv run boss labels owner/name` creates all of boss's labels in a repo.

## Pull requests

```bash
gh pr create --fill                            # title/body from commits
gh pr create --title "..." --body "..." --base main --draft
gh pr list                                     # --author @me, --label x, --state merged
gh pr status                                   # PRs involving you
gh pr view 34 --comments                       # --web to open
gh pr checkout 34
gh pr diff 34
gh pr checks 34 --watch                        # wait for CI
gh pr review 34 --approve                      # or --request-changes / --comment --body "..."
gh pr merge 34 --squash --delete-branch        # or --merge / --rebase; --auto waits for checks
gh pr ready 34                                 # draft -> ready for review
gh pr close 34
```

## Actions

```bash
gh run list --limit 10                         # --branch main, --workflow ci.yml
gh run view <run-id> --log-failed              # only the failing steps' logs
gh run watch <run-id> --exit-status
gh run rerun <run-id> --failed
gh workflow list
gh workflow run ci.yml --ref main              # trigger a workflow_dispatch
```

## Releases, secrets, gists

```bash
gh release create v0.1.0 --generate-notes [files...]
gh release list
gh release download v0.1.0
gh secret set NAME                             # prompts for the value; or --body "..."
gh secret list
gh gist create file.py --public
```

## Search

```bash
gh search issues "label:bug state:open" --owner bryanbrady
gh search prs --author @me --state open
gh search repos "topic:cli language:python" --sort stars
gh search code "issues_with_label" --repo bryanbrady/boss
```

## Scripting

```bash
gh issue list --json number,title,labels                      # --json with no fields lists them
gh issue list --json number,title --jq '.[] | "\(.number)\t\(.title)"'
gh pr view 34 --json state --jq .state
gh api repos/{owner}/{repo}/issues --paginate --jq '.[].title'   # {owner}/{repo} = current repo
gh api -X POST repos/owner/name/issues -f title="..." -f body="..."
gh api graphql -f query='{ viewer { login } }'
```

`--json` output is plain JSON when piped, unless the environment forces color:
`CLICOLOR_FORCE=1` or `GH_FORCE_TTY` make gh add ANSI codes that break `jq` and parsers.
Run `NO_COLOR=1 CLICOLOR_FORCE= gh ...` in scripts if your shell sets them.

## Handy

```bash
gh browse                     # open the repo in a browser; gh browse 12 for issue/PR 12
gh alias set iq 'issue list --label boss:queued'
gh <command> --help
```
