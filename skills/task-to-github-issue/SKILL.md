# Task → GitHub Issue

Creates one task in the session task list and one GitHub issue whose body is that task. The tests listed are the definition of done: shell commands that must all exit 0. boss runs them to decide whether an agent finished the task, so each one must work unattended (see [Writing tests](#writing-tests)).

## Inputs

| Field | Required | Default / inference |
|---|---|---|
| `repo` | yes | `owner/name` from the user; else infer from `git remote get-url origin` in the working dir |
| `title` | yes | Short imperative summary derived from the description if not given |
| `description` | yes | What the task is, scope, context, links |
| `tests` | yes | Shell commands that exit 0 when the task is done, run from the repo root |
| `labels` | no | none |
| `assignee`, `milestone` | no | none |

Parse these from free-form input (e.g. `/task-to-github-issue repo=acme/fw labels=bug,ci "Fix CAN timeout on boot" tests: cargo test can::timeout`).

Ask only if `repo`, the task itself, or `tests` cannot be determined. Do not invent tests; if the user gives none, ask once. If the user is not present, create the issue with the `## Tests required for completion` section reading `- [ ] TBD — acceptance tests not specified` and say so. boss treats an issue like that as unverified.

## Writing tests

boss reads the section whose heading starts with "Tests". Each list item there that starts with a code span is one test, and so is each fenced block. Each test runs as `bash -lc '<test>'` from the repository root in a fresh container, and passes if it exits 0. So each test must be:

- **A command, not a description.** Turn a test name into the command that runs it: `can::timeout` becomes `cargo test can::timeout`. If the repo doesn't make the test runner clear (check its README, `Makefile`, `pyproject.toml`, `package.json` and CI config), ask.
- **Self-contained.** Each test runs in its own container. Files it writes in the repo carry over to the next test. Nothing else does: no environment variables, no `/tmp`, no running services. Include any setup the test needs: `uv run pytest ...` rather than `pytest ...`, `npm ci && npm test` rather than `npm test`.
- **Runnable offline and without credentials.** The container has the checkout, but no GitHub access (no `gh`, no `git push`), no secrets, no display, and HTTPS only to package registries.
- **Specific to the task.** Name the test file, test function or behavior that proves this task is done. Add the full suite as a separate test when regressions matter.
- **Non-interactive and bounded.** No prompts or watch modes (`--watch`, dev servers). A test is killed after 15 minutes.

Checks that can't be a command ("the output looks right", "the UI matches the mock") go in a `## Manual verification` section, never in the tests section. Only the tests section's heading may start with "Tests".

## Steps

1. **Resolve the repo.** Use the given `owner/name`, or infer from the git remote. If the session needs repo access, attach it with push access before calling GitHub.

2. **Pick the GitHub backend**, in order:
   - A connected GitHub MCP tool that creates issues (load via ToolSearch, e.g. query `github issue create`).
   - `gh` CLI: check `gh auth status`. If unauthenticated and no MCP tool exists, stop and tell the user which one to set up.

3. **Validate labels** (if any). List existing labels (`gh label list --repo <repo> --limit 200 --json name` or the MCP equivalent). Match case-insensitively. For labels that do not exist: do not create them silently. Omit them from the issue and report them, unless the user explicitly said to create missing labels (`gh label create <name> --repo <repo>`).

4. **Create the task** with TaskCreate:
   - subject: `title`
   - description: the description plus the test list
   - metadata: `{ "repo": "<repo>" }`

5. **Write the issue body** to a temp file (avoids shell quoting problems):

   ```markdown
   ## Task
   <description>

   ## Tests required for completion
   - [ ] `<shell command 1>`
   - [ ] `<shell command 2>`

   ## Manual verification
   - <only if some checks can't be commands; omit the section otherwise>

   ## Definition of done
   Every command under "Tests required for completion" exits 0 when run from the repository root.
   ```

   Put each test command in a code span at the start of its list item, verbatim. Use a fenced block for a multi-line test; boss runs the whole block as one script.

6. **Create the issue.**

   ```bash
   gh issue create --repo <repo> --title "<title>" --body-file /tmp/issue_body.md \
     [--label <l1> --label <l2>] [--assignee <user>] [--milestone "<m>"]
   ```

   Or call the MCP create-issue tool with the same fields.

7. **Link back.** TaskUpdate the task: append the issue URL to its description and set metadata `{ "issue_url": "<url>", "issue_number": <n> }`.

8. **Report** in one or two lines: issue URL, labels applied, labels skipped (if any).

## Failure handling

- 404 / permission error: report the exact error. Private repos return 404 without access, so do not conclude the repo does not exist.
- Issue creation fails after the task was created: keep the task, add `GitHub issue NOT created: <reason>` to its description, and tell the user.
- Never retry `gh issue create` blindly after an ambiguous failure (timeout); first check `gh issue list --repo <repo> --search "<title> in:title" --state open` to avoid duplicates.

## Batch mode

If given several tasks, validate labels once, then repeat steps 4–7 per task and report a table of title → issue URL.
