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
    def test_env_has_working_defaults_and_no_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text(
                "SERVER_DOMAIN=panel.example.com\nUSE_HTTPS=true\n"
                "PUBLIC_IP=\nADMIN_PASSWORD_HASH=\n", encoding="utf-8")
            self.assertTrue(bootstrap.ensure_env_file(root))
            text = (root / ".env").read_text()
            self.assertIn("HOST=0.0.0.0", text)
            self.assertIn("PORT=8446", text)
            self.assertIn("JWT_SECRET_KEY=", text)
            values = bootstrap.env_file_values(root)
            # The template's placeholders must NOT be copied over.
            self.assertNotIn("SERVER_DOMAIN", values)
            self.assertNotIn("USE_HTTPS", values)
            self.assertNotIn("PUBLIC_IP", values)
            self.assertNotIn("panel.example.com", values.values())
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)

    def test_jwt_secret_is_random_and_not_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bootstrap.ensure_env_file(root)
            first = bootstrap.env_file_values(root)["JWT_SECRET_KEY"]
            root2 = root / "second"
            root2.mkdir()
            bootstrap.ensure_env_file(root2)
            second = bootstrap.env_file_values(root2)["JWT_SECRET_KEY"]
            self.assertGreaterEqual(len(first), 32)
            self.assertNotEqual(first, second)

    def test_existing_env_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("PORT=1234\n", encoding="utf-8")
            before = (root / ".env").stat().st_mtime_ns
            self.assertFalse(bootstrap.ensure_env_file(root))
            self.assertEqual((root / ".env").read_text(), "PORT=1234\n")
            self.assertEqual((root / ".env").stat().st_mtime_ns, before)

    def test_runtime_dirs_created_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = bootstrap.ensure_dirs(root, ["logs", "lpv"])
            self.assertEqual(created, ["logs", "lpv"])
            self.assertEqual(bootstrap.ensure_dirs(root, ["logs", "lpv"]), [])
            self.assertTrue((root / "logs").is_dir())


class EnvRepair(unittest.TestCase):
    """A .env copied from the old template locked people out / broke URLs."""

    TEMPLATE = (
        "ADMIN_USERNAME=admin\n"
        "ADMIN_PASSWORD_HASH=\n"
        "PUBLIC_IP=\n"
        "SERVER_DOMAIN=panel.example.com\n"
        "USE_HTTPS=true\n"
        "HOST=0.0.0.0\n"
        "PORT=8446\n"
        "BROWSER_BACKEND=sb\n"
    )

    def _write(self, tmp: str, text: str) -> Path:
        root = Path(tmp)
        (root / ".env").write_text(text, encoding="utf-8")
        return root

    def test_repairs_placeholders_and_reports_each_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, self.TEMPLATE)
            changes = bootstrap.repair_env_file(root)
        joined = " | ".join(changes)
        self.assertIn("placeholder SERVER_DOMAIN", joined)
        self.assertIn("ADMIN_PASSWORD_HASH", joined)
        self.assertIn("PUBLIC_IP", joined)
        self.assertIn("JWT", joined)

    def test_repaired_file_has_no_active_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, self.TEMPLATE)
            bootstrap.repair_env_file(root)
            values = bootstrap.env_file_values(root)
        self.assertNotIn("SERVER_DOMAIN", values)
        self.assertNotIn("PUBLIC_IP", values)
        self.assertFalse(values.get("ADMIN_PASSWORD_HASH"))
        self.assertTrue(values.get("JWT_SECRET_KEY"))
        self.assertEqual(values["USE_HTTPS"], "false")
        self.assertEqual(values["PORT"], "8446")

    def test_loopback_host_is_opened_up_with_an_explanation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, "HOST=127.0.0.1\nPORT=8446\n")
            changes = bootstrap.repair_env_file(root)
            values = bootstrap.env_file_values(root)
            text = (root / ".env").read_text()
        self.assertEqual(values["HOST"], "0.0.0.0")
        self.assertTrue(any("HOST=127.0.0.1" in c for c in changes))
        self.assertIn("HOST=0.0.0.0", text)
        self.assertIn("loopback kept phones", text)

    def test_loopback_host_is_kept_when_a_tunnel_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(
                tmp,
                "HOST=127.0.0.1\nTUNNEL_HOSTNAME=x.trycloudflare.com\n"
                "JWT_SECRET_KEY=keep-me\n",
            )
            self.assertEqual(bootstrap.repair_env_file(root), [])
            self.assertEqual(bootstrap.env_file_values(root)["HOST"], "127.0.0.1")

    def test_cloudflare_tunnel_flag_also_keeps_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(
                tmp, "HOST=127.0.0.1\nCLOUDFLARE_TUNNEL=true\nJWT_SECRET_KEY=keep\n")
            self.assertEqual(bootstrap.repair_env_file(root), [])
            self.assertEqual(bootstrap.env_file_values(root)["HOST"], "127.0.0.1")

    def test_repair_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, self.TEMPLATE)
            self.assertTrue(bootstrap.repair_env_file(root))
            first = (root / ".env").read_text()
            self.assertEqual(bootstrap.repair_env_file(root), [])
            self.assertEqual((root / ".env").read_text(), first)

    def test_real_settings_are_preserved(self):
        real = (
            "ADMIN_USERNAME=admin\n"
            "ADMIN_PASSWORD_HASH=$2b$12$abcdefghijklmnopqrstuv\n"
            "SERVER_DOMAIN=panel.myserver.net\n"
            "USE_HTTPS=true\n"
            "JWT_SECRET_KEY=keep-this-one\n"
            "HOST=0.0.0.0\n"
            "PORT=9000\n"
            "CUSTOM_SETTING=1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, real)
            self.assertEqual(bootstrap.repair_env_file(root), [])
            self.assertEqual((root / ".env").read_text(), real)

    def test_comments_and_blank_lines_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, "# my notes\nHOST=0.0.0.0\n\n# tail\n")
            bootstrap.repair_env_file(root)
            text = (root / ".env").read_text()
        self.assertIn("# my notes", text)
        self.assertIn("# tail", text)

    def test_missing_env_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bootstrap.repair_env_file(Path(tmp)), [])

    def test_placeholder_detection(self):
        for value in ("panel.example.com", "https://panel.example.com/",
                      "EXAMPLE.COM", "ops.example.org"):
            self.assertTrue(bootstrap.is_placeholder_domain(value), value)
        for value in ("panel.myserver.net", "198.51.100.10", "", "mysite.io"):
            self.assertFalse(bootstrap.is_placeholder_domain(value), value)

    def test_admin_password_is_generated_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._write(tmp, self.TEMPLATE)
            try:
                import bcrypt  # noqa: F401
            except Exception:
                self.skipTest("bcrypt not installed in this interpreter")
            password = bootstrap.ensure_admin_password(root, stream=io.StringIO())
            self.assertTrue(password, "a password should be generated")
            values = bootstrap.env_file_values(root)
            self.assertTrue(values["ADMIN_PASSWORD_HASH"].startswith("$2"))
            self.assertIsNone(bootstrap.ensure_admin_password(root))  # only once
            self.assertEqual(bootstrap.env_file_values(root)["ADMIN_PASSWORD_HASH"],
                             values["ADMIN_PASSWORD_HASH"])


class DotenvLoading(unittest.TestCase):
    """main.py must treat `KEY=` as "not configured"."""

    def _load_dotenv(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("def _load_dotenv(")
        end = source.index("_loaded = (0 if")
        namespace: dict = {"os": os, "Path": Path}
        exec(compile(source[start:end], "main.py", "exec"), namespace)
        return namespace["_load_dotenv"]

    def test_blank_values_do_not_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "ADMIN_PASSWORD_HASH=\nJWT_SECRET_KEY=\nPUBLIC_IP=\n"
                "HOST=0.0.0.0\nPORT=8446\n",
                encoding="utf-8",
            )
            saved = {k: os.environ.pop(k, None) for k in
                     ("ADMIN_PASSWORD_HASH", "JWT_SECRET_KEY", "PUBLIC_IP", "HOST", "PORT")}
            try:
                loader = self._load_dotenv()
                loaded = loader(env)
                self.assertEqual(loaded, 2)
                for key in ("ADMIN_PASSWORD_HASH", "JWT_SECRET_KEY", "PUBLIC_IP"):
                    self.assertNotIn(key, os.environ, key)
                self.assertEqual(os.environ["HOST"], "0.0.0.0")
                self.assertEqual(os.environ["PORT"], "8446")
            finally:
                for key in ("ADMIN_PASSWORD_HASH", "JWT_SECRET_KEY", "PUBLIC_IP", "HOST", "PORT"):
                    os.environ.pop(key, None)
                for key, value in saved.items():
                    if value is not None:
                        os.environ[key] = value


class ConfigSanitising(unittest.TestCase):
    """config.py must ignore blank values and template domains."""

    def _run(self, env: dict) -> str:
        code = (
            "import sys, json; sys.path.insert(0, %r); import config; "
            "c = config.CONFIG; print(json.dumps([c.server_domain, c.public_ip, "
            "c.host, c.port]))" % str(ROOT)
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
            env={**{k: v for k, v in os.environ.items()
                    if k not in ("SERVER_DOMAIN", "PUBLIC_IP", "HOST", "PORT")}, **env},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_placeholder_domain_is_ignored(self):
        import json
        domain, ip, host, port = json.loads(self._run({"SERVER_DOMAIN": "panel.example.com"}))
        self.assertEqual(domain, "")

    def test_blank_values_fall_back_to_defaults(self):
        import json
        domain, ip, host, port = json.loads(
            self._run({"SERVER_DOMAIN": "", "PUBLIC_IP": "", "HOST": "", "PORT": ""}))
        self.assertEqual(domain, "")
        # Blank means "no address is baked in": links stay server-relative so
        # the same install works on localhost, a LAN IP, nginx or a tunnel.
        self.assertEqual(ip, "")
        self.assertTrue(host)
        self.assertEqual(port, 8446)

    def test_real_values_are_kept(self):
        import json
        domain, ip, host, port = json.loads(
            self._run({"SERVER_DOMAIN": "panel.myserver.net", "HOST": "127.0.0.1",
                       "PORT": "9000"}))
        self.assertEqual(domain, "panel.myserver.net")
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 9000)


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
        self.assertIn("HOST (from environment)", text)
        self.assertIn("this machine only", text)
        self.assertIn("0.0.0.0", text)
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
        self.assertIn("HOST (from environment)", text)
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
        for host in ("0.0.0.0", "::", "", "198.51.100.10", "203.0.113.5"):
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
        bootstrap.banner("0.0.0.0", 9000, stream=out, machine_ip="198.51.100.10")
        text = out.getvalue()
        for path in ("/admin", "/mini", "/client.html", "/health"):
            self.assertIn(f"http://198.51.100.10:9000{path}", text)
        self.assertIn("http://127.0.0.1:9000/admin", text)   # local alias kept
        self.assertNotIn("only this machine can open it", text)

    def test_specific_bind_address_is_used_as_is(self):
        out = io.StringIO()
        bootstrap.banner("203.0.113.5", 8446, stream=out)
        self.assertIn("http://203.0.113.5:8446/admin", out.getvalue())

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
                         stream=out, machine_ip="198.51.100.10")
        self.assertIn("https://x.trycloudflare.com", out.getvalue())


class AdminLoginSummary(unittest.TestCase):
    """A locked-out operator must be able to see what the login accepts."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {k: os.environ.pop(k, None)
                       for k in ("ADMIN_PASSWORD_HASH", "ADMIN_USERNAME", "FIXIIS_IGNORE_ENV")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_hash_validity(self):
        good = "$2b$12$K9Xm2pQ4rT7vN8wL3jH6e.nSuc39Ma1gWXoznLSkhTeaL9F1CfDne"
        self.assertTrue(bootstrap.is_usable_password_hash(good))
        self.assertTrue(bootstrap.is_usable_password_hash("a" * 32))      # legacy MD5
        self.assertFalse(bootstrap.is_usable_password_hash(""))
        self.assertFalse(bootstrap.is_usable_password_hash("admin"))      # plain text
        self.assertFalse(bootstrap.is_usable_password_hash("$2b$12$short"))
        self.assertFalse(bootstrap.is_usable_password_hash("$2b$12$" + "x" * 80))

    def test_no_env_means_the_built_in_default(self):
        text, warn = bootstrap.admin_login_summary(self.root)
        self.assertIn("default password: admin", text)
        self.assertIn("--set-password", text)
        self.assertTrue(warn, "using the shipped default is worth a warning")

    def test_a_good_hash_is_reported_as_set(self):
        (self.root / ".env").write_text(
            "ADMIN_PASSWORD_HASH=" + "$2b$12$K9Xm2pQ4rT7vN8wL3jH6e.nSuc39Ma1gWXoznLSkhTeaL9F1CfDne" + "\n",
            encoding="utf-8")
        text, warn = bootstrap.admin_login_summary(self.root)
        self.assertIn("password set in .env", text)
        self.assertIn("--set-password", text, "a forgotten password needs a way out")
        self.assertFalse(warn)

    def test_an_unusable_hash_says_no_password_can_work(self):
        (self.root / ".env").write_text("ADMIN_PASSWORD_HASH=admin\n", encoding="utf-8")
        text, warn = bootstrap.admin_login_summary(self.root)
        self.assertIn("unusable", text)
        self.assertTrue(warn)

    def test_banner_prints_the_login_line(self):
        out = io.StringIO()
        bootstrap.banner("0.0.0.0", 8446, stream=out, machine_ip="198.51.100.10",
                         login="user 'admin' - default password: admin")
        self.assertIn("login", out.getvalue())
        self.assertIn("default password: admin", out.getvalue())


class OptionalPackagesDoNotBlockStartup(unittest.TestCase):
    def test_only_critical_missing_packages_stop_the_boot(self):
        saved = {k: os.environ.get(k) for k in
                 ("FIXIIS_NO_SETUP", "FIXIIS_AUTO_SETUP", "FIXIIS_BANNER")}
        try:
            os.environ["FIXIIS_NO_SETUP"] = "1"
            os.environ.pop("FIXIIS_AUTO_SETUP", None)
            os.environ["FIXIIS_BANNER"] = "0"
            # These tests must not create data folders in the checkout, so
            # the first-run helpers are stubbed out.
            quiet = [
                mock.patch.object(bootstrap, "ensure_dirs", lambda *a, **k: []),
                mock.patch.object(bootstrap, "repair_env_file", lambda *a, **k: []),
            ]
            for patch in quiet:
                patch.start()
            try:
                with mock.patch.object(bootstrap, "missing_packages", lambda *a, **k: ["brotli"]):
                    startup = bootstrap.prepare([])      # optional only -> no exit
                    self.assertFalse(startup.installed)
                with mock.patch.object(bootstrap, "missing_packages", lambda *a, **k: ["fastapi"]):
                    with self.assertRaises(SystemExit) as raised:
                        bootstrap.prepare([])            # critical -> refuses to run
                    self.assertEqual(raised.exception.code, 2)
            finally:
                for patch in quiet:
                    patch.stop()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.chdir(ROOT)


class ConfigIsTheSourceOfTruth(unittest.TestCase):
    """`python main.py` must work with config.py alone - no .env required."""

    def test_prepare_never_creates_env(self):
        saved = {k: os.environ.get(k) for k in
                 ("FIXIIS_NO_SETUP", "FIXIIS_AUTO_SETUP", "FIXIIS_IGNORE_ENV")}
        calls = []
        try:
            os.environ["FIXIIS_NO_SETUP"] = "1"
            os.environ.pop("FIXIIS_AUTO_SETUP", None)
            os.environ.pop("FIXIIS_IGNORE_ENV", None)
            with mock.patch.object(bootstrap, "ensure_env_file",
                                   lambda *a, **k: calls.append(1) or True), \
                 mock.patch.object(bootstrap, "ensure_dirs", lambda *a, **k: []), \
                 mock.patch.object(bootstrap, "repair_env_file", lambda *a, **k: []), \
                 mock.patch.object(bootstrap, "missing_packages", lambda *a, **k: []):
                bootstrap.prepare([])
            self.assertEqual(calls, [], "prepare() must not write a .env")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.chdir(ROOT)

    def test_host_and_port_default_to_config_py(self):
        host, host_src = bootstrap.effective("HOST")
        self.assertEqual(host, "0.0.0.0")
        self.assertIn(host_src, ("config.py", ".env", "environment"))
        port, port_src = bootstrap.effective("PORT")
        self.assertEqual(int(port), 8446)

    def test_env_file_overrides_config_only_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("PORT=9234\n", encoding="utf-8")
            saved = {k: os.environ.pop(k, None) for k in ("PORT", "FIXIIS_IGNORE_ENV")}
            try:
                value, source = bootstrap.effective("PORT", root=root)
                self.assertEqual((value, source), ("9234", ".env"))

                # --ignore-env: the same .env is ignored, config.py wins.
                os.environ["FIXIIS_IGNORE_ENV"] = "1"
                value, source = bootstrap.effective("PORT", root=root)
                self.assertEqual((int(value), source), (8446, "config.py"))
            finally:
                os.environ.pop("FIXIIS_IGNORE_ENV", None)
                for key, value in saved.items():
                    if value is not None:
                        os.environ[key] = value

    def test_environment_beats_everything(self):
        saved = os.environ.get("PORT")
        os.environ["PORT"] = "8123"
        try:
            self.assertEqual(bootstrap.effective("PORT"), ("8123", "environment"))
        finally:
            if saved is None:
                os.environ.pop("PORT", None)
            else:
                os.environ["PORT"] = saved

    def test_ignore_env_flag_is_parsed(self):
        saved = os.environ.get("FIXIIS_IGNORE_ENV")
        try:
            with mock.patch.object(bootstrap, "ensure_dirs", lambda *a, **k: []), \
                 mock.patch.object(bootstrap, "missing_packages", lambda *a, **k: []), \
                 mock.patch.object(bootstrap, "repair_env_file", lambda *a, **k: []):
                bootstrap.prepare(["--ignore-env"])
            self.assertTrue(bootstrap.env_is_ignored())
        finally:
            if saved is None:
                os.environ.pop("FIXIIS_IGNORE_ENV", None)
            else:
                os.environ["FIXIIS_IGNORE_ENV"] = saved
            os.chdir(ROOT)

    def test_check_says_where_settings_come_from(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "--check"],
            capture_output=True, text=True, timeout=180,
            env={k: v for k, v in os.environ.items()
                 if k not in ("HOST", "PORT", "FIXIIS_IGNORE_ENV")},
        )
        text = done.stdout
        self.assertIn("from config.py", text)
        self.assertIn("config.py only", text)

    def test_set_password_writes_env_and_keeps_other_lines(self):
        try:
            import bcrypt  # noqa: F401
        except Exception:
            self.skipTest("bcrypt not installed in this interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("HOST=127.0.0.1\nCUSTOM=1\n", encoding="utf-8")
            ok = bootstrap.set_admin_password(root, stream=io.StringIO(), password="hunter2hunter2")
            self.assertTrue(ok)
            values = bootstrap.env_file_values(root)
            self.assertTrue(values["ADMIN_PASSWORD_HASH"].startswith("$2"))
            self.assertTrue(values["JWT_SECRET_KEY"])
            self.assertEqual(values["HOST"], "127.0.0.1")
            self.assertEqual(values["CUSTOM"], "1")

    def test_set_password_replaces_an_existing_hash(self):
        try:
            import bcrypt  # noqa: F401
        except Exception:
            self.skipTest("bcrypt not installed in this interpreter")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("ADMIN_PASSWORD_HASH=$2b$12$old\n", encoding="utf-8")
            self.assertTrue(bootstrap.set_admin_password(root, stream=io.StringIO(),
                                                         password="another-password"))
            values = bootstrap.env_file_values(root)
            self.assertNotEqual(values["ADMIN_PASSWORD_HASH"], "$2b$12$old")
            self.assertEqual(values["ADMIN_PASSWORD_HASH"].count("ADMIN"), 0)


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
