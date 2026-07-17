import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run


class RunLauncherTests(unittest.TestCase):
    def test_no_arguments_or_direct_options_start_ui(self) -> None:
        self.assertEqual(["ui"], run.normalize_cli_args([]))
        self.assertEqual(
            ["ui", "--no-browser", "--port", "9000"],
            run.normalize_cli_args(["--no-browser", "--port", "9000"]),
        )

    def test_existing_cli_subcommands_are_preserved(self) -> None:
        self.assertEqual(
            ["login", "--expired"],
            run.normalize_cli_args(["login", "--expired"]),
        )

    def test_find_venv_python_prefers_project_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)

            self.assertEqual(executable.absolute(), run.find_venv_python(root))

    @unittest.skipIf(os.name == "nt", "Unix virtualenv symlink behavior")
    def test_find_venv_python_preserves_the_virtualenv_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_python = root / "python3.13"
            base_python.write_text("", encoding="utf-8")
            base_python.chmod(0o755)
            executable = root / ".venv" / "bin" / "python"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(base_python)

            selected = run.find_venv_python(root)
            self.assertEqual(executable.absolute(), selected)
            self.assertNotEqual(base_python.resolve(), selected)

    def test_activate_project_venv_reexecutes_with_original_arguments(self) -> None:
        executable = Path("/tmp/project-venv/bin/python")
        with patch.object(run, "find_venv_python", return_value=executable):
            with patch.object(run, "is_current_python", return_value=False):
                with patch.object(run.os, "execv") as execv:
                    run.activate_project_venv(["--no-browser"])

        execv.assert_called_once_with(
            str(executable),
            [str(executable), str(run.ROOT / "run.py"), "--no-browser"],
        )

    def test_runtime_error_rejects_unsupported_python(self) -> None:
        fake_version = (3, 12, 0)
        with patch.object(run.sys, "version_info", fake_version):
            message = run.runtime_error()

        self.assertIn("需要 Python 3.13.x", message)


if __name__ == "__main__":
    unittest.main()
