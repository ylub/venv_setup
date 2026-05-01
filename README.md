# Venv Setup

Utilities for creating, maintaining, checking, and removing project-local Python virtual environments (.venv) in a shared project workspace.

Note: this utility set was developed largely with the Codex CLI.

## Files

- [`venv_setup.py`](venv_setup.py)
  Scans project Python files, builds or updates `requirements.txt`, creates or rebuilds `.venv`, and installs requirements.
- [`remove_venv.py`](remove_venv.py)
  Removes `.venv` from one or more project folders without changing `requirements.txt` or other project files.

## `venv_setup.py`

### What it does

- Recursively scans a target project for `.py` files
- Uses `ast` import parsing instead of regex
- Filters out standard-library imports
- Detects local project modules and sibling workspace modules so they are not written as pip requirements
- Maps common import names to pip package names, including:
  - `docx -> python-docx`
  - `PIL -> pillow`
  - `fitz -> PyMuPDF`
  - `cv2 -> opencv-python`
  - `yaml -> PyYAML`
  - `sklearn -> scikit-learn`
  - `googleapiclient -> google-api-python-client`
  - `google.auth` / `google.oauth2 -> google-auth`
  - `google_auth_oauthlib -> google-auth-oauthlib`
  - common macOS PyObjC imports such as `AppKit`, `Foundation`, and `WebKit`
- Merges and updates `requirements.txt`
- Creates or reuses `.venv`
- Upgrades pip inside `.venv`
- Installs `requirements.txt`
- Supports a wizard mode and a normal CLI mode
- Supports running against multiple sibling projects in one command

### Single-project examples

Run from inside a project folder:

```bash
cd /path/to/project
python3 /path/to/venv_setup/venv_setup.py
```

Useful variants:

```bash
python3 /path/to/venv_setup/venv_setup.py --dry-run
python3 /path/to/venv_setup/venv_setup.py --check
python3 /path/to/venv_setup/venv_setup.py --check --req
python3 /path/to/venv_setup/venv_setup.py --rebuild --force
```

### Multi-project examples

Run from a parent workspace directory that contains both `venv_setup/` and the target projects:

```bash
cd /path/to/workspace
python3 venv_setup/venv_setup.py --folders project_alpha project_beta
python3 venv_setup/venv_setup.py --folders project_alpha project_beta project_gamma --dry-run
python3 venv_setup/venv_setup.py --folders project_alpha project_beta --check
```

### Notes

- `--req` allows `requirements.txt` to be written even during `--dry-run` or `--check`.
- `--dry-run` still skips `.venv` creation and package installation.
- The script warns if the shell is already inside another virtual environment.
- The script keeps a local `.venv_setup_cache.json` in each target project to speed up repeated scans.

## `remove_venv.py`

### What it does

- Removes only the target project’s `.venv`
- Leaves `requirements.txt` unchanged
- Leaves all source files, config files, logs, and data files untouched
- Supports one project or multiple sibling project folders

### Single-project examples

```bash
cd /path/to/workspace
python3 venv_setup/remove_venv.py --folder project_alpha
python3 venv_setup/remove_venv.py --folder project_beta --dry-run
python3 venv_setup/remove_venv.py --folder project_gamma --force
```

### Multi-project examples

```bash
cd /path/to/workspace
python3 venv_setup/remove_venv.py --folders project_alpha project_beta project_gamma
python3 venv_setup/remove_venv.py --folders project_alpha project_beta --dry-run
python3 venv_setup/remove_venv.py --folders project_alpha project_beta --force
```

## Safety

- Neither script deletes anything without either:
  - a dedicated destructive flag, or
  - an explicit confirmation prompt
- `remove_venv.py` only deletes `.venv`
- `venv_setup.py --rebuild` only deletes `.venv`
- Both scripts are intended for project folders inside a common workspace

## Current ignore targets

This folder’s `.gitignore` excludes:

- `__pycache__/`
- `.DS_Store`
- common local scratch/output files for this utility folder
