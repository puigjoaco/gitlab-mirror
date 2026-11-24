# GitHub → GitLab mirror

Copies every repository owned by `puigjoaco` (including private, archived and
forked repositories, and this repository) to `puig.joaco` on GitLab.com. Only
branches and tags are copied; LFS, issues and other GitHub metadata are excluded.
GitHub is authoritative: force pushes and branch/tag deletions also apply to
GitLab. Use these GitLab projects as replicas: their branch protection rules
are removed when syncing changes so force pushes and deletions work. The GitLab
default branch follows GitHub when branches are synced.

Add these repository **Actions secrets**:

- `SOURCE_GITHUB_TOKEN`: GitHub fine-grained PAT for `puigjoaco`, all repositories,
  with **Contents: read-only** (Metadata read access is included). It must cover
  private repositories and any newly created repositories.
- `GITLAB_TOKEN`: GitLab personal access token for `puig.joaco`, with **api** and
  **write_repository** scopes. It is renewed automatically within 30 days of expiry.
- `SECRETS_GITHUB_TOKEN`: GitHub fine-grained PAT for `puigjoaco`, restricted to
  **this repository only**, with repository **Secrets: read and write** and
  **Actions: read and write** permissions. This allows renewal to replace the
  `GITLAB_TOKEN` Actions secret, and each run to re-enable this workflow so GitHub
  never disables the schedule for inactivity.

Renew both GitHub PAT secrets manually before their expiry. GitLab.com PATs last
at most about one year: each normal run checks the GitLab token's expiry and, when
due, rotates it with an expiry 364 days from the current UTC date. If GitLab rejects
that date, rotation retries once using GitLab's default expiry. A token with no
expiry date is left alone. The new value is masked immediately, saved with `gh`
via stdin (with up to three attempts and backoff), and used for that same run.
Workflow concurrency prevents overlapping runs from rotating simultaneously.

Rotation immediately revokes the old GitLab token. If saving the new Actions
secret fails, the workflow fails and stops before mirroring. To recover, create a
new GitLab PAT with **api** and **write_repository** scopes, replace the repository's
`GITLAB_TOKEN` Actions secret in GitHub Settings → Secrets and variables → Actions,
and fix/renew `SECRETS_GITHUB_TOKEN` if needed before rerunning. Do not paste tokens
into logs, commands, or repository files.

GitLab copies have matching visibility on creation. Private GitHub repositories
always make the GitLab copy private; existing private/internal copies are never
made public. Names remain unchanged; paths GitLab rejects are reported as failures.

The workflow runs on pushes to `main`, manually, and approximately every five
minutes. GitHub may delay scheduled runs; schedules require the workflow on the
default branch. GitHub disables schedules after 60 days without repository
activity; each run re-enables the workflow through the API to prevent that
(a failure is logged as `keepalive: failed` and does not fail the run).
Runs cannot overlap and have a five-minute limit. Large initial copies may need
longer; completed repositories are skipped on later runs.

Run manually:

```sh
gh workflow run mirror.yml -R puigjoaco/gitlab-mirror
```

Unchanged branch/tag SHAs skip cloning and pushing. Comparisons run concurrently;
no persistent cache is needed. Failures do not stop other repositories, but make
the workflow fail (enable GitHub Actions failure notifications for email).
Repository names and short results are logged; commit messages, file names and
Git command output are suppressed.

Local checks: `python3 -m unittest`. With both secrets already in the environment,
`python3 mirror.py --dry-run` lists and compares without creating projects,
changing visibility/protection, or pushing. It still needs the tokens to inspect
private repositories. Dry runs also skip token rotation and secret updates.
