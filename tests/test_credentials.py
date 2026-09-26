"""Persistent Steam API key behavior without touching the real Windows registry."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse
import unittest
from unittest.mock import patch

from stormcopy import credentials
from stormcopy.__main__ import main as cli_main
from stormcopy.index import connect
from stormcopy.steam import discover


class _RegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeWinreg:
    """Minimal winreg implementation for HKCU\\Environment tests."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    KEY_WRITE = 4
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.paths = []

    def OpenKey(self, root, path, *_args):
        self.paths.append((root, path))
        return _RegistryKey()

    def CreateKeyEx(self, root, path, *_args):
        self.paths.append((root, path))
        return _RegistryKey()

    def CreateKey(self, root, path):
        self.paths.append((root, path))
        return _RegistryKey()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, value_type, value):
        if value_type != self.REG_SZ:
            raise AssertionError("API keys should be saved as a string")
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]

    def CloseKey(self, _key):
        pass


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.winreg = _FakeWinreg()
        self.registry_patch = patch.dict(sys.modules, {"winreg": self.winreg})
        self.module_patch = patch.object(credentials, "winreg", self.winreg, create=True)
        self.broadcast_patch = patch.object(credentials, "_broadcast_environment_change")
        self.registry_patch.start()
        self.module_patch.start()
        self.broadcast_patch.start()
        self.addCleanup(self.broadcast_patch.stop)
        self.addCleanup(self.module_patch.stop)
        self.addCleanup(self.registry_patch.stop)

    def test_saved_key_is_available_in_same_session(self):
        with patch.object(credentials.os, "name", "nt"), \
             patch.dict(os.environ, {}, clear=True):
            credentials.save_api_key("example-secret")
            self.assertEqual(credentials.get_api_key(), "example-secret")
            self.assertEqual(self.winreg.values.get("STEAM_API_KEY"), "example-secret")
            self.assertTrue(any(root is self.winreg.HKEY_CURRENT_USER and
                                path.lower() == "environment"
                                for root, path in self.winreg.paths))

    def test_explicit_and_current_session_values_take_precedence(self):
        self.winreg.values["STEAM_API_KEY"] = "saved-secret"
        with patch.object(credentials.os, "name", "nt"), \
             patch.dict(os.environ, {"STEAM_API_KEY": "process-secret"}):
            self.assertEqual(credentials.get_api_key(), "process-secret")
            self.assertEqual(credentials.get_api_key("explicit-secret"), "explicit-secret")
        with patch.object(credentials.os, "name", "nt"), \
             patch.dict(os.environ, {"STEAM_API_KEY": ""}):
            self.assertFalse(credentials.get_api_key())

    def test_clear_removes_saved_key(self):
        with patch.object(credentials.os, "name", "nt"), \
             patch.dict(os.environ, {}, clear=True):
            credentials.save_api_key("example-secret")
            credentials.clear_api_key()
            self.assertNotIn("STEAM_API_KEY", self.winreg.values)
            self.assertFalse(credentials.get_api_key())

    def test_blank_or_whitespace_key_is_rejected(self):
        with patch.object(credentials.os, "name", "nt"):
            for value in ("", " ", "\t\n"):
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        credentials.save_api_key(value)
        self.assertNotIn("STEAM_API_KEY", self.winreg.values)

    def test_repeated_32_character_key_is_rejected_before_registry_write(self):
        example_key = "0123456789abcdef0123456789abcdef"
        with patch.object(credentials.os, "name", "nt"):
            with self.assertRaisesRegex(ValueError, "(?i:pasted twice|single key)"):
                credentials.save_api_key(example_key * 2)
        self.assertNotIn("STEAM_API_KEY", self.winreg.values)

    def test_non_windows_save_has_clear_error(self):
        with patch.object(credentials.os, "name", "posix"):
            with self.assertRaisesRegex(ValueError, "Windows"):
                credentials.save_api_key("example-secret")

    def test_discover_reads_saved_key_when_process_has_none(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect(Path(folder) / "catalog.sqlite")
            self.winreg.values["STEAM_API_KEY"] = "saved-secret"
            try:
                with patch.object(credentials.os, "name", "nt"), \
                     patch.dict(os.environ, {}, clear=True), \
                     patch("stormcopy.steam._request_json", return_value={
                         "response": {"result": 1, "publishedfiledetails": []}}) as request:
                    result = discover(db, max_pages=1, delay=0)
                self.assertTrue(result["complete"])
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(request.call_args.args[0]).query)
                self.assertEqual(query["key"], ["saved-secret"])
            finally:
                db.close()


class CredentialCliTests(unittest.TestCase):
    def _run(self, arguments, *, environment=None, prompt_value=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        env = {"STEAM_API_KEY": ""}
        if environment:
            env.update(environment)
        with patch.object(sys, "argv", ["stormcopy", *arguments]), \
             patch.dict(os.environ, env), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch("getpass.getpass", return_value=prompt_value) as prompt, \
             redirect_stdout(stdout), redirect_stderr(stderr):
            cli_main()
        return stdout.getvalue(), stderr.getvalue(), prompt

    def test_set_api_key_saves_process_environment_without_printing_it(self):
        with patch("stormcopy.__main__.save_api_key",
                   return_value={"saved": True, "scope": "Windows user environment"}) as save:
            output, errors, prompt = self._run(
                ["set-api-key", "--json"], environment={"STEAM_API_KEY": "sensitive-secret"})
        save.assert_called_once_with("sensitive-secret")
        prompt.assert_not_called()
        self.assertNotIn("sensitive-secret", output + errors)
        self.assertIn('"saved": true', output)

    def test_set_api_key_prompts_when_environment_absent(self):
        with patch("stormcopy.__main__.save_api_key",
                   return_value={"saved": True, "scope": "Windows user environment"}) as save:
            output, errors, prompt = self._run(
                ["set-api-key"], prompt_value="prompt-secret")
        save.assert_called_once_with("prompt-secret")
        prompt.assert_called_once()
        self.assertNotIn("prompt-secret", output + errors)

    def test_prompt_override_ignores_current_environment(self):
        with patch("stormcopy.__main__.save_api_key",
                   return_value={"saved": True, "scope": "Windows user environment"}) as save:
            output, errors, prompt = self._run(
                ["set-api-key", "--prompt"],
                environment={"STEAM_API_KEY": "old-secret"},
                prompt_value="new-secret")
        save.assert_called_once_with("new-secret")
        prompt.assert_called_once()
        self.assertNotIn("old-secret", output + errors)
        self.assertNotIn("new-secret", output + errors)

    def test_clear_command_removes_saved_key(self):
        with patch("stormcopy.__main__.clear_api_key",
                   return_value={"saved": False, "removed": True,
                                 "scope": "Windows user environment"}) as clear:
            output, errors, prompt = self._run(["set-api-key", "--clear", "--json"])
        clear.assert_called_once_with()
        prompt.assert_not_called()
        self.assertNotIn("secret", output + errors)


if __name__ == "__main__":
    unittest.main()
