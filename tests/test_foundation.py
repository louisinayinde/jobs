"""Tests fondation & imports (US-1.1.T).

Vérifie que le projet s'installe et se charge réellement, pas juste qu'il existe.
"""

import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PACKAGES = {"adapters", "core", "cv", "board"}


def test_install_in_clean_venv_exits_zero(tmp_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--no-input", str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_each_package_imports_without_side_effects() -> None:
    for package in EXPECTED_PACKAGES:
        result = subprocess.run(
            [sys.executable, "-c", f"import src.{package}"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == ""
        assert result.stderr == ""


def test_cli_help_shows_usage_and_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_expected_package_directories_exist() -> None:
    src_dir = REPO_ROOT / "src"
    actual_packages = {
        entry.name
        for entry in src_dir.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    }

    assert actual_packages == EXPECTED_PACKAGES

    assert (REPO_ROOT / "config").is_dir()
    assert (REPO_ROOT / ".github" / "workflows").is_dir()
