import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pyxcom import XClient
from pyxcom.auth import load_x_cookies, profile_cookie_db
from pyxcom.cli import build_parser
from pyxcom.errors import AuthenticationError


def cookie(name, value, domain=".x.com"):
    return SimpleNamespace(name=name, value=value, domain=domain)


class SafariAuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.first = self.directory / "container" / "Cookies.binarycookies"
        self.second = self.directory / "legacy" / "Cookies.binarycookies"
        self.platform = patch("pyxcom.auth.sys.platform", "darwin")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        locations = patch(
            "pyxcom.auth._SAFARI_COOKIE_FILES", (str(self.first), str(self.second))
        )
        locations.start()
        self.addCleanup(locations.stop)
        self.reader = patch("pyxcom.auth.browser_cookie3.safari")
        self.read = self.reader.start()
        self.addCleanup(self.reader.stop)
        self.read.return_value = [
            cookie("auth_token", "dummy-a"),
            cookie("ct0", "dummy-b"),
        ]

    def create_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline fixture")

    def test_default_prefers_container_cookie_file(self):
        self.create_file(self.first)
        self.create_file(self.second)
        self.assertEqual(
            load_x_cookies("safari"), {"auth_token": "dummy-a", "ct0": "dummy-b"}
        )
        self.read.assert_called_once_with(
            domain_name=".x.com", cookie_file=str(self.first)
        )

    def test_missing_container_file_falls_back_to_legacy_file(self):
        self.create_file(self.second)
        load_x_cookies("safari")
        self.read.assert_called_once_with(
            domain_name=".x.com", cookie_file=str(self.second)
        )

    def test_explicit_cookie_file_is_passed_to_reader(self):
        selected = self.directory / "explicit.binarycookies"
        self.create_file(selected)
        load_x_cookies("safari", cookie_db=selected)
        self.read.assert_called_once_with(
            domain_name=".x.com", cookie_file=str(selected)
        )

    def test_profile_is_rejected_before_cookie_file_inspection(self):
        with patch("pyxcom.auth.Path.stat") as inspect:
            with self.assertRaisesRegex(AuthenticationError, "profile.*not supported"):
                load_x_cookies("safari", profile="Personal")
            inspect.assert_not_called()
        self.read.assert_not_called()
        with self.assertRaisesRegex(AuthenticationError, "profile.*not supported"):
            profile_cookie_db("safari", "Personal")

    def test_safari_requires_macos_before_reading(self):
        for platform in ("linux", "win32"):
            with (
                self.subTest(platform=platform),
                patch("pyxcom.auth.sys.platform", platform),
            ):
                with self.assertRaisesRegex(AuthenticationError, "only on macOS"):
                    load_x_cookies("safari")
        self.read.assert_not_called()

    def test_missing_default_files_explain_manual_login_and_explicit_path(self):
        with self.assertRaises(AuthenticationError) as raised:
            load_x_cookies("safari")
        message = str(raised.exception)
        self.assertIn("not found", message)
        self.assertIn("manually", message)
        self.assertIn("cookie_db", message)
        self.read.assert_not_called()

    def test_explicit_missing_or_directory_path_does_not_reach_reader(self):
        for selected in (self.directory / "missing.binarycookies", self.directory):
            with self.subTest(selected=selected):
                with self.assertRaises(AuthenticationError):
                    load_x_cookies("safari", cookie_db=selected)
        self.read.assert_not_called()

    def test_permission_denied_default_does_not_fall_back(self):
        self.create_file(self.second)
        checked = []
        original_stat = Path.stat

        def inspect(path, *args, **kwargs):
            checked.append(path)
            if path == self.first:
                raise PermissionError(errno.EPERM, "secret backend details")
            return original_stat(path, *args, **kwargs)

        with patch("pyxcom.auth.Path.stat", new=inspect):
            with self.assertRaisesRegex(AuthenticationError, "Full Disk Access"):
                load_x_cookies("safari")
        self.assertEqual(checked, [self.first])
        self.read.assert_not_called()

    def test_permission_denied_explicit_file_is_actionable_and_sanitized(self):
        with patch(
            "pyxcom.auth.Path.stat",
            side_effect=PermissionError(errno.EACCES, "secret backend details"),
        ):
            with self.assertRaises(AuthenticationError) as raised:
                load_x_cookies("safari", cookie_db=self.first)
        self.assertIn("Full Disk Access", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.read.assert_not_called()

    def test_reader_permission_errors_are_actionable_and_sanitized(self):
        self.create_file(self.first)
        for failure in (
            PermissionError("secret token"),
            OSError(errno.EPERM, "secret token"),
            OSError(errno.EACCES, "secret token"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.read.side_effect = failure
                with self.assertRaises(AuthenticationError) as raised:
                    load_x_cookies("safari")
                self.assertIn("Full Disk Access", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    def test_corrupt_reader_error_is_sanitized(self):
        self.create_file(self.first)
        self.read.side_effect = ValueError("secret cookie contents")
        with self.assertRaises(AuthenticationError) as raised:
            load_x_cookies("safari")
        self.assertIn("Could not parse", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_only_login_cookies_on_exact_x_domains_are_returned(self):
        self.create_file(self.first)
        self.read.return_value = [
            cookie("auth_token", "dummy-a", "x.com"),
            cookie("ct0", "dummy-b", ".x.com"),
            cookie("twid", "unused"),
            cookie("auth_token", "unrelated", "example.com"),
            cookie("auth_token", "subdomain", "api.x.com"),
            cookie("auth_token", "lookalike", "x.com.example.com"),
            cookie("auth_token", "malformed", "..x.com"),
        ]
        self.assertEqual(
            load_x_cookies("safari"), {"auth_token": "dummy-a", "ct0": "dummy-b"}
        )

    def test_missing_login_fails_without_cookie_values(self):
        self.create_file(self.first)
        self.read.return_value = [
            cookie("ct0", "secret-value"),
            cookie("twid", "unused"),
        ]
        with self.assertRaises(AuthenticationError) as raised:
            load_x_cookies("safari")
        self.assertIn("sign in to x.com", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))


class BrowserAuthCompatibilityTests(unittest.TestCase):
    def test_chrome_and_edge_keep_default_reader_arguments(self):
        for browser in ("chrome", "edge"):
            with (
                self.subTest(browser=browser),
                patch(
                    f"pyxcom.auth.browser_cookie3.{browser}",
                    return_value=[
                        cookie("auth_token", "dummy-a"),
                        cookie("ct0", "dummy-b"),
                    ],
                ) as read,
            ):
                self.assertEqual(
                    load_x_cookies(browser),
                    {"auth_token": "dummy-a", "ct0": "dummy-b"},
                )
                read.assert_called_once_with(domain_name=".x.com")

    def test_chrome_and_edge_profile_paths_keep_reader_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "Cookies"
            selected.write_bytes(b"offline fixture")
            for browser in ("chrome", "edge"):
                with (
                    self.subTest(browser=browser),
                    patch(
                        "pyxcom.auth.profile_cookie_db", return_value=selected
                    ) as resolve,
                    patch(
                        f"pyxcom.auth.browser_cookie3.{browser}",
                        return_value=[cookie("auth_token", "a"), cookie("ct0", "b")],
                    ) as read,
                ):
                    load_x_cookies(browser, profile="Profile 3")
                    resolve.assert_called_once_with(browser, "Profile 3")
                    read.assert_called_once_with(
                        domain_name=".x.com", cookie_file=str(selected)
                    )

    def test_unknown_browser_is_rejected(self):
        with self.assertRaisesRegex(AuthenticationError, "Supported browsers"):
            load_x_cookies("unknown")

    def test_cli_accepts_safari(self):
        args = build_parser().parse_args(["user", "example", "--browser", "safari"])
        self.assertEqual(args.browser, "safari")
        self.assertIsNone(args.profile)

    def test_client_forwards_safari_configuration_to_loader(self):
        selected = Path("offline.binarycookies")
        with (
            patch(
                "pyxcom.client.load_x_cookies",
                return_value={"auth_token": "dummy-a", "ct0": "dummy-b"},
            ) as load,
            patch("pyxcom.client.XTransport") as transport,
        ):
            with XClient(browser="safari", cookie_db=selected):
                pass
            load.assert_called_once_with("safari", profile=None, cookie_db=selected)
            transport.return_value.close.assert_called_once()
