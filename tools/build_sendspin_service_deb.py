"""Build a bundled-runtime Sendspin service Debian package.

Subcommands
-----------
prepare  Stage the bundled CPython runtime and app directory under build/.
         Requires uv. Safe to cache in CI between runs.
package  Call nfpm to produce the .deb from the staged build directory.
         Requires nfpm. Fast; no Python or uv needed.
build    Run prepare then package in one step (default when omitted).
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import platform
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

PACKAGE_NAME = "sendspin-service"
PYTHON_VERSION = "3.13"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = PROJECT_ROOT / "build" / PACKAGE_NAME
DIST_ROOT = PROJECT_ROOT / "dist"
NFPM_CONFIG = PROJECT_ROOT / "packaging" / "nfpm.yaml"
APP_BUILD_ROOT = BUILD_ROOT / "app"
RUNTIME_ROOT = BUILD_ROOT / "runtime"
BIN_ROOT = BUILD_ROOT / "bin"
DEV_PACKAGE_VERSION = "0.0.0+dev"
UV = shutil.which("uv") or "uv"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageMeta:
    """Debian package metadata passed to nfpm."""

    name: str
    version: str
    architecture: str


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the requested package build step."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    meta = PackageMeta(
        name=PACKAGE_NAME,
        version=_package_version(),
        architecture=args.architecture or _debian_architecture(),
    )

    if args.command in ("prepare", "build"):
        _check_tool("uv")
        if args.clean:
            shutil.rmtree(BUILD_ROOT, ignore_errors=True)
        runtime_python = _uv_python(args.python_version)
        _build_app(runtime_python)
        _prepare_bundle(runtime_python, meta.version)

    if args.command in ("package", "build"):
        _check_tool("nfpm")
        DIST_ROOT.mkdir(exist_ok=True)
        output = _package(meta)
        _print_size_report(output, BUILD_ROOT)

    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the Sendspin service bundled-runtime Debian package",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_shared_args(parser)
    _add_prepare_args(parser)

    shared = argparse.ArgumentParser(add_help=False)
    _add_shared_args(shared)

    prepare_shared = argparse.ArgumentParser(add_help=False)
    _add_prepare_args(prepare_shared)

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "prepare",
        parents=[shared, prepare_shared],
        help="Stage the bundle under build/ (requires uv)",
    )
    subparsers.add_parser(
        "package",
        parents=[shared],
        help="Package the staged bundle into a .deb (requires nfpm)",
    )
    subparsers.add_parser(
        "build",
        parents=[shared, prepare_shared],
        help="Run prepare then package in one step (default)",
    )

    args = parser.parse_args()
    if args.command is None:
        args.command = "build"
    if not hasattr(args, "clean"):
        args.clean = True
    if not hasattr(args, "python_version"):
        args.python_version = PYTHON_VERSION
    return args


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--architecture",
        help="Debian architecture override, for example amd64 or arm64",
    )


def _add_prepare_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-clean",
        action="store_false",
        dest="clean",
        default=True,
        help="Reuse the existing build directory",
    )
    parser.add_argument(
        "--python-version",
        default=PYTHON_VERSION,
        help=f"Python runtime version to bundle, defaults to {PYTHON_VERSION}",
    )


# ---------------------------------------------------------------------------
# Prepare step: runtime + app bundle
# ---------------------------------------------------------------------------


def _build_app(runtime_python: Path) -> None:
    shutil.rmtree(APP_BUILD_ROOT, ignore_errors=True)
    APP_BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    _run(
        [
            UV,
            "pip",
            "install",
            "--python",
            str(runtime_python),
            "--target",
            str(APP_BUILD_ROOT),
            *_service_dependencies(),
        ]
    )
    shutil.copytree(
        PROJECT_ROOT / "sendspin_service",
        APP_BUILD_ROOT / "sendspin_service",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )
    # sendspin_service imports custom_plugins.race_voice.const/services and reads
    # locales.json; without this the service fails at startup (FileNotFoundError
    # on locales.json, well before any request), not just at packaging time.
    # "player" is the RH plugin's own built browser player (a few MiB) -- the
    # service has its own, built separately; excluded here to avoid bundling it.
    shutil.copytree(
        PROJECT_ROOT / "custom_plugins",
        APP_BUILD_ROOT / "custom_plugins",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "player"),
        dirs_exist_ok=True,
    )
    shutil.copytree(
        PROJECT_ROOT / "custom_plugins/race_voice/assets",
        APP_BUILD_ROOT / "sendspin_service/assets",
        dirs_exist_ok=True,
    )


def _uv_python(python_version: str) -> Path:
    _run([UV, "python", "install", python_version])
    result = subprocess.run(  # noqa: S603
        [
            UV,
            "python",
            "find",
            "--no-project",
            "--python-preference",
            "only-managed",
            python_version,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    python = Path(result.stdout.strip())
    if not python.exists():
        message = f"uv did not return an existing Python interpreter: {python}"
        raise RuntimeError(message)
    return python


def _prepare_bundle(runtime_python: Path, package_version: str) -> None:
    shutil.rmtree(RUNTIME_ROOT, ignore_errors=True)
    shutil.copytree(runtime_python.parent.parent, RUNTIME_ROOT, symlinks=True)
    _prune_runtime(RUNTIME_ROOT)
    _prune_app(APP_BUILD_ROOT)
    _remove_cache_files(BUILD_ROOT)
    _write_launcher(BIN_ROOT / PACKAGE_NAME, runtime_python.name, package_version)


def _prune_runtime(runtime_root: Path) -> None:
    for directory in (
        runtime_root / "include",
        runtime_root / "share",
        runtime_root / "lib" / "pkgconfig",
    ):
        shutil.rmtree(directory, ignore_errors=True)
    for pattern in ("tcl*", "tk*", "Tix*", "itcl*", "thread*"):
        for directory in (runtime_root / "lib").glob(pattern):
            shutil.rmtree(directory, ignore_errors=True)

    version_dirs = sorted((runtime_root / "lib").glob("python3.*"))
    for version_dir in version_dirs:
        for directory in (
            "__phello__",
            "ensurepip",
            "idlelib",
            "lib2to3",
            "pydoc_data",
            "test",
            "tkinter",
            "turtledemo",
            "unittest",
            "venv",
        ):
            shutil.rmtree(version_dir / directory, ignore_errors=True)
        for config_dir in version_dir.glob("config-*"):
            shutil.rmtree(config_dir, ignore_errors=True)

    for executable in ("2to3*", "idle*", "pydoc*", "python*-config"):
        for path in (runtime_root / "bin").glob(executable):
            path.unlink(missing_ok=True)


def _prune_app(app_root: Path) -> None:
    for directory_name in ("tests", "_pyinstaller"):
        for directory in app_root.rglob(directory_name):
            shutil.rmtree(directory, ignore_errors=True)


def _remove_cache_files(root: Path) -> None:
    for cache_dir in root.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for compiled_file in root.rglob("*.pyc"):
        compiled_file.unlink(missing_ok=True)


def _write_launcher(target: Path, python_name: str, package_version: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    quoted_version = shlex.quote(package_version)
    target.write_text(
        f"""#!/bin/sh
set -eu
export SENDSPIN_SERVICE_VERSION={quoted_version}
export PYTHONHOME=/opt/{PACKAGE_NAME}/runtime
export PYTHONPATH=/opt/{PACKAGE_NAME}/app
exec /opt/{PACKAGE_NAME}/runtime/bin/{python_name} -m sendspin_service "$@"
""",
        encoding="utf-8",
    )
    target.chmod(0o755)


# ---------------------------------------------------------------------------
# Package step: nfpm
# ---------------------------------------------------------------------------


def _package(meta: PackageMeta) -> Path:
    env = os.environ.copy()
    env["VERSION"] = meta.version
    env["ARCH"] = meta.architecture
    env.setdefault("PYTHONUTF8", "1")
    _run(
        [
            "nfpm",
            "package",
            "--config",
            str(NFPM_CONFIG),
            "--packager",
            "deb",
            "--target",
            str(DIST_ROOT),
        ],
        extra_env=env,
    )
    return DIST_ROOT / f"{meta.name}_{meta.version}_{meta.architecture}.deb"


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _package_version() -> str:
    if version := os.environ.get("SENDSPIN_SERVICE_VERSION"):
        return version.removeprefix("v")
    return DEV_PACKAGE_VERSION


def _service_dependencies() -> list[str]:
    dependencies = _pyproject()["project"]["optional-dependencies"][PACKAGE_NAME]
    return [str(dependency) for dependency in dependencies]


@functools.lru_cache(maxsize=1)
def _pyproject() -> dict[str, object]:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())


def _debian_architecture() -> str:
    dpkg = shutil.which("dpkg")
    if dpkg is not None:
        result = subprocess.run(  # noqa: S603
            [dpkg, "--print-architecture"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    message = f"Cannot map machine architecture to Debian architecture: {machine}"
    raise RuntimeError(message)


def _check_tool(command: str) -> None:
    if shutil.which(command) is None:
        message = f"Required build tool not found: {command}"
        raise RuntimeError(message)


def _directory_size(path: Path) -> int:
    return sum(
        file.lstat().st_size
        for file in path.rglob("*")
        if file.is_file() or file.is_symlink()
    )


def _print_size_report(output: Path, bundle_root: Path) -> None:
    deb_size = output.stat().st_size
    installed_size = _directory_size(bundle_root)
    logger.info("Built: %s", output)
    logger.info(".deb size: %s", _format_bytes(deb_size))
    logger.info("Installed size: %s", _format_bytes(installed_size))


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"  # unreachable, satisfies type checkers


def _run(command: list[str], *, extra_env: dict[str, str] | None = None) -> None:
    logger.info("+ %s", " ".join(command))
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    if extra_env:
        env.update(extra_env)
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)  # noqa: S603


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd)
        message = f"Command failed with exit code {exc.returncode}: {command}"
        raise SystemExit(message) from None
