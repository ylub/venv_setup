#!/usr/bin/env python3
"""Virtual environment setup and maintenance helper for Python projects.

This script scans the current working directory for Python files, extracts
third-party imports with ``ast``, updates ``requirements.txt`` safely, manages a
``.venv`` virtual environment, and installs the detected requirements.

It supports both a step-by-step wizard and normal command-line operation.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EXCLUDED_DIR_NAMES = {"legacy", "backup", "backups", "old", "__pycache__", ".venv"}
IMPORT_TO_PACKAGE = {
    "AppKit": "pyobjc-framework-cocoa",
    "ApplicationServices": "pyobjc-framework-Quartz",
    "AVKit": "pyobjc-framework-AVKit",
    "Cocoa": "pyobjc-framework-cocoa",
    "CoreFoundation": "pyobjc-framework-cocoa",
    "CoreText": "pyobjc-framework-Quartz",
    "cv2": "opencv-python",
    "docx": "python-docx",
    "fitz": "PyMuPDF",
    "Foundation": "pyobjc-framework-cocoa",
    "google.auth": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google.oauth2": "google-auth",
    "googleapiclient": "google-api-python-client",
    "objc": "pyobjc",
    "PIL": "pillow",
    "Quartz": "pyobjc-framework-Quartz",
    "sklearn": "scikit-learn",
    "WebKit": "pyobjc-framework-WebKit",
    "yaml": "PyYAML",
}
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
HELP_TEXT = {
    "mode": (
        "Choose Wizard for guided prompts and explanations. Choose Command-line "
        "mode to run the normal workflow with the supplied flags."
    ),
    "rebuild": "Rebuild deletes the existing .venv directory and recreates it from scratch.",
    "requirements": "requirements.txt will be merged carefully and a diff will be shown before writing.",
    "venv": "The script creates .venv in the current working directory if it does not exist.",
    "install": "Install runs pip upgrade first, then installs from requirements.txt inside .venv.",
}
CACHE_VERSION = 2


@dataclass
class ScanResults:
    python_files: list[Path] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    packages: list[str] = field(default_factory=list)
    local_modules: set[str] = field(default_factory=set)
    parse_errors: list[str] = field(default_factory=list)
    cache_hits: int = 0


@dataclass
class RequirementsPlan:
    path: Path
    before_text: str
    after_text: str
    diff_text: str
    changed: bool
    existed_before: bool


@dataclass
class VenvRecommendation:
    should_create: bool
    confidence: str
    summary: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class WizardState:
    current_step: str = "scan"
    previous_step: str | None = None
    selected_options: dict[str, Any] = field(default_factory=dict)
    scan_results: ScanResults | None = None
    planned_actions: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)

    def move_to(self, step: str) -> None:
        if self.current_step:
            self.history.append(self.current_step)
            self.previous_step = self.current_step
        self.current_step = step

    def go_back(self) -> bool:
        if not self.history:
            return False
        self.previous_step = self.current_step
        self.current_step = self.history.pop()
        return True

    def restart(self) -> None:
        self.previous_step = self.current_step
        self.current_step = "scan"
        self.selected_options.clear()
        self.scan_results = None
        self.planned_actions.clear()
        self.history.clear()


class AppError(Exception):
    """Raised for expected operational failures."""


class UserAbort(Exception):
    """Raised when the user quits or declines a required action."""


class WizardBack(Exception):
    """Raised to move the wizard to the previous step."""


class WizardRestart(Exception):
    """Raised to restart the wizard from the beginning."""


class Console:
    """Small terminal formatter using ANSI slots that respect Terminal/iTerm themes."""

    def __init__(self, use_color: bool = True):
        self.use_color = use_color and self.supports_color()
        self.styles = self._build_styles()

    @staticmethod
    def supports_color() -> bool:
        if not sys.stdout.isatty():
            return False
        if os.environ.get("NO_COLOR"):
            return False
        term = os.environ.get("TERM", "")
        return term not in {"", "dumb"}

    def _build_styles(self) -> dict[str, str]:
        palette = self._terminal_palette()
        if palette:
            return {
                "header": f"{ANSI_BOLD}{self._ansi_rgb(palette['bold'])}",
                "section": f"{ANSI_BOLD}{self._ansi_rgb(palette['cursor'])}",
                "info": self._ansi_rgb(palette["normal"]),
                "warn": f"{ANSI_BOLD}{self._ansi_rgb(palette['selection'])}",
                "error": f"{ANSI_BOLD}{self._ansi_rgb(palette['selection'])}",
                "ok": self._ansi_rgb(palette["cursor"]),
                "muted": ANSI_DIM,
                "prompt": f"{ANSI_BOLD}{self._ansi_rgb(palette['bold'])}",
                "reset": ANSI_RESET,
            }
        return {
            "header": "\033[1m\033[38;5;81m",
            "section": "\033[1m\033[38;5;110m",
            "info": "\033[38;5;111m",
            "warn": "\033[1m\033[38;5;179m",
            "error": "\033[1m\033[38;5;174m",
            "ok": "\033[38;5;114m",
            "muted": ANSI_DIM,
            "prompt": "\033[1m\033[38;5;151m",
            "reset": ANSI_RESET,
        }

    @staticmethod
    def _ansi_rgb(rgb: tuple[int, int, int]) -> str:
        red, green, blue = rgb
        return f"\033[38;2;{red};{green};{blue}m"

    @staticmethod
    def _run_osascript(script: str) -> str:
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    @staticmethod
    def _parse_triplets(raw: str) -> list[tuple[int, int, int]]:
        triplets: list[tuple[int, int, int]] = []
        for chunk in raw.replace("{", "").split("},"):
            values = [value.strip(" }") for value in chunk.split(",") if value.strip(" }")]
            if len(values) < 3:
                continue
            try:
                numbers = [int(value) for value in values[:3]]
            except ValueError:
                continue
            triplets.append(tuple(max(0, min(255, round(number / 65535 * 255))) for number in numbers))
        return triplets

    def _terminal_palette(self) -> dict[str, tuple[int, int, int]] | None:
        if not self.use_color or platform.system() != "Darwin":
            return None

        term_program = os.environ.get("TERM_PROGRAM", "")
        raw = ""
        if term_program == "Apple_Terminal":
            raw = self._run_osascript(
                'tell application "Terminal" to get {normal text color, bold text color, '
                'cursor color, selected text color} of current settings of selected tab of front window'
            )
        elif term_program == "iTerm.app":
            raw = self._run_osascript(
                'tell application "iTerm2" to tell current session of current window to get '
                '{foreground color, bold color, cursor color, selection color}'
            )

        colors = self._parse_triplets(raw)
        if len(colors) < 4:
            return None
        return {
            "normal": colors[0],
            "bold": colors[1],
            "cursor": colors[2],
            "selection": colors[3],
        }

    def style(self, text: str, role: str) -> str:
        if not self.use_color:
            return text
        return f"{self.styles.get(role, '')}{text}{self.styles['reset']}"

    def print(self, text: str = "", role: str | None = None) -> None:
        if role:
            print(self.style(text, role))
        else:
            print(text)

    def section(self, title: str) -> None:
        self.print(f"\n== {title} ==", "section")

    def header(self, title: str) -> None:
        self.print(f"\n{title}", "header")

    def bullet(self, text: str, role: str | None = None) -> None:
        self.print(f"- {text}", role)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan a project for third-party imports, update requirements.txt, and manage .venv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python3 venv_setup.py
              python3 venv_setup.py --dry-run
              python3 venv_setup.py --dry-run --req
              python3 venv_setup.py --check
              python3 venv_setup.py --check --req
              python3 venv_setup.py --folders quiz_up cipher_suite_plus
              python3 venv_setup.py --rebuild --force
              python3 venv_setup.py --no-color
            """
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing files or installing.")
    parser.add_argument("--rebuild", action="store_true", help="Delete and recreate .venv after confirmation.")
    parser.add_argument("--force", action="store_true", help="Write requirements.txt without prompting after the diff.")
    parser.add_argument("--check", action="store_true", help="Show current environment status and exit.")
    parser.add_argument(
        "--req",
        action="store_true",
        help="Write requirements.txt even when --dry-run or --check is used.",
    )
    parser.add_argument(
        "--folders",
        nargs="+",
        metavar="folder",
        help="Run the setup flow for one or more project folders relative to the current directory.",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color output.")
    return parser


def current_project_root() -> Path:
    return Path.cwd().resolve()


def resolve_project_roots(base_dir: Path, folders: list[str] | None) -> list[Path]:
    if not folders:
        return [base_dir]

    project_roots: list[Path] = []
    seen: set[Path] = set()
    for folder in folders:
        candidate = (base_dir / folder).resolve()
        if not candidate.exists():
            raise AppError(f"Folder not found: {folder}")
        if not candidate.is_dir():
            raise AppError(f"Not a directory: {folder}")
        if candidate in seen:
            continue
        seen.add(candidate)
        project_roots.append(candidate)
    return project_roots


def cache_path(project_root: Path) -> Path:
    return project_root / ".venv_setup_cache.json"


def is_hidden_directory(path: Path) -> bool:
    return path.name.startswith(".")


def should_skip_dir(path: Path) -> bool:
    if path.name in EXCLUDED_DIR_NAMES:
        return True
    if is_hidden_directory(path):
        return True
    return False


def load_scan_cache(project_root: Path) -> dict[str, Any]:
    path = cache_path(project_root)
    if not path.exists():
        return {"files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"files": {}}
    if data.get("cache_version") != CACHE_VERSION:
        return {"files": {}}
    return data


def save_scan_cache(project_root: Path, cache: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    try:
        cache["cache_version"] = CACHE_VERSION
        cache_path(project_root).write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        # Cache failures should never block the main workflow.
        pass


def scan_python_files(project_root: Path) -> list[Path]:
    results: list[Path] = []
    stack = [project_root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if should_skip_dir(entry_path):
                            continue
                        stack.append(entry_path)
                        continue
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
                        results.append(entry_path)
        except OSError:
            continue
    results.sort()
    return results


def module_is_stdlib(name: str) -> bool:
    try:
        return name in sys.stdlib_module_names
    except AttributeError:
        return False


def extract_imports_from_ast(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                continue
            if node.module:
                imports.add(node.module)
    return imports


def parse_imports_for_file(file_path: Path, cached_entry: dict[str, Any] | None) -> tuple[set[str], str | None, bool]:
    try:
        stat = file_path.stat()
    except OSError as exc:
        return set(), f"{file_path}: could not stat file ({exc})", False

    file_key = str(file_path)
    cache_matches = (
        cached_entry
        and cached_entry.get("mtime_ns") == stat.st_mtime_ns
        and cached_entry.get("size") == stat.st_size
        and isinstance(cached_entry.get("imports"), list)
    )
    if cache_matches:
        return set(cached_entry["imports"]), None, True

    try:
        source = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return set(), f"{file_path}: could not read file ({exc})", False
    except OSError as exc:
        return set(), f"{file_path}: could not read file ({exc})", False

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        return set(), f"{file_path}: syntax error on line {exc.lineno}", False

    imports = extract_imports_from_ast(tree)
    return imports, None, False


def detect_local_modules(project_root: Path, python_files: list[Path]) -> set[str]:
    local_modules: set[str] = set()

    for file_path in python_files:
        local_modules.add(file_path.stem)
        try:
            relative_parts = file_path.relative_to(project_root).parts
        except ValueError:
            continue

        if len(relative_parts) == 1 and file_path.suffix == ".py":
            local_modules.add(file_path.stem)

    for dirpath, dirnames, filenames in os.walk(project_root):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not should_skip_dir(current / name)]
        if "__init__.py" in filenames:
            try:
                rel = current.relative_to(project_root)
            except ValueError:
                continue
            if rel.parts:
                local_modules.add(rel.parts[0])

    # Also treat simple sibling projects as local modules when the workspace is
    # organized as peer folders such as ``quiz_up/`` and ``express/`` and the
    # code imports them via a manual ``sys.path`` insertion.
    parent_dir = project_root.parent
    try:
        siblings = list(parent_dir.iterdir())
    except OSError:
        siblings = []
    for sibling in siblings:
        if sibling == project_root or not sibling.is_dir():
            continue
        if should_skip_dir(sibling):
            continue
        sibling_module = sibling / f"{sibling.name}.py"
        sibling_package = sibling / "__init__.py"
        if sibling_module.exists() or sibling_package.exists():
            local_modules.add(sibling.name)

    return local_modules


def resolve_packages(imports: set[str], local_modules: set[str]) -> list[str]:
    packages = set()
    for import_name in sorted(imports, key=str.casefold):
        root_name = import_name.split(".")[0]
        if module_is_stdlib(root_name):
            continue
        if root_name in local_modules:
            continue
        package_name = None
        parts = import_name.split(".")
        for stop in range(len(parts), 0, -1):
            candidate = ".".join(parts[:stop])
            if candidate in IMPORT_TO_PACKAGE:
                package_name = IMPORT_TO_PACKAGE[candidate]
                break
        if package_name is None:
            package_name = IMPORT_TO_PACKAGE.get(root_name, root_name)
        packages.add(package_name)
    return sorted(packages, key=str.casefold)


def analyze_project(project_root: Path, console: Console, dry_run: bool) -> ScanResults:
    results = ScanResults()
    cached_data = load_scan_cache(project_root)
    next_cache: dict[str, Any] = {"files": {}}

    console.section("Scan")
    console.bullet(f"Project root: {project_root}")
    results.python_files = scan_python_files(project_root)
    results.local_modules = detect_local_modules(project_root, results.python_files)
    console.bullet(f"Python files found: {len(results.python_files)}")
    console.bullet(f"Local project modules detected: {len(results.local_modules)}")

    console.section("Imports")
    for file_path in results.python_files:
        cached_entry = cached_data.get("files", {}).get(str(file_path))
        file_imports, error, cache_hit = parse_imports_for_file(file_path, cached_entry)
        if cache_hit:
            results.cache_hits += 1
        if error:
            results.parse_errors.append(error)
            continue
        results.imports.update(file_imports)
        try:
            stat = file_path.stat()
        except OSError:
            continue
        next_cache["files"][str(file_path)] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "imports": sorted(file_imports),
        }

    results.packages = resolve_packages(results.imports, results.local_modules)
    console.bullet(f"Unique third-party imports: {len(results.packages)}")
    console.bullet(f"Cached import parses reused: {results.cache_hits}")

    if results.parse_errors:
        console.print("Some files could not be parsed cleanly:", "warn")
        for error in results.parse_errors[:10]:
            console.bullet(error, "warn")
        if len(results.parse_errors) > 10:
            console.bullet(f"... and {len(results.parse_errors) - 10} more parse issues.", "warn")

    save_scan_cache(project_root, next_cache, dry_run=dry_run)
    return results


def normalize_requirement_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def parse_requirement_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("-"):
        return None
    for marker in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";"):
        if marker in stripped:
            return normalize_requirement_name(stripped.split(marker, 1)[0])
    return normalize_requirement_name(stripped)


def requirement_entries(text: str) -> list[str]:
    return [line for line in text.splitlines() if parse_requirement_line(line)]


def merge_requirements(existing_text: str, detected_packages: list[str], local_modules: set[str]) -> str:
    existing_lines = existing_text.splitlines()
    preserved_specs: dict[str, str] = {}
    manual_lines: list[str] = []

    for line in existing_lines:
        package_name = parse_requirement_line(line)
        if package_name:
            preserved_specs[package_name] = line.strip()
        else:
            manual_lines.append(line.rstrip())

    merged_lines = []
    detected_normalized = {normalize_requirement_name(package) for package in detected_packages}
    local_module_normalized = {normalize_requirement_name(module) for module in local_modules}
    import_alias_to_package = {
        normalize_requirement_name(alias): normalize_requirement_name(package)
        for alias, package in IMPORT_TO_PACKAGE.items()
    }
    for package in sorted(detected_packages, key=str.casefold):
        normalized = normalize_requirement_name(package)
        merged_lines.append(preserved_specs.get(normalized, package))

    def should_preserve_extra(name: str) -> bool:
        if name in detected_normalized or name in local_module_normalized:
            return False
        mapped_package = import_alias_to_package.get(name)
        if mapped_package and mapped_package in detected_normalized:
            return False
        # Drop namespace placeholder entries when concrete Google packages were
        # actually detected from the imports.
        if name == "google" and any(pkg.startswith("google-") for pkg in detected_normalized):
            return False
        return True

    extra_manual_packages = [
        spec
        for name, spec in preserved_specs.items()
        if should_preserve_extra(name)
    ]
    merged_lines.extend(sorted(extra_manual_packages, key=str.casefold))

    final_lines: list[str] = []
    if manual_lines:
        while manual_lines and not manual_lines[0].strip():
            final_lines.append(manual_lines.pop(0))
        final_lines.extend(manual_lines)
        if final_lines and final_lines[-1].strip():
            final_lines.append("")
    final_lines.extend(merged_lines)

    normalized_text = "\n".join(final_lines).rstrip() + "\n"
    if not normalized_text.strip():
        normalized_text = ""
    return normalized_text


def build_requirements_plan(project_root: Path, packages: list[str]) -> RequirementsPlan:
    req_path = project_root / "requirements.txt"
    existed_before = req_path.exists()
    before_text = req_path.read_text(encoding="utf-8") if existed_before else ""
    local_modules = detect_local_modules(project_root, scan_python_files(project_root))
    after_text = merge_requirements(before_text, packages, local_modules)
    changed = before_text != after_text
    diff_text = "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile="requirements.txt (current)",
            tofile="requirements.txt (proposed)",
        )
    )
    return RequirementsPlan(
        path=req_path,
        before_text=before_text,
        after_text=after_text,
        diff_text=diff_text,
        changed=changed,
        existed_before=existed_before,
    )


def assess_venv_value(
    scan_results: ScanResults,
    requirements_plan: RequirementsPlan,
    venv_exists: bool,
    active_venv: str | None,
) -> VenvRecommendation:
    reasons: list[str] = []
    score = 0

    package_count = len(scan_results.packages)
    requirement_lines = requirement_entries(requirements_plan.after_text)

    if package_count:
        score += 3
        reasons.append(f"detected {package_count} third-party package{'s' if package_count != 1 else ''}")
    if requirement_lines:
        score += 2
        reasons.append("requirements.txt contains installable packages")
    if len(scan_results.python_files) >= 3:
        score += 1
        reasons.append(f"project has {len(scan_results.python_files)} Python scripts")
    if venv_exists:
        score += 1
        reasons.append(".venv already exists, so maintaining it is consistent")
    if active_venv:
        reasons.append("your current shell is already using a virtual environment")

    if score >= 3:
        return VenvRecommendation(
            should_create=True,
            confidence="high",
            summary="A project-local virtual environment is worth it here.",
            reasons=reasons,
        )
    if score >= 1:
        return VenvRecommendation(
            should_create=True,
            confidence="medium",
            summary="A virtual environment is probably worth using for this project.",
            reasons=reasons,
        )
    return VenvRecommendation(
        should_create=False,
        confidence="low",
        summary="A virtual environment is optional here unless you want strict project isolation.",
        reasons=reasons or ["no third-party packages were detected"],
    )


def write_requirements(plan: RequirementsPlan, dry_run: bool, force_write: bool = False) -> bool:
    needs_write = plan.changed or (force_write and not plan.existed_before)
    if (dry_run and not force_write) or not needs_write:
        return False
    plan.path.write_text(plan.after_text, encoding="utf-8")
    return True


def venv_paths(project_root: Path) -> tuple[Path, Path]:
    venv_dir = project_root / ".venv"
    if os.name == "nt":
        python_path = venv_dir / "Scripts" / "python.exe"
    else:
        python_path = venv_dir / "bin" / "python"
    return venv_dir, python_path


def prompt_yes_no(console: Console, prompt: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        response = input(console.style(f"{prompt} ({suffix}): ", "prompt")).strip().lower()
        if not response:
            return default
        if response in {"y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        console.print("Please answer y or n.", "warn")


def delete_existing_venv(
    venv_dir: Path,
    console: Console,
    dry_run: bool,
    confirm_callback: Any | None = None,
) -> bool:
    if not venv_dir.exists():
        return False
    console.print(f"Warning: {venv_dir} will be deleted.", "warn")
    confirmer = confirm_callback or (lambda prompt, default=False: prompt_yes_no(console, prompt, default=default))
    if not confirmer("Continue with rebuild?", default=False):
        raise UserAbort("Rebuild cancelled by user.")
    if dry_run:
        console.bullet(f"[dry-run] Would delete {venv_dir}")
        return False
    shutil.rmtree(venv_dir)
    return True


def ensure_venv(
    project_root: Path,
    console: Console,
    dry_run: bool,
    rebuild: bool,
    confirm_callback: Any | None = None,
) -> tuple[Path, Path, list[str]]:
    actions: list[str] = []
    venv_dir, python_path = venv_paths(project_root)
    console.section("Venv")
    console.bullet(f"Target venv: {venv_dir}")

    if rebuild:
        deleted = delete_existing_venv(venv_dir, console, dry_run=dry_run, confirm_callback=confirm_callback)
        if deleted:
            actions.append("deleted existing .venv")

    if venv_dir.exists():
        console.bullet(".venv already exists.")
        return venv_dir, python_path, actions

    if dry_run:
        console.bullet("[dry-run] Would create .venv")
        actions.append("would create .venv")
        return venv_dir, python_path, actions

    console.bullet("Creating .venv with the standard library venv module.")
    builder = venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
    builder.create(venv_dir)
    actions.append("created .venv")
    return venv_dir, python_path, actions


def run_subprocess(command: list[str], console: Console, dry_run: bool) -> tuple[int, str]:
    if dry_run:
        console.bullet(f"[dry-run] Would run: {' '.join(command)}")
        return 0, ""
    result = subprocess.run(command, capture_output=True, text=True)
    output = "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)
    return result.returncode, output


def run_captured(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def install_requirements(venv_python: Path, requirements_path: Path, console: Console, dry_run: bool) -> list[str]:
    actions: list[str] = []
    console.section("Install")

    if not requirements_path.exists():
        raise AppError(f"requirements.txt not found at {requirements_path}")
    if not venv_python.exists() and not dry_run:
        raise AppError(f"Virtual environment python not found at {venv_python}")

    for command, label in [
        ([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], "upgraded pip"),
        ([str(venv_python), "-m", "pip", "install", "-r", str(requirements_path)], "installed requirements"),
    ]:
        console.bullet(f"Running: {' '.join(command)}")
        return_code, output = run_subprocess(command, console, dry_run=dry_run)
        if return_code != 0:
            raise AppError(output or f"Command failed: {' '.join(command)}")
        if output:
            console.print(output)
        actions.append(label if not dry_run else f"would {label}")
    return actions


def detect_active_venv() -> str | None:
    return os.environ.get("VIRTUAL_ENV")


def print_venv_recommendation(console: Console, recommendation: VenvRecommendation) -> None:
    role = "ok" if recommendation.should_create else "info"
    console.section("Venv Fit")
    console.print(
        f"Recommendation ({recommendation.confidence} confidence): {recommendation.summary}",
        role,
    )
    for reason in recommendation.reasons:
        console.bullet(reason, "muted")


def summarize_check(
    project_root: Path,
    console: Console,
    scan_results: ScanResults | None = None,
    requirements_plan: RequirementsPlan | None = None,
) -> int:
    console.section("Check")
    venv_dir, venv_python = venv_paths(project_root)
    active_venv = detect_active_venv()
    if active_venv:
        console.print(f"Warning: already inside a virtual environment: {active_venv}", "warn")

    console.bullet(f"Active Python: {sys.executable}")
    console.bullet(f"Target venv python: {venv_python}")
    console.bullet(f"Target venv exists: {'yes' if venv_dir.exists() else 'no'}")

    if not venv_python.exists():
        console.bullet("pip version: unavailable (.venv does not exist)", "warn")
        console.bullet("installed packages: unavailable (.venv does not exist)", "warn")
        if scan_results and requirements_plan:
            recommendation = assess_venv_value(scan_results, requirements_plan, venv_dir.exists(), active_venv)
            print_venv_recommendation(console, recommendation)
        return 0

    code, pip_version = run_subprocess([str(venv_python), "-m", "pip", "--version"], console, dry_run=False)
    if code == 0 and pip_version:
        console.bullet(f"pip version: {pip_version}")

    result = run_captured([str(venv_python), "-m", "pip", "list", "--format=json"])
    installed_names: set[str] = set()
    stderr_text = result.stderr.strip()
    if stderr_text:
        console.bullet(f"pip list warning: {stderr_text}", "muted")
    if result.returncode == 0 and result.stdout.strip():
        try:
            installed = json.loads(result.stdout)
            installed_names = {normalize_requirement_name(item["name"]) for item in installed if "name" in item}
            console.bullet(f"installed packages: {len(installed_names)}")
        except json.JSONDecodeError:
            console.bullet("installed packages: unable to parse pip list output", "warn")

    req_path = project_root / "requirements.txt"
    missing: list[str] = []
    if req_path.exists():
        for line in req_path.read_text(encoding="utf-8").splitlines():
            name = parse_requirement_line(line)
            if name and name not in installed_names:
                missing.append(name)
    console.bullet(f"missing packages: {', '.join(missing) if missing else 'none'}")

    if scan_results and requirements_plan:
        recommendation = assess_venv_value(scan_results, requirements_plan, venv_dir.exists(), active_venv)
        print_venv_recommendation(console, recommendation)
    return 0


def confirm_write_requirements(
    plan: RequirementsPlan,
    console: Console,
    force: bool,
    dry_run: bool,
    force_req: bool = False,
) -> bool:
    console.section("Requirements")
    if not plan.changed and not (force_req and not plan.existed_before):
        console.bullet("requirements.txt is already up to date.")
        return False

    if plan.diff_text:
        console.print(plan.diff_text)
    elif not plan.existed_before and force_req:
        console.bullet("requirements.txt will be created as an empty file.")
    else:
        console.bullet("requirements.txt will be created.")

    if dry_run and not force_req:
        console.bullet("[dry-run] Would update requirements.txt")
        return False

    if force or force_req:
        return True

    return prompt_yes_no(console, "Write the proposed requirements.txt?", default=False)


def print_summary(
    console: Console,
    scan_results: ScanResults,
    actions: list[str],
    recommendation: VenvRecommendation | None = None,
) -> None:
    console.section("Summary")
    console.bullet(f"Scripts scanned: {len(scan_results.python_files)}")
    console.bullet(f"Packages detected: {len(scan_results.packages)}")
    console.bullet(f"Actions taken: {', '.join(actions) if actions else 'none'}")
    if recommendation:
        console.bullet(
            f"Venv recommendation: {'yes' if recommendation.should_create else 'optional'} "
            f"({recommendation.confidence} confidence)"
        )


def interactive_nav_prompt(console: Console, prompt: str, help_key: str) -> str:
    while True:
        raw = input(console.style(f"{prompt}: ", "prompt")).strip()
        lowered = raw.lower()
        if lowered == "help":
            console.print(HELP_TEXT.get(help_key, "No extra help is available for this step."), "info")
            continue
        if lowered == "quit":
            raise UserAbort("Wizard exited safely.")
        if lowered == "back":
            raise WizardBack()
        if lowered == "restart":
            raise WizardRestart()
        return raw


def wizard_yes_no(console: Console, prompt: str, help_key: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        response = interactive_nav_prompt(console, f"{prompt} ({suffix})", help_key)
        lowered = response.lower()
        if not lowered:
            return default
        if lowered in {"y", "yes"}:
            return True
        if lowered in {"n", "no"}:
            return False
        console.print("Please answer y or n, or use help/back/restart/quit.", "warn")


def choose_mode(console: Console) -> str:
    console.header("Choose mode")
    console.print("1. Wizard")
    console.print("2. Command-line / flags mode")
    while True:
        response = interactive_nav_prompt(console, "Enter 1 or 2", "mode")
        if response in {"1", "2"}:
            return response
        console.print("Please enter 1 or 2.", "warn")


def run_wizard(project_root: Path, args: argparse.Namespace, console: Console) -> int:
    state = WizardState()
    console.header("Virtual Environment Setup Wizard")
    console.print("Type help, back, restart, or quit at any prompt.", "info")

    while True:
        try:
            if state.current_step == "scan":
                console.section("Scan")
                console.print("The wizard will scan this project for Python files and analyze imports.", "info")
                proceed = wizard_yes_no(console, "Start the project scan?", "mode", default=True)
                if not proceed:
                    raise UserAbort("Wizard cancelled before scanning.")
                state.scan_results = analyze_project(project_root, console, dry_run=args.dry_run)
                state.selected_options["rebuild"] = wizard_yes_no(
                    console,
                    "Rebuild the virtual environment if it already exists?",
                    "rebuild",
                    default=False,
                )
                state.move_to("requirements")
                continue

            if state.current_step == "requirements":
                assert state.scan_results is not None
                plan = build_requirements_plan(project_root, state.scan_results.packages)
                console.section("Requirements")
                console.print("Next, the wizard prepares requirements.txt from the detected packages.", "info")
                if plan.changed:
                    console.print(plan.diff_text or "requirements.txt will be created.")
                else:
                    console.bullet("requirements.txt is already up to date.")
                should_write = wizard_yes_no(
                    console,
                    "Write the proposed requirements.txt?",
                    "requirements",
                    default=not args.dry_run,
                )
                state.selected_options["write_requirements"] = should_write
                state.selected_options["requirements_plan"] = plan
                state.move_to("venv")
                continue

            if state.current_step == "venv":
                console.section("Venv")
                console.print("The wizard can create or reuse .venv in the current project folder.", "info")
                create_venv = wizard_yes_no(console, "Create or maintain .venv now?", "venv", default=True)
                state.selected_options["manage_venv"] = create_venv
                state.move_to("install")
                continue

            if state.current_step == "install":
                console.section("Install")
                console.print("If you continue, pip will be upgraded and requirements will be installed into .venv.", "info")
                install_now = wizard_yes_no(console, "Install packages into .venv?", "install", default=True)
                state.selected_options["install"] = install_now
                state.move_to("run")
                continue

            if state.current_step == "run":
                return execute_setup(project_root, args, console, wizard_state=state)

            raise AppError(f"Unknown wizard step: {state.current_step}")

        except WizardBack:
            if not state.go_back():
                console.print("Already at the first step.", "warn")
        except WizardRestart:
            console.print("Restarting the wizard from the beginning.", "warn")
            state.restart()


def execute_setup(
    project_root: Path,
    args: argparse.Namespace,
    console: Console,
    wizard_state: WizardState | None = None,
) -> int:
    actions: list[str] = []

    active_venv = detect_active_venv()
    if active_venv:
        console.print(f"Warning: current shell is already inside a virtual environment: {active_venv}", "warn")

    venv_dir, _ = venv_paths(project_root)

    scan_results = wizard_state.scan_results if wizard_state and wizard_state.scan_results else analyze_project(
        project_root,
        console,
        dry_run=args.dry_run,
    )

    plan = (
        wizard_state.selected_options.get("requirements_plan")
        if wizard_state and "requirements_plan" in wizard_state.selected_options
        else build_requirements_plan(project_root, scan_results.packages)
    )
    assert isinstance(plan, RequirementsPlan)
    recommendation = assess_venv_value(scan_results, plan, venv_dir.exists(), active_venv)

    should_write = (
        wizard_state.selected_options.get("write_requirements")
        if wizard_state
        else confirm_write_requirements(plan, console, force=args.force, dry_run=args.dry_run, force_req=args.req)
    )
    if should_write:
        wrote = write_requirements(plan, dry_run=args.dry_run, force_write=args.req)
        actions.append("updated requirements.txt" if wrote else "would update requirements.txt")

    manage_venv = wizard_state.selected_options.get("manage_venv", True) if wizard_state else True
    install_now = wizard_state.selected_options.get("install", True) if wizard_state else True
    rebuild = wizard_state.selected_options.get("rebuild", args.rebuild) if wizard_state else args.rebuild

    if manage_venv:
        confirm_callback = None
        if wizard_state:
            confirm_callback = lambda prompt, default=False: wizard_yes_no(console, prompt, "rebuild", default=default)
        _, venv_python, venv_actions = ensure_venv(
            project_root,
            console,
            dry_run=args.dry_run,
            rebuild=rebuild,
            confirm_callback=confirm_callback,
        )
        actions.extend(venv_actions)
    else:
        _, venv_python = venv_paths(project_root)

    if install_now:
        planned_requirements = requirement_entries(plan.after_text)
        if not planned_requirements and not (project_root / "requirements.txt").exists():
            console.section("Install")
            console.bullet("Skipping install because no third-party requirements were detected.")
            actions.append("skipped install (no requirements)")
        elif not planned_requirements and (project_root / "requirements.txt").exists():
            console.section("Install")
            console.bullet("Skipping install because requirements.txt has no installable package entries.")
            actions.append("skipped install (empty requirements)")
        elif wizard_state and not wizard_yes_no(console, "Proceed with package installation now?", "install", default=True):
            console.bullet("Skipped installation at user request.")
            actions.append("skipped install")
        else:
            install_actions = install_requirements(
                venv_python,
                project_root / "requirements.txt",
                console,
                dry_run=args.dry_run,
            )
            actions.extend(install_actions)

    if args.dry_run:
        print_venv_recommendation(console, recommendation)
    print_summary(console, scan_results, actions, recommendation=recommendation if args.dry_run else None)
    return 0


def run_cli(project_root: Path, args: argparse.Namespace, console: Console) -> int:
    if args.check:
        scan_results = analyze_project(project_root, console, dry_run=args.dry_run)
        plan = build_requirements_plan(project_root, scan_results.packages)
        if args.req:
            should_write = confirm_write_requirements(
                plan,
                console,
                force=args.force,
                dry_run=args.dry_run,
                force_req=True,
            )
            if should_write:
                write_requirements(plan, dry_run=args.dry_run, force_write=True)
        return summarize_check(project_root, console, scan_results=scan_results, requirements_plan=plan)
    return execute_setup(project_root, args, console)


def run_many_projects(project_roots: list[Path], args: argparse.Namespace, console: Console) -> int:
    failures: list[tuple[Path, str]] = []

    for index, project_root in enumerate(project_roots, start=1):
        console.header(f"Project {index}/{len(project_roots)}")
        console.bullet(str(project_root), "info")
        try:
            run_cli(project_root, args, console)
        except UserAbort as exc:
            failures.append((project_root, str(exc)))
            console.print(str(exc), "warn")
        except AppError as exc:
            failures.append((project_root, str(exc)))
            console.print(str(exc), "error")

    if len(project_roots) > 1:
        console.section("Multi-Project Summary")
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
    base_dir = current_project_root()
    project_roots = resolve_project_roots(base_dir, args.folders)

    try:
        if args.folders:
            return run_many_projects(project_roots, args, console)
        project_root = project_roots[0]
        if len(sys.argv) == 1 and sys.stdin.isatty():
            mode = choose_mode(console)
            if mode == "1":
                return run_wizard(project_root, args, console)
            return run_cli(project_root, args, console)
        return run_cli(project_root, args, console)
    except KeyboardInterrupt:
        console.print("\nInterrupted by user.", "warn")
        return 130
    except UserAbort as exc:
        console.print(str(exc), "warn")
        return 1
    except AppError as exc:
        console.print(str(exc), "error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
