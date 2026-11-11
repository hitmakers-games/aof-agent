"""Regression checks for credential boundaries and preserving existing work."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bootstrap
from credential import repository
from github_app import AccessError, GitHubApp


def metadata(name="aof-back"):
    return {"name": name, "owner": {"login": "hitmakers-games"},
            "clone_url": "https://github.com/hitmakers-games/" + name + ".git", "default_branch": "master"}


class RepositoryTests(unittest.TestCase):
    def test_credentials_only_for_corporate_https(self):
        self.assertEqual(repository({"protocol": "https", "host": "github.com", "path": "hitmakers-games/aof-back.git"}), "aof-back")
        for fields in [
            {"protocol": "http", "host": "github.com", "path": "hitmakers-games/aof-back.git"},
            {"protocol": "https", "host": "attacker.test", "path": "hitmakers-games/aof-back.git"},
            {"protocol": "https", "host": "github.com", "path": "another-org/aof-back.git"},
            {"protocol": "https", "host": "github.com", "path": "hitmakers-games/../aof-back.git"},
        ]:
            with self.subTest(fields=fields), self.assertRaises(AccessError):
                repository(fields)

    def test_metadata_cannot_escape_repository_directory(self):
        for name in ("../other", "/tmp/other", "aof-back/other", ".git"):
            with self.subTest(name=name), self.assertRaises(AccessError):
                bootstrap.validate(metadata(name))

    def test_metadata_cannot_redirect_credentials(self):
        item = metadata()
        item["clone_url"] = "https://attacker.test/aof-back.git"
        with self.assertRaises(AccessError):
            bootstrap.validate(item)

    def test_token_is_read_only_and_scoped_to_one_repository(self):
        app = object.__new__(GitHubApp)
        app.installation_id = 180768396
        app.jwt = Mock(return_value="synthetic-jwt")
        app.request = Mock(return_value={"token": "synthetic-token"})
        self.assertEqual(app.token("aof-back"), "synthetic-token")
        self.assertEqual(app.request.call_args.args[2], {"permissions": {"contents": "read"}, "repositories": ["aof-back"]})

    def test_wrong_installation_is_rejected(self):
        app = object.__new__(GitHubApp)
        app.app_id, app.installation_id, app.organization = 5707786, 180768396, "hitmakers-games"
        app.jwt = Mock(return_value="synthetic-jwt")
        app.request = Mock(return_value={"app_id": 5707786, "account": {"login": "another-org"}})
        with self.assertRaises(AccessError):
            app.verify_installation()

    def test_existing_checkout_and_unsaved_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / "aof-back"
            checkout.mkdir()
            note = checkout / "unsaved.txt"
            note.write_text("must survive")
            replies = [subprocess.CompletedProcess([], 0, metadata()["clone_url"]),
                       subprocess.CompletedProcess([], 0, "synthetic-head"),
                       subprocess.CompletedProcess([], 0, "origin/master")]
            with patch.object(bootstrap, "git", side_effect=replies) as git:
                item = bootstrap.populate(root, metadata())
            self.assertEqual(note.read_text(), "must survive")
            self.assertEqual(item["state"], "existing; left untouched")
            self.assertFalse(any("clone" in call.args[0] or "reset" in call.args[0] for call in git.call_args_list))

    def test_existing_wrong_origin_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "aof-back").mkdir()
            with patch.object(bootstrap, "git", return_value=subprocess.CompletedProcess([], 0, "https://other.test/repo")):
                with self.assertRaises(AccessError):
                    bootstrap.populate(root, metadata())
            self.assertTrue((root / "aof-back").exists())

    def test_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "aof-back").symlink_to("/tmp", target_is_directory=True)
            with self.assertRaises(AccessError):
                bootstrap.populate(root, metadata())

    def test_failed_clone_is_preserved_and_error_does_not_include_git_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(bootstrap, "git", return_value=subprocess.CompletedProcess([], 1, "", "synthetic-secret")):
                with self.assertRaises(AccessError) as error:
                    bootstrap.populate(root, metadata())
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertFalse((root / "aof-back").exists())
            self.assertEqual(len(list(root.glob(".clone-aof-back-*"))), 1)

    def test_plain_directory_is_not_accepted_as_persistent_workspace(self):
        with patch.object(bootstrap.os.path, "ismount", return_value=False):
            with self.assertRaises(AccessError):
                bootstrap.main()


if __name__ == "__main__":
    unittest.main()
