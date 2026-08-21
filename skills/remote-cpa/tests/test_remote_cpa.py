from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cpa = load_module("remote_cpa_request_test", ROOT / "scripts" / "cpa_request.py")
provider = load_module("remote_cpa_provider_test", ROOT / "scripts" / "codex_provider.py")


class CpaRequestTests(unittest.TestCase):
    def write_config(self, directory: Path, value: dict, mode: int = 0o600) -> Path:
        path = directory / "remote-cpa.local.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(mode)
        return path

    def test_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "missing.json"
            with mock.patch.dict(os.environ, {"CPA_CONFIG": str(missing)}, clear=False):
                with self.assertRaisesRegex(cpa.CpaError, "was not found"):
                    cpa.local_config()

    def test_secret_in_local_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), {"base_url": "http://example/v1", "api_key": "no"})
            with mock.patch.dict(os.environ, {"CPA_CONFIG": str(path)}, clear=False):
                with self.assertRaisesRegex(cpa.CpaError, "secret field"):
                    cpa.local_config()

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_posix_config_must_be_private(self):
        with tempfile.TemporaryDirectory() as raw:
            path = self.write_config(Path(raw), {"base_url": "http://example/v1"}, 0o644)
            with mock.patch.dict(os.environ, {"CPA_CONFIG": str(path)}, clear=False):
                with self.assertRaisesRegex(cpa.CpaError, "0600"):
                    cpa.local_config()

    @unittest.skipIf(os.name == "nt", "Windows uses the live-tested SSH reverse-tunnel callback")
    def test_ssh_lookup_uses_no_stdin_transport(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            key = directory / "id_test"
            key.write_text("private key placeholder", encoding="utf-8")
            config = {
                "base_url": "http://<your-cpa-host>:8317/v1",
                "ssh_auto": True,
                "ssh_host": "<your-cpa-host>",
                "ssh_user": "a1234",
                "ssh_key": str(key),
                "remote_config": "/Users/a1234/.local/opt/CLIProxyAPI/config.local.yaml",
            }
            path = self.write_config(directory, config)
            completed = types.SimpleNamespace(returncode=0, stdout="secret-value\n", stderr="")
            with mock.patch.dict(os.environ, {"CPA_CONFIG": str(path)}, clear=False):
                with mock.patch.object(cpa.subprocess, "run", return_value=completed) as run:
                    self.assertEqual(cpa.resolve_api_key(), "secret-value")
            _, kwargs = run.call_args
            self.assertNotIn("input", kwargs)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            command = run.call_args.args[0]
            self.assertEqual(len(command[-1:]), 1)
            self.assertIn("python3 -c", command[-1])
            self.assertNotIn("python3 - ", command[-1])
            self.assertNotIn("'\"'\"'", command[-1])

    def test_doctor_never_prints_token(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            key = directory / "id_test"
            key.write_text("key", encoding="utf-8")
            path = self.write_config(
                directory,
                {
                    "base_url": "http://example.test/v1",
                    "ssh_auto": True,
                    "ssh_host": "host",
                    "ssh_user": "user",
                    "ssh_key": str(key),
                    "remote_config": "/config.yaml",
                },
            )
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"CPA_CONFIG": str(path)}, clear=False):
                with mock.patch.object(cpa, "resolve_api_key", return_value="TOP-SECRET"):
                    with mock.patch.object(cpa, "request_json", return_value={"data": [{"id": "m"}]}):
                        with contextlib.redirect_stdout(output):
                            cpa.command_doctor()
            self.assertNotIn("TOP-SECRET", output.getvalue())
            self.assertIn("status=ok", output.getvalue())


class ProviderTests(unittest.TestCase):
    def test_top_level_edit_does_not_touch_nested_model(self):
        original = 'model = "root"\n\n[profiles.work]\nmodel = "nested"\n'
        changed = provider.set_top_level_routing(original, "cpa-model", provider.PROVIDER_ID)
        parsed = provider.tomllib.loads(changed)
        self.assertEqual(parsed["model"], "cpa-model")
        self.assertEqual(parsed["profiles"]["work"]["model"], "nested")

    def test_enable_and_disable_restore_without_touching_auth_or_state(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".codex"
            home.mkdir()
            config = home / "config.toml"
            original = 'model = "built-in"\n\n[profiles.work]\nmodel = "nested"\n'
            config.write_text(original, encoding="utf-8")
            auth = home / "auth.json"
            state = home / "state_5.sqlite"
            auth.write_text('{"unchanged":true}', encoding="utf-8")
            state.write_bytes(b"unchanged-state")
            local = home / "remote-cpa.local.json"
            local.write_text(
                json.dumps({"base_url": "http://<your-cpa-host>:8317/v1", "provider_name": "CPA"}),
                encoding="utf-8",
            )
            local.chmod(0o600)
            env = {"CODEX_HOME": str(home), "CPA_CONFIG": str(local)}
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(provider, "run_cpa_doctor"):
                    provider.enable("gpt-5.6-sol")
                enabled = provider.tomllib.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(enabled["model_provider"], provider.PROVIDER_ID)
                self.assertEqual(enabled["profiles"]["work"]["model"], "nested")
                self.assertTrue((home / provider.PREVIOUS_CONFIG_NAME).exists())
                provider.disable(None)
            self.assertEqual(provider.tomllib.loads(config.read_text(encoding="utf-8"))["model"], "built-in")
            self.assertEqual(auth.read_text(encoding="utf-8"), '{"unchanged":true}')
            self.assertEqual(state.read_bytes(), b"unchanged-state")

    def test_disable_without_snapshot_requires_explicit_model(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / ".codex"
            home.mkdir()
            config = home / "config.toml"
            config.write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "cliproxyapi"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                with self.assertRaisesRegex(SystemExit, "No previous routing snapshot"):
                    provider.disable(None)
                provider.disable("gpt-5.4")
            parsed = provider.tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gpt-5.4")
            self.assertNotIn("model_provider", parsed)


if __name__ == "__main__":
    unittest.main()
