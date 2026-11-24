import contextlib
import io
import json
import os
import subprocess
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, call, patch
import urllib.error

import mirror


def repository(name="sample", private=False, **fields):
    return {"name": name, "private": private, "owner": {"login": "puigjoaco"},
            "default_branch": "main", **fields}


class ListingTests(unittest.TestCase):
    def test_pagination_keeps_private_archived_forks_and_self(self):
        first = [repository(f"repo-{i}") for i in range(100)]
        last = [repository("private", private=True),
                repository("archived", archived=True), repository("fork", fork=True),
                repository("gitlab-mirror"), repository("foreign", owner={"login": "other"})]
        api = mirror.API("https://example.invalid", "placeholder")
        api.request = Mock(side_effect=[first, last])
        result = mirror.owned_repositories(api)
        self.assertEqual(len(result), 104)
        self.assertEqual([repo["name"] for repo in result[-4:]],
                         ["private", "archived", "fork", "gitlab-mirror"])
        self.assertEqual(api.request.call_args_list, [
            call("GET", "/user/repos?affiliation=owner&per_page=100&page=1"),
            call("GET", "/user/repos?affiliation=owner&per_page=100&page=2")])

    def test_empty_listing(self):
        api = mirror.API("https://example.invalid", "placeholder")
        api.request = Mock(return_value=[])
        self.assertEqual(mirror.owned_repositories(api), [])

    def test_visibility(self):
        self.assertEqual(mirror.visibility(repository(private=True)), "private")
        self.assertEqual(mirror.visibility(repository()), "public")


class RefTests(unittest.TestCase):
    def test_only_actual_branches_and_tags(self):
        output = ("a\tHEAD\nb\trefs/heads/main\nc\trefs/tags/v1\n"
                  "d\trefs/tags/v1^{}\ne\trefs/pull/1/head\nf\trefs/notes/test\n")
        self.assertEqual(mirror.branch_and_tag_refs(output),
                         {"refs/heads/main": "b", "refs/tags/v1": "c"})

    def test_skip_detects_rewrite_addition_deletion_and_drift(self):
        source = {"refs/heads/main": "a", "refs/tags/v1": "b"}
        self.assertTrue(mirror.unchanged(source, dict(source)))
        self.assertTrue(mirror.unchanged({}, {}))
        for target in ({}, {**source, "refs/heads/extra": "a"},
                       {**source, "refs/heads/main": "changed"},
                       {"refs/heads/main": "a"}):
            with self.subTest(target=target):
                self.assertFalse(mirror.unchanged(source, target))


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.project = {"id": 42, "visibility": "public", "default_branch": "main"}
        self.api.request.return_value = self.project
        self.api.pages.return_value = []
        self.refs = {"refs/heads/main": "a"}
        self.namespace = {"id": 7}
        self.git = patch("mirror.git", return_value="").start()
        self.remote = patch("mirror.remote_refs", side_effect=[self.refs, self.refs]).start()
        self.addCleanup(patch.stopall)

    def sync(self, repo=None, dry_run=False):
        mirror.mirror_repository(repo or repository(), "github-placeholder",
                                 "gitlab-placeholder", self.api, self.namespace, dry_run)

    def test_unchanged_skips_clone_and_push(self):
        self.sync()
        self.git.assert_not_called()
        self.assertEqual(self.api.request.call_count, 1)

    def test_private_is_restricted_even_when_refs_unchanged(self):
        self.sync(repository(private=True))
        self.api.request.assert_any_call("PUT", "/projects/42", {"visibility": "private"})
        self.git.assert_not_called()

    def test_visibility_update_failure_prevents_any_git_access(self):
        self.api.request.side_effect = [self.project, mirror.MirrorError("API PUT failed (HTTP 403)")]
        with self.assertRaises(mirror.MirrorError):
            self.sync(repository(private=True))
        self.remote.assert_not_called()

    def test_public_does_not_publish_existing_private_or_internal(self):
        for visibility in ("private", "internal"):
            self.api.request.reset_mock()
            self.project["visibility"] = visibility
            self.remote.side_effect = [self.refs, self.refs]
            self.sync()
            self.assertEqual(self.api.request.call_count, 1)

    def test_creation_preserves_path_and_visibility(self):
        self.project["visibility"] = "private"
        self.api.request.side_effect = [None, self.project]
        self.sync(repository("some.repo", private=True))
        self.assertEqual(self.api.request.call_args_list[1], call("POST", "/projects", {
            "namespace_id": 7, "path": "some.repo", "name": "some.repo",
            "visibility": "private", "initialize_with_readme": False}))

    def test_rejected_path_is_not_renamed(self):
        self.api.request.side_effect = [None, mirror.MirrorError("API POST failed (HTTP 400)")]
        with self.assertRaisesRegex(mirror.MirrorError, "project creation/path rejected"):
            self.sync(repository(".github"))
        self.assertEqual(self.api.request.call_args.args[2]["path"], ".github")
        self.remote.assert_not_called()

    def test_push_forces_and_prunes_only_heads_and_tags(self):
        self.remote.side_effect = [self.refs, {}]
        self.sync()
        calls = self.git.call_args_list
        self.assertEqual(calls[0].args[2:5], ("clone", "--mirror", "--quiet"))
        push = calls[-1].args
        self.assertEqual(push[2:5], ("push", "--quiet", "--prune"))
        self.assertEqual(push[-2:], mirror.REFSPECS)
        self.assertFalse(any("refs/pull" in str(item) for item in calls))

    def test_changed_default_created_and_selected_before_prune(self):
        self.project["default_branch"] = "old"
        self.remote.side_effect = [self.refs, {"refs/heads/old": "b"}]
        events = []
        self.git.side_effect = lambda *args, **kwargs: events.append(("git", args[2:])) or ""
        self.api.request.side_effect = lambda method, path, *args, **kwargs: (
            events.append((method, path, args)) or self.project)
        self.sync()
        create_index = next(i for i, event in enumerate(events)
                            if event[0] == "git" and "+refs/heads/main:refs/heads/main" in event[1])
        select_index = next(i for i, event in enumerate(events) if event[0] == "PUT")
        prune_index = next(i for i, event in enumerate(events)
                           if event[0] == "git" and "--prune" in event[1])
        self.assertLess(create_index, select_index)
        self.assertLess(select_index, prune_index)

    def test_branch_protection_names_are_encoded(self):
        self.api.pages.return_value = [{"name": "release/*"}]
        mirror.unprotect_branches(self.api, self.project)
        self.api.request.assert_called_with("DELETE", "/projects/42/protected_branches/release%2F%2A")

    def test_dry_run_does_not_create_or_mutate(self):
        self.api.request.return_value = None
        self.sync(dry_run=True)
        self.assertEqual(self.api.request.call_count, 1)
        self.remote.assert_not_called()
        self.api.request.reset_mock()
        self.api.request.return_value = self.project
        self.remote.side_effect = [self.refs, {}]
        self.sync(repository(private=True), dry_run=True)
        self.assertEqual(self.api.request.call_count, 1)
        self.git.assert_not_called()
        self.api.pages.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def test_failure_aggregation_continues_without_leaking_errors(self):
        seen = []

        def operation(repo):
            seen.append(repo["name"])
            if repo["name"] == "bad":
                raise mirror.MirrorError("git command failed")
            if repo["name"] == "unknown":
                raise RuntimeError("sensitive server response")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = mirror.run_repositories(
                [repository("bad"), repository("good"), repository("unknown")], operation)
        self.assertEqual(status, 1)
        self.assertCountEqual(seen, ["bad", "good", "unknown"])
        self.assertEqual(output.getvalue().splitlines(), [
            "bad: failed: git command failed", "good: ok", "unknown: failed: unexpected error"])

    def test_success_status(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mirror.run_repositories([repository()], lambda repo: None), 0)
            self.assertEqual(mirror.run_repositories([], lambda repo: None), 0)

    @patch("mirror.subprocess.run")
    def test_git_credentials_only_in_environment_and_output_suppressed(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "private filenames", "secret server error")
        with self.assertRaisesRegex(mirror.MirrorError, "^git command failed$"):
            mirror.git("placeholder", "github.com", "ls-remote", "https://github.com/example/repo.git")
        command = run.call_args.args[0]
        self.assertNotIn("placeholder", " ".join(command))
        self.assertIn("Authorization: Basic ", run.call_args.kwargs["env"]["GIT_CONFIG_VALUE_0"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    @patch("mirror.urllib.request.urlopen")
    def test_http_error_does_not_echo_response(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 403, "sensitive body", {}, None)
        with self.assertRaisesRegex(mirror.MirrorError, r"^API GET failed \(HTTP 403\)$"):
            mirror.API("https://example.invalid", "placeholder").request("GET", "/test")


class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 11, 24)
        self.api = mirror.API("https://gitlab.com/api/v4", "old-placeholder")
        self.urlopen = patch("mirror.urllib.request.urlopen").start()
        self.run = patch("mirror.subprocess.run").start()
        self.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.sleep = patch("mirror.time.sleep").start()
        self.mask = patch("mirror.mask_token").start()
        self.addCleanup(patch.stopall)

    def response(self, value):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(value).encode()
        return response

    def responses(self, expiry, *extra):
        self.urlopen.side_effect = [self.response({"expires_at": expiry}), *extra]

    def renew(self, credential="github-placeholder"):
        return mirror.renew_gitlab_token(self.api, "old-placeholder", credential, self.today)

    def test_far_expiry_and_missing_date_do_not_rotate(self):
        for expiry in ((self.today + timedelta(days=31)).isoformat(), None):
            with self.subTest(expiry=expiry):
                self.urlopen.reset_mock()
                self.responses(expiry)
                self.assertEqual(self.renew(credential=""), "old-placeholder")
                self.assertEqual(self.urlopen.call_count, 1)
                request = self.urlopen.call_args.args[0]
                self.assertEqual(request.full_url,
                                 "https://gitlab.com/api/v4/personal_access_tokens/self")
                self.assertEqual(request.get_header("Private-token"), "old-placeholder")
                self.run.assert_not_called()

    def test_near_expiry_rotates_masks_and_saves_using_stdin(self):
        for days in (30, 1, 0):
            with self.subTest(days=days):
                self.responses((self.today + timedelta(days=days)).isoformat(),
                               self.response({"token": "new-placeholder"}))
                events = []
                self.mask.side_effect = lambda token: events.append("mask")
                self.run.side_effect = lambda *args, **kwargs: (
                    events.append("save") or subprocess.CompletedProcess([], 0, "", ""))
                self.assertEqual(self.renew(), "new-placeholder")
                self.assertEqual(events, ["mask", "save"])
                self.mask.assert_called_with("new-placeholder")
                request = self.urlopen.call_args.args[0]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.full_url,
                                 "https://gitlab.com/api/v4/personal_access_tokens/self/rotate")
                self.assertEqual(json.loads(request.data), {
                    "expires_at": (self.today + timedelta(days=364)).isoformat()})
                self.assertEqual(self.run.call_args.args[0], [
                    "gh", "secret", "set", "GITLAB_TOKEN", "-R", "puigjoaco/gitlab-mirror"])
                kwargs = self.run.call_args.kwargs
                self.assertEqual(kwargs["input"], "new-placeholder")
                self.assertEqual(kwargs["env"]["GH_TOKEN"], "github-placeholder")
                self.assertTrue(kwargs["capture_output"])
                self.assertTrue(kwargs["text"])

    def test_date_validation_rejection_retries_once_with_default(self):
        for status in (400, 422):
            with self.subTest(status=status):
                self.urlopen.reset_mock()
                self.responses(self.today.isoformat(), urllib.error.HTTPError(
                    "https://gitlab.com", status, "suppressed", {}, None),
                    self.response({"token": "new-placeholder"}))
                self.assertEqual(self.renew(), "new-placeholder")
                self.assertEqual(self.urlopen.call_count, 3)
                self.assertEqual(json.loads(self.urlopen.call_args.args[0].data), {})

    def test_ambiguous_rotation_failure_is_not_retried(self):
        for error in (urllib.error.URLError("suppressed"), urllib.error.HTTPError(
                "https://gitlab.com", 500, "suppressed", {}, None)):
            with self.subTest(error=type(error).__name__):
                self.urlopen.reset_mock()
                self.responses(self.today.isoformat(), error)
                with self.assertRaises(mirror.MirrorError):
                    self.renew()
                self.assertEqual(self.urlopen.call_count, 2)
                self.run.assert_not_called()

    def test_missing_saving_credential_prevents_rotation(self):
        self.responses(self.today.isoformat())
        with self.assertRaisesRegex(mirror.MirrorError, "required before rotating"):
            self.renew(credential="")
        self.assertEqual(self.urlopen.call_count, 1)
        self.run.assert_not_called()

    def test_secret_save_retries_with_backoff(self):
        self.responses(self.today.isoformat(), self.response({"token": "new-placeholder"}))
        self.run.side_effect = [subprocess.CompletedProcess([], 1, "suppressed", "suppressed"),
                                subprocess.TimeoutExpired("gh", 20),
                                subprocess.CompletedProcess([], 0, "", "")]
        self.assertEqual(self.renew(), "new-placeholder")
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_secret_save_exhaustion_has_safe_recovery_message(self):
        self.responses(self.today.isoformat(), self.response({"token": "new-placeholder"}))
        self.run.return_value = subprocess.CompletedProcess(
            [], 1, "new-placeholder", "github-placeholder")
        with self.assertRaises(mirror.MirrorError) as caught:
            self.renew()
        message = str(caught.exception)
        self.assertIn("old token is revoked", message)
        self.assertIn("set GITLAB_TOKEN manually", message)
        self.assertNotIn("placeholder", message)
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])


class RenewalIntegrationTests(unittest.TestCase):
    @patch.dict(os.environ, {"SOURCE_GITHUB_TOKEN": "github-placeholder",
                             "GITLAB_TOKEN": "old-placeholder",
                             "SECRETS_GITHUB_TOKEN": "saving-placeholder"}, clear=True)
    @patch("mirror.keep_schedule_alive")
    @patch("mirror.run_repositories")
    @patch("mirror.mirror_repository")
    @patch("mirror.owned_repositories", return_value=[repository()])
    @patch("mirror.API")
    @patch("mirror.renew_gitlab_token", return_value="new-placeholder")
    def test_same_run_uses_new_token_and_api(self, renew, api, owned, sync, run, keepalive):
        namespace = {"id": 7, "full_path": mirror.GITLAB_USER, "kind": "user"}
        github, old_gitlab, new_gitlab = Mock(), Mock(), Mock()
        api.side_effect = [github, old_gitlab, new_gitlab]
        new_gitlab.request.return_value = namespace
        sync.return_value = None
        run.side_effect = lambda repos, operation: operation(repos[0]) or 0
        with patch("sys.argv", ["mirror.py"]):
            self.assertEqual(mirror.main(), 0)
        renew.assert_called_once_with(old_gitlab, "old-placeholder", "saving-placeholder")
        self.assertEqual(api.call_args_list[-1],
                         call("https://gitlab.com/api/v4", "new-placeholder"))
        self.assertEqual(sync.call_args.args[2], "new-placeholder")
        self.assertIs(sync.call_args.args[3], new_gitlab)
        keepalive.assert_called_once_with("saving-placeholder")

    @patch.dict(os.environ, {"SOURCE_GITHUB_TOKEN": "github-placeholder",
                             "GITLAB_TOKEN": "old-placeholder"}, clear=True)
    @patch("mirror.run_repositories", return_value=0)
    @patch("mirror.owned_repositories", return_value=[])
    @patch("mirror.API")
    @patch("mirror.renew_gitlab_token")
    @patch("mirror.keep_schedule_alive")
    def test_dry_run_skips_rotation(self, keepalive, renew, api, owned, run):
        api.return_value.request.return_value = {
            "full_path": mirror.GITLAB_USER, "kind": "user"}
        with patch("sys.argv", ["mirror.py", "--dry-run"]):
            self.assertEqual(mirror.main(), 0)
        renew.assert_not_called()
        keepalive.assert_not_called()

    @patch.dict(os.environ, {"SOURCE_GITHUB_TOKEN": "github-placeholder",
                             "GITLAB_TOKEN": "old-placeholder"}, clear=True)
    @patch("mirror.run_repositories")
    @patch("mirror.API")
    @patch("mirror.renew_gitlab_token", side_effect=mirror.MirrorError("secret save failed"))
    def test_rotation_failure_stops_before_mirroring(self, renew, api, run):
        output = io.StringIO()
        with patch("sys.argv", ["mirror.py"]), contextlib.redirect_stdout(output):
            self.assertEqual(mirror.main(), 1)
        run.assert_not_called()
        api.return_value.request.assert_not_called()
        self.assertEqual(output.getvalue(), "failed: secret save failed\n")


class KeepaliveTests(unittest.TestCase):
    @patch("mirror.API")
    def test_reenables_this_workflow(self, api):
        mirror.keep_schedule_alive("saving-placeholder")
        api.assert_called_once_with("https://api.github.com", "saving-placeholder", github=True)
        api.return_value.request.assert_called_once_with(
            "PUT", "/repos/puigjoaco/gitlab-mirror/actions/workflows/mirror.yml/enable")

    @patch("mirror.API")
    def test_failure_is_logged_and_not_fatal(self, api):
        api.return_value.request.side_effect = mirror.APIError("PUT", 403)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            mirror.keep_schedule_alive("saving-placeholder")
        self.assertEqual(output.getvalue(), "keepalive: failed: API PUT failed (HTTP 403)\n")

    @patch("mirror.API")
    def test_without_token_does_nothing(self, api):
        mirror.keep_schedule_alive("")
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
