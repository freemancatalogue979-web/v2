#!/usr/bin/env python3
"""Tests for bootstrap.py — the `python main.py` zero-setup launcher.

Offline only: nothing here installs a package or touches the network.
Run with:

    python3 tests/test_bootstrap.py
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402


def _venv_python_path(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class RequirementParsing(unittest.TestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        path = tmp / "requirements.txt"
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def test_parses_names_and_drops_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), """
                # comment line
                fastapi==0.109.2
                uvicorn[standard]==0.27.1
                PyJWT==2.8.0

                -r extra.txt
                --index-url https://example.invalid/simple
                seleniumbase>=4.34.0,<5
                brotli>=1.0.9
                httpx ; python_version >= "3.10"
            """)
            got = bootstrap.parse_requirements(path)
        self.assertEqual(
            got,
            ["fastapi", "uvicorn", "PyJWT", "seleniumbase", "brotli", "httpx"],
        )

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(bootstrap.parse_requirements(Path("/nonexistent/req.txt")), [])

    def test_real_requirements_covers_what_main_imports(self):
        names = {n.lower() for n in bootstrap.parse_requirements()}
        for needed in ("fastapi", "uvicorn", "httpx", "bcrypt", "pyjwt", "slowapi", "psutil"):
            self.assertIn(needed, names)
        self.assertIn("opencv-python-headless", names)

    def test_import_names_for_awkward_packages(self):
        self.assertEqual(bootstrap.import_name("PyJWT"), "jwt")
        self.assertEqual(bootstrap.import_name("opencv-python-headless"), "cv2")
        self.assertEqual(bootstrap.import_name("playwright-stealth"), "playwright_stealth")
        self.assertEqual(bootstrap.import_name("asyncio-dgram"), "asyncio_dgram")
        self.assertEqual(bootstrap.import_name("SomethingUnknown"), "somethingunknown")

    def test_missing_packages_uses_the_finder(self):
        required = ["fastapi", "uvicorn", "PyJWT"]
        missing = bootstrap.missing_packages(required, finder=lambda m: m != "jwt")
        self.assertEqual(missing, ["PyJWT"])
        self.assertEqual(bootstrap.missing_packages(required, finder=lambda m: True), [])


class VenvDetection(unittest.TestCase):
    def test_finds_venv_dot_venv_and_env(self):
        for name in ("venv", ".venv", "env"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                python = _venv_python_path(root / name)
                python.parent.mkdir(parents=True)
                python.write_text("", encoding="utf-8")
                self.assertEqual(bootstrap.find_venv(root), root / name, name)

    def test_no_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(bootstrap.find_venv(Path(tmp)))

    def test_running_in_current_prefix(self):
        self.assertTrue(bootstrap.running_in(Path(sys.prefix)))

    def test_running_in_other_prefix_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(bootstrap.running_in(Path(tmp)))

    def test_reexec_actually_lands_in_the_venv(self):
        """A project venv must be used even when started with the system python."""
        if os.name == "nt":
            self.skipTest("posix venv layout only")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            shutil.copy(ROOT / "bootstrap.py", project / "bootstrap.py")
            (project / "main.py").write_text(
                "import sys\n"
                "import bootstrap\n"
                "s = bootstrap.prepare(sys.argv[1:])\n"
                "print('PREFIX=' + sys.prefix)\n",
                encoding="utf-8",
            )
            made = subprocess.run(
                [sys.executable, "-m", "venv", str(project / "venv")],
                capture_output=True,
            )
            if made.returncode != 0:
                self.skipTest("python -m venv unavailable here")

            done = subprocess.run(
                [sys.executable, str(project / "main.py")],
                cwd=tmp,              # deliberately NOT the project directory
                capture_output=True,
                text=True,
                env={**os.environ, "FIXIIS_NO_SETUP": "1"},
                timeout=120,
            )
            self.assertIn(f"PREFIX={project / 'venv'}", done.stdout, done.stderr)


class FirstRunFiles(unittest.TestCase):
    def test_env_created_from_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("PORT=9000\n", encoding="utf-8")
            self.assertTrue(bootstrap.ensure_env_file(root))
            self.assertEqual((root / ".env").read_text(), "PORT=9000\n")
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

    def test_existing_env_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("PORT=1234\n", encoding="utf-8")
            (root / ".env.example").write_text("PORT=9000\n", encoding="utf-8")
            before = (root / ".env").stat().st_mtime_ns
            self.assertFalse(bootstrap.ensure_env_file(root))
            self.assertEqual((root / ".env").read_text(), "PORT=1234\n")
            self.assertEqual((root / ".env").stat().st_mtime_ns, before)

    def test_minimal_env_when_no_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(bootstrap.ensure_env_file(root))
            self.assertIn("PORT=", (root / ".env").read_text())

    def test_runtime_dirs_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = bootstrap.ensure_dirs(root, ["logs", "lpv"])
            self.assertEqual(created, ["logs", "lpv"])
            self.assertEqual(bootstrap.ensure_dirs(root, ["logs", "lpv"]), [])
            self.assertTrue((root / "logs").is_dir())


class CommandLine(unittest.TestCase):
    def _run(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "main.py"), *args],
            cwd=cwd or tempfile.gettempdir(),
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_check_runs_from_any_directory(self):
        done = self._run("--check")
        self.assertIn("environment check", done.stdout)
        self.assertIn(f"directory: {ROOT}", done.stdout)
        self.assertIn("typed in:", done.stdout)
        self.assertIn(done.returncode, (0, 1))
        self.assertIn("python main.py", done.stdout)

    def test_help_lists_the_flags(self):
        done = self._run("--help")
        self.assertEqual(done.returncode, 0)
        for flag in ("--check", "--setup", "--no-install", "--port", "--open"):
            self.assertIn(flag, done.stdout)

    def test_unknown_flag_is_rejected(self):
        done = self._run("--nonsense")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown option", done.stderr)

    def test_bad_port_value(self):
        done = self._run("--port", "abc")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--port must be a number", done.stderr)

    def test_port_flag_overrides_check(self):
        done = self._run("--check", "--port", "65001")
        self.assertIn("65001", done.stdout)

    def test_network_and_local_shortcuts(self):
        out = self._run("--check", "--network")
        self.assertIn("network reachable", out.stdout)

        out = self._run("--check", "--local")
        self.assertIn("this machine only", out.stdout)

    def test_port_out_of_range_is_rejected(self):
        for value in ("0", "70000", "-5"):
            done = self._run("--port", value)
            self.assertEqual(done.returncode, 2, value)
            self.assertIn("--port must be between 1 and 65535", done.stderr)

    def test_junk_port_in_environment_does_not_crash(self):
        saved = os.environ.get("PORT")
        os.environ["PORT"] = "not-a-number"
        try:
            self.assertEqual(bootstrap.port_from_env(), 8446)
            os.environ["PORT"] = "9000"
            self.assertEqual(bootstrap.port_from_env(), 9000)
            os.environ["PORT"] = "99999"
            self.assertEqual(bootstrap.port_from_env(), 8446)
        finally:
            if saved is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = saved


class Diagnostics(unittest.TestCase):
    def test_report_marks_missing_critical_packages(self):
        out = io.StringIO()
        code = bootstrap.diagnose(stream=out, finder=lambda m: False)
        text = out.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] package fastapi", text)
        self.assertIn("--setup", text)

    def test_loopback_bind_is_reported_with_a_fix(self):
        out = io.StringIO()
        saved = os.environ.get("HOST")
        os.environ["HOST"] = "127.0.0.1"
        try:
            code = bootstrap.diagnose(stream=out, finder=lambda m: True)
        finally:
            if saved is None:
                os.environ.pop("HOST", None)
            else:
                os.environ["HOST"] = saved
        text = out.getvalue()
        self.assertIn("[warn] bind address", text)
        self.assertIn("this machine only", text)
        self.assertIn("--host 0.0.0.0", text)
        self.assertNotEqual(code, 0)  # Chrome is still missing in CI

    def test_public_bind_shows_the_network_url(self):
        out = io.StringIO()
        saved = os.environ.get("HOST")
        os.environ["HOST"] = "0.0.0.0"
        try:
            bootstrap.diagnose(stream=out, finder=lambda m: True)
        finally:
            if saved is None:
                os.environ.pop("HOST", None)
            else:
                os.environ["HOST"] = saved
        text = out.getvalue()
        self.assertIn("bind address", text)
        self.assertIn("network reachable", text)

    def test_report_is_green_when_everything_is_present(self):
        out = io.StringIO()
        old = os.environ.get("CHROME_PATH")
        os.environ["CHROME_PATH"] = sys.executable  # any existing file will do
        try:
            # Pretend the repo has no venv so this stays independent of
            # whether a developer has one checked out.
            with mock.patch.object(bootstrap, "find_venv", lambda *a, **k: None):
                code = bootstrap.diagnose(stream=out, finder=lambda m: True)
        finally:
            if old is None:
                os.environ.pop("CHROME_PATH", None)
            else:
                os.environ["CHROME_PATH"] = old
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("Ready: python main.py", text)


class Hosts(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.53", " [::1] "):
            self.assertTrue(bootstrap.is_loopback(host), host)
        for host in ("0.0.0.0", "::", "", "192.168.1.20", "10.0.0.5"):
            self.assertFalse(bootstrap.is_loopback(host), host)

    def test_env_file_values_reads_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "# comment\nHOST=0.0.0.0\nPORT = 9444\nQUOTED=\"a=b\"\nbad line\n",
                encoding="utf-8",
            )
            values = bootstrap.env_file_values(root)
        self.assertEqual(values["HOST"], "0.0.0.0")
        self.assertEqual(values["PORT"], "9444")
        self.assertEqual(values["QUOTED"], "a=b")

    def test_real_environment_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("HOST=127.0.0.1\nPORT=1234\n", encoding="utf-8")
            saved = {k: os.environ.get(k) for k in ("HOST", "PORT")}
            try:
                os.environ.pop("HOST", None)
                os.environ.pop("PORT", None)
                self.assertEqual(bootstrap.host_from_env(root=root), "127.0.0.1")
                self.assertEqual(bootstrap.port_from_env(root=root), 1234)
                os.environ["HOST"] = "0.0.0.0"
                os.environ["PORT"] = "5555"
                self.assertEqual(bootstrap.host_from_env(root=root), "0.0.0.0")
                self.assertEqual(bootstrap.port_from_env(root=root), 5555)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_default_host_is_reachable_on_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.environ.pop("HOST", None)
            try:
                self.assertEqual(bootstrap.host_from_env(root=Path(tmp)), "0.0.0.0")
                self.assertFalse(bootstrap.is_loopback(bootstrap.host_from_env(root=Path(tmp))))
            finally:
                if saved is not None:
                    os.environ["HOST"] = saved


class Banner(unittest.TestCase):
    def test_network_address_is_used_when_bound_publicly(self):
        out = io.StringIO()
        bootstrap.banner("0.0.0.0", 9000, stream=out, machine_ip="192.168.0.14")
        text = out.getvalue()
        for path in ("/admin", "/mini", "/client.html", "/health"):
            self.assertIn(f"http://192.168.0.14:9000{path}", text)
        self.assertIn("http://127.0.0.1:9000/admin", text)   # local alias kept
        self.assertNotIn("only this machine can open it", text)

    def test_specific_bind_address_is_used_as_is(self):
        out = io.StringIO()
        bootstrap.banner("10.1.2.3", 8446, stream=out)
        self.assertIn("http://10.1.2.3:8446/admin", out.getvalue())

    def test_loopback_banner_warns_and_says_how_to_fix(self):
        out = io.StringIO()
        bootstrap.banner("127.0.0.1", 8446, stream=out)
        text = out.getvalue()
        for path in ("/admin", "/mini", "/client.html", "/health"):
            self.assertIn(f"http://127.0.0.1:8446{path}", text)
        self.assertIn("only this machine can open it", text)
        self.assertIn("--host 0.0.0.0", text)
        self.assertIn("HOST=0.0.0.0", text)

    def test_localhost_counts_as_loopback(self):
        out = io.StringIO()
        bootstrap.banner("localhost", 8446, stream=out)
        self.assertIn("only this machine can open it", out.getvalue())

    def test_tunnel_line(self):
        out = io.StringIO()
        bootstrap.banner("0.0.0.0", 8446, tunnel="https://x.trycloudflare.com",
                         stream=out, machine_ip="192.168.0.14")
        self.assertIn("https://x.trycloudflare.com", out.getvalue())


class InstallSafety(unittest.TestCase):
    def test_no_requirements_and_no_packages_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                bootstrap.install_requirements(sys.executable, Path(tmp), packages=())
            )

    def test_hint_names_the_one_command_to_run(self):
        hint = bootstrap._install_hint(["fastapi", "uvicorn"])
        self.assertIn("fastapi", hint)
        self.assertIn("main.py --setup", hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
