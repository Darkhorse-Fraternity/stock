import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HermesScriptRegressionTests(unittest.TestCase):
    def test_daily_llm_timeout_stays_below_hermes_script_limit(self) -> None:
        script = (ROOT / "scripts" / "hermes-ai-run.sh").read_text(encoding="utf-8")
        match = re.search(r"STOCK_AGENT_LLM_TIMEOUT:-(?P<seconds>\d+)", script)
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        example_match = re.search(r"^STOCK_AGENT_LLM_TIMEOUT=(?P<seconds>\d+)$", example, re.MULTILINE)

        self.assertIsNotNone(match)
        self.assertIsNotNone(example_match)
        self.assertLess(int(match.group("seconds")), 120)
        self.assertLess(int(example_match.group("seconds")), 120)
        self.assertIn('STOCK_AGENT_ENABLE_TICK="${STOCK_AGENT_ENABLE_TICK:-0}"', script)

    def test_runtime_scripts_load_ignored_environment_file_without_private_defaults(self) -> None:
        for name in (
            "hermes-agent-data-run.sh",
            "hermes-ai-run.sh",
            "hermes-portfolio-risk-run.sh",
            "hermes-tracking-run.sh",
        ):
            with self.subTest(script=name):
                script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("STOCK_AGENT_ENV_FILE", script)
                self.assertIn('. "$ENV_FILE"', script)
                self.assertNotIn("/home/aura", script)
                self.assertNotIn("192.168.", script)

    def test_private_environment_files_are_ignored_but_example_is_versioned(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".env", ignore)
        self.assertIn(".env.*", ignore)
        self.assertIn("!.env.example", ignore)
        self.assertTrue((ROOT / ".env.example").is_file())

    def test_systemd_service_reads_env_without_embedding_instance_secrets(self) -> None:
        service = (ROOT / "deploy" / "stock-agent-admin.service").read_text(encoding="utf-8")

        self.assertIn("EnvironmentFile=-%h/internal-tools/apps/stock-agent/.env", service)
        self.assertIn(
            "ExecStart=%h/internal-tools/apps/stock-agent/.venv/bin/python",
            service,
        )
        self.assertNotIn("192.168.", service)
        self.assertNotIn("STOCK_AGENT_DEFAULT_DELIVERY_TARGET=", service)
        self.assertNotIn("STOCK_AGENT_LLM_API_KEY=", service)

    def test_ai_script_applies_external_env_before_resolving_app_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_dir = root / "relocated-stock-agent"
            package_dir = app_dir / "src" / "stock_recommender"
            package_dir.mkdir(parents=True)
            (package_dir / "cli.py").write_text("", encoding="utf-8")

            fake_python = root / "fake-python"
            fake_python.write_text(
                '#!/bin/sh\nprintf "%s|%s|%s\\n" "$PWD" "$STOCK_AGENT_LLM_MODEL" "$STOCK_AGENT_MODE"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            env_file = root / "runtime.env"
            env_file.write_text(
                f"STOCK_AGENT_APP_DIR={app_dir}\n"
                f"STOCK_AGENT_PYTHON={fake_python}\n"
                "STOCK_AGENT_LLM_MODEL=fixture-model\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [str(ROOT / "scripts" / "hermes-ai-run.sh")],
                check=True,
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ["PATH"],
                    "STOCK_AGENT_ENV_FILE": str(env_file),
                },
            )

            self.assertEqual(
                result.stdout.strip(),
                f"{app_dir}|fixture-model|ai",
            )

    def test_installed_runtime_launcher_stays_inside_hermes_scripts_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_dir = root / "stock-agent"
            package_dir = app_dir / "src" / "stock_recommender"
            scripts_dir = app_dir / "scripts"
            runtime_dir = root / "hermes" / "scripts"
            package_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            (package_dir / "cli.py").write_text("", encoding="utf-8")

            fake_python = root / "fake-python"
            fake_python.write_text(
                '#!/bin/sh\nprintf "%s|%s|%s\\n" "$PWD" "$STOCK_AGENT_LLM_MODEL" "$STOCK_AGENT_MODE"\n',
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            (app_dir / ".env").write_text(
                f"STOCK_AGENT_PYTHON={fake_python}\n"
                "STOCK_AGENT_LLM_MODEL=runtime-model\n",
                encoding="utf-8",
            )

            shutil.copytree(ROOT / "scripts", scripts_dir)
            result = subprocess.run(
                [str(scripts_dir / "install-hermes-launchers.sh")],
                check=True,
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ["PATH"],
                    "HERMES_SCRIPTS_DIR": str(runtime_dir),
                },
            )

            runtime_script = runtime_dir / "hermes-ai-run.sh"
            self.assertIn(str(runtime_dir), result.stdout)
            self.assertFalse(runtime_script.is_symlink())
            self.assertEqual(runtime_script.resolve().parent, runtime_dir.resolve())

            app_dir_pointer = runtime_dir / "stock-agent-app-dir"
            self.assertEqual(
                app_dir_pointer.read_text(encoding="utf-8").strip(), str(app_dir.resolve())
            )
            self.assertEqual(app_dir_pointer.stat().st_mode & 0o777, 0o600)

            result = subprocess.run(
                [str(runtime_script)],
                check=True,
                capture_output=True,
                text=True,
                env={"PATH": os.environ["PATH"]},
                cwd=runtime_dir,
            )

            self.assertEqual(
                result.stdout.strip(),
                f"{app_dir.resolve()}|runtime-model|ai",
            )

            for name in (
                "hermes-agent-data-run.sh",
                "hermes-portfolio-risk-run.sh",
                "hermes-tracking-run.sh",
            ):
                installed = runtime_dir / name
                self.assertTrue(installed.is_file())
                self.assertFalse(installed.is_symlink())

if __name__ == "__main__":
    unittest.main()
