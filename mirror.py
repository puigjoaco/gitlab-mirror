#!/usr/bin/env python3
"""Mirror one GitHub user's branches and tags to their GitLab namespace."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB_USER = "puigjoaco"
GITLAB_USER = "puig.joaco"
# Large repositories (about 1 GB) need minutes for the first copy.
TRANSFER_TIMEOUT = 1500
REFSPECS = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")


class MirrorError(Exception):
    """Only fixed, safe-to-log messages belong in this exception."""


class APIError(MirrorError):
    def __init__(self, method, status):
        self.status = status
        super().__init__(f"API {method} failed (HTTP {status})")


class API:
    def __init__(self, base, token, github=False):
        self.base = base
        self.headers = ({"Authorization": "Bearer " + token,
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}
                        if github else {"PRIVATE-TOKEN": token})

    def request(self, method, path, data=None, missing_ok=False):
        body = None if data is None else json.dumps(data).encode()
        request = urllib.request.Request(
            self.base + path, data=body, method=method,
            headers={**self.headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            error.close()
            if missing_ok and error.code == 404:
                return None
            # Never echo response bodies, request headers, or URLs.
            raise APIError(method, error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise MirrorError("API request failed") from None

    def pages(self, path):
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            yield from batch
            if len(batch) < 100:
                return
            page += 1


def mask_token(token):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = token.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::add-mask::" + escaped, flush=True)


def save_gitlab_token(token, secrets_github_token):
    # Never pass the value in argv, log subprocess output, or write a token file.
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["gh", "secret", "set", "GITLAB_TOKEN", "-R", "puigjoaco/gitlab-mirror"],
                input=token, env={**os.environ, "GH_TOKEN": secrets_github_token},
                capture_output=True, text=True, timeout=20)
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise MirrorError(
        "GitLab token rotated but saving Actions secret GITLAB_TOKEN failed after 3 attempts; "
        "the old token is revoked. Create a new GitLab token and set GITLAB_TOKEN manually.")


def renew_gitlab_token(gitlab, token, secrets_github_token, today=None):
    today = today or datetime.now(timezone.utc).date()
    expires_at = gitlab.request("GET", "/personal_access_tokens/self").get("expires_at")
    if not expires_at:
        return token
    try:
        expiry = datetime.strptime(expires_at, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise MirrorError("GitLab token expiry date is invalid") from None
    if expiry > today + timedelta(days=30):
        return token
    if not secrets_github_token:
        raise MirrorError("SECRETS_GITHUB_TOKEN is required before rotating GITLAB_TOKEN")
    path = "/personal_access_tokens/self/rotate"
    try:
        rotated = gitlab.request("POST", path, {
            "expires_at": (today + timedelta(days=364)).isoformat()})
    except APIError as error:
        # Validation rejection did not rotate the token. Do not retry ambiguous
        # network/server failures: the old credential may already be revoked.
        if error.status not in (400, 422):
            raise
        rotated = gitlab.request("POST", path, {})
    new_token = rotated.get("token") if isinstance(rotated, dict) else None
    if not isinstance(new_token, str) or not new_token:
        raise MirrorError(
            "GitLab rotation returned no token; create a new GitLab token and set "
            "Actions secret GITLAB_TOKEN manually if the old token was revoked")
    mask_token(new_token)
    save_gitlab_token(new_token, secrets_github_token)
    return new_token


def owned_repositories(github):
    # /user/repos includes private repos, forks and archived repos.
    return [repo for repo in github.pages("/user/repos?affiliation=owner")
            if repo["owner"]["login"].lower() == GITHUB_USER.lower()]


def visibility(repo):
    return "private" if repo["private"] else "public"


def branch_and_tag_refs(output):
    refs = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        if ref.startswith(("refs/heads/", "refs/tags/")) and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


def unchanged(source_refs, target_refs):
    # Compare SHAs, not timestamps: detects rewrites, deletions and target drift.
    return source_refs == target_refs


def git(token, host, *args, cwd=None, timeout=120):
    username = "x-access-token" if host == "github.com" else "oauth2"
    authorization = base64.b64encode(f"{username}:{token}".encode()).decode()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
           "GIT_CONFIG_VALUE_0": "Authorization: Basic " + authorization}
    # Credentials live only in the child environment, never in URLs or files.
    try:
        result = subprocess.run(
            ["git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise MirrorError("git command failed or timed out") from None
    if result.returncode:
        raise MirrorError("git command failed")
    return result.stdout


def remote_refs(token, host, url):
    return branch_and_tag_refs(git(token, host, "ls-remote", "--refs", url,
                                   "refs/heads/*", "refs/tags/*"))


def unprotect_branches(gitlab, project):
    # Exact mirroring requires deletion as well as force-push permission.
    for rule in list(gitlab.pages(f"/projects/{project['id']}/protected_branches")):
        name = urllib.parse.quote(rule["name"], safe="")
        gitlab.request("DELETE", f"/projects/{project['id']}/protected_branches/{name}")


def mirror_repository(repo, github_token, gitlab_token, gitlab, namespace, dry_run=False):
    name = repo["name"]
    encoded_path = urllib.parse.quote(f"{GITLAB_USER}/{name}", safe="")
    project = gitlab.request("GET", f"/projects/{encoded_path}", missing_ok=True)
    created = project is None
    if created:
        if dry_run:
            return
        # GitLab validates the original path; never silently rename a repository.
        display_name = re.sub(r"[^A-Za-z0-9_. -]", "_", name).strip(" .-") or "Mirror"
        try:
            project = gitlab.request("POST", "/projects", {
                "namespace_id": namespace["id"], "path": name, "name": display_name,
                "visibility": visibility(repo), "initialize_with_readme": False})
        except MirrorError as error:
            raise MirrorError("project creation/path rejected: " + str(error)) from None
    if repo["private"] and project["visibility"] != "private" and not dry_run:
        gitlab.request("PUT", f"/projects/{project['id']}", {"visibility": "private"})

    source_url = f"https://github.com/{GITHUB_USER}/{urllib.parse.quote(name, safe='')}.git"
    target_url = f"https://gitlab.com/{GITLAB_USER}/{urllib.parse.quote(name, safe='')}.git"
    source_refs = remote_refs(github_token, "github.com", source_url)
    target_refs = remote_refs(gitlab_token, "gitlab.com", target_url)
    if unchanged(source_refs, target_refs) or dry_run:
        return

    with tempfile.TemporaryDirectory(prefix="gitlab-mirror-") as directory:
        bare = os.path.join(directory, "repository.git")
        git(github_token, "github.com", "clone", "--mirror", "--quiet", source_url, bare,
            timeout=TRANSFER_TIMEOUT)
        unprotect_branches(gitlab, project)
        # GitLab refuses deletion of its default branch. Create the new default
        # first, then select it before the final pruning push.
        default = repo.get("default_branch")
        if default and f"refs/heads/{default}" in source_refs and project.get("default_branch") != default:
            git(gitlab_token, "gitlab.com", "push", "--quiet", target_url,
                f"+refs/heads/{default}:refs/heads/{default}", cwd=bare, timeout=TRANSFER_TIMEOUT)
            gitlab.request("PUT", f"/projects/{project['id']}", {"default_branch": default})
        # The first push can automatically protect the default branch.
        unprotect_branches(gitlab, project)
        git(gitlab_token, "gitlab.com", "push", "--quiet", "--prune", target_url,
            *REFSPECS, cwd=bare, timeout=TRANSFER_TIMEOUT)
        unprotect_branches(gitlab, project)


def run_repositories(repositories, operation):
    def attempt(repo):
        try:
            operation(repo)
            return f"{repo['name']}: ok", False
        except MirrorError as error:
            return f"{repo['name']}: failed: {error}", True
        except Exception:
            # No tracebacks: third-party errors may contain credentials or content.
            return f"{repo['name']}: failed: unexpected error", True

    failures = 0
    # Bounded parallel comparisons keep idle runs short without a persistent cache.
    with ThreadPoolExecutor(max_workers=8) as executor:
        for message, failed in executor.map(attempt, repositories):
            print(message, flush=True)
            failures += failed
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list and compare; never write")
    args = parser.parse_args()
    github_token = os.environ.get("SOURCE_GITHUB_TOKEN", "")
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")
    secrets_github_token = os.environ.get("SECRETS_GITHUB_TOKEN", "")
    for token in (github_token, gitlab_token, secrets_github_token):
        if token:
            mask_token(token)
    if not github_token or not gitlab_token:
        print("failed: SOURCE_GITHUB_TOKEN and GITLAB_TOKEN are required")
        return 1
    github = API("https://api.github.com", github_token, github=True)
    gitlab = API("https://gitlab.com/api/v4", gitlab_token)
    try:
        if not args.dry_run:
            gitlab_token = renew_gitlab_token(gitlab, gitlab_token, secrets_github_token)
            gitlab = API("https://gitlab.com/api/v4", gitlab_token)
        namespace = gitlab.request("GET", "/namespaces/" + urllib.parse.quote(GITLAB_USER, safe=""))
        if namespace["full_path"] != GITLAB_USER or namespace["kind"] != "user":
            raise MirrorError("GitLab namespace does not match")
        repositories = owned_repositories(github)
        return run_repositories(repositories, lambda repo: mirror_repository(
            repo, github_token, gitlab_token, gitlab, namespace, args.dry_run))
    except MirrorError as error:
        print(f"failed: {error}")
        return 1
    except Exception:
        print("failed: unexpected error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
