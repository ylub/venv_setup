#!/usr/bin/env python3
"""Remove a project's .venv directory without touching anything else."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
from pathlib import Path


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"


class AppError(Exception):
    """Raised for expected operational failures."""


class Console:
    def __init__(self, use_color: bool = True):
        self.use_color = use_color and self.supports_color()
        self.styles = {
            "header": "\033[1m\033[38;5;81m",
            "info": "\033[38;5;111m",
            "warn": "\033[1m\033[38;5;179m",
            "error": "\033[1m\033[38;5;174m",
            "ok": "\033[38;5;114m",
            "muted": ANSI_DIM,
            "prompt": "\033[1m\033[38;5;151m",
            "reset": ANSI_RESET,
        }

    @staticmethod
    def supports_color() -> bool:
        if not sys.stdout.isatty():
            return False
        if os.environ.get("NO_COLOR"):
            return False
        term = os.environ.get("TERM", "")
        return term not in {"", "dumb"}

    def style(self, text: str, role: str) -> str:
        if not self.use_color:
            return text
        return f"{self.styles.get(role, '')}{text}{self.styles['reset']}"

    def print(self, text: str = "", role: str | None = None) -> None:
        if role:
            print(self.style(text, role))
        else:
            print(text)

    def header(self, title: str) -> None:
        self.print(f"\n{title}", "header")

    def bullet(self, text: str, role: str | None = None) -> None:
        self.print(f"- {text}", role)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove a project's .venv directory and leave the rest of the project untouched.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 remove_venv.py
              python3 remove_venv.py --folder quiz_up
              python3 remove_venv.py --folders quiz_up time vault
              python3 remove_venv.py --folder alef_beis --dry-run
              python3 remove_venv.py --folder ShulBook --force
            """
        ),
    )
    parser.add_argument(
        "--folder",
        metavar="folder",
        help="Target project folder relative to the current directory. If omitted, use the current directory.",
    )
    parser.add_argument(
        "--folders",
        nargs="+",
        metavar="folder",
        help="Remove .venv from multiple project folders relative to the current directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting it.")
    parser.add_argument("--force", action="store_true", help="Remove .venv without an interactive confirmation prompt.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    return parser


def resolve_project_root(base_dir: Path, folder: str | None) -> Path:
    project_root = (base_dir / folder).resolve() if folder else base_dir.resolve()
    if not project_root.exists():
        raise AppError(f"Project folder not found: {project_root}")
    if not project_root.is_dir():
        raise AppError(f"Target is not a directory: {project_root}")
    return project_root


def resolve_project_roots(base_dir: Path, folder: str | None, folders: list[str] | None) -> list[Path]:
    if folder and folders:
        raise AppError("Use either --folder or --folders, not both.")
    if folders:
        project_roots: list[Path] = []
        seen: set[Path] = set()
        for item in folders:
            project_root = resolve_project_root(base_dir, item)
            if project_root in seen:
                continue
            seen.add(project_root)
            project_roots.append(project_root)
        return project_roots
    return [resolve_project_root(base_dir, folder)]


def prompt_yes_no(console: Console, prompt: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(console.style(f"{prompt} ({suffix}): ", "prompt")).strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        console.print("Please answer y or n.", "warn")


def remove_venv(project_root: Path, args: argparse.Namespace, console: Console) -> int:
    venv_dir = project_root / ".venv"
    req_path = project_root / "requirements.txt"

    console.header("Remove Project Venv")
    console.bullet(f"Project folder: {project_root}")
    console.bullet(f"Target venv: {venv_dir}")
    console.bullet(f"requirements.txt present: {'yes' if req_path.exists() else 'no'}")
    console.bullet("Only .venv will be removed. No project files will be changed.", "info")

    if not venv_dir.exists():
        console.bullet(".venv does not exist. Nothing to remove.", "warn")
        return 0
    if not venv_dir.is_dir():
        raise AppError(f"Expected a directory at {venv_dir}, but found something else.")

    if args.dry_run:
        console.bullet(f"[dry-run] Would remove {venv_dir}", "warn")
        console.bullet("requirements.txt would be kept unchanged.", "ok")
        return 0

    if not args.force:
        confirmed = prompt_yes_no(
            console,
            "Remove this project's .venv now? requirements.txt will be kept",
            default=False,
        )
        if not confirmed:
            console.print("Cancelled. Nothing was removed.", "warn")
            return 1

    shutil.rmtree(venv_dir)
    console.bullet(".venv removed successfully.", "ok")
    console.bullet("requirements.txt was left unchanged.", "ok")
    return 0


def remove_many(project_roots: list[Path], args: argparse.Namespace, console: Console) -> int:
    failures: list[tuple[Path, str]] = []

    for index, project_root in enumerate(project_roots, start=1):
        console.header(f"Project {index}/{len(project_roots)}")
        console.bullet(str(project_root), "info")
        try:
            result = remove_venv(project_root, args, console)
            if result != 0:
                failures.append((project_root, "cancelled"))
        except AppError as exc:
            failures.append((project_root, str(exc)))
            console.print(str(exc), "error")

    if len(project_roots) > 1:
        console.header("Multi-Project Summary")
        console.bullet(f"Projects requested: {len(project_roots)}")
        console.bullet(f"Projects completed: {len(project_roots) - len(failures)}")
        console.bullet(f"Projects failed: {len(failures)}")
        for project_root, reason in failures:
            console.bullet(f"{project_root.name}: {reason}", "warn")

    return 1 if failures else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    console = Console(use_color=not args.no_color)
    base_dir = Path.cwd()

    try:
        project_roots = resolve_project_roots(base_dir, args.folder, args.folders)
        if len(project_roots) > 1:
            return remove_many(project_roots, args, console)
        return remove_venv(project_roots[0], args, console)
    except KeyboardInterrupt:
        console.print("\nInterrupted by user.", "warn")
        return 130
    except AppError as exc:
        console.print(str(exc), "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
