# Installation

[← docs index](index.md)

## Requirements

| | |
|---|---|
| Python | 3.10 or newer (CPython; 3.10 – 3.13 are tested in CI) |
| Runtime dependencies | **none** — standard library only |
| Operating systems | Linux, macOS, Windows |

No dependencies is a deliberate feature: `quarantine` is meant to be safe to
add to someone else's crowded environment without a resolver argument.

## Install from PyPI

The package lives at
[pypi.org/project/quarantine-py](https://pypi.org/project/quarantine-py/):

```bash
pip install quarantine-py
```

Releases are built in CI and published to PyPI with [trusted
publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) - no long-lived
API keys and no manual uploads sit between the tagged source and the wheel
you install.

With [uv](https://docs.astral.sh/uv/):

```bash
uv add quarantine-py            # inside a uv project
uv pip install quarantine-py    # standalone
```

Pin it like any other library:

```
# requirements.txt
quarantine-py~=0.1
```

```toml
# pyproject.toml
dependencies = ["quarantine-py>=0.1,<0.2"]
```

## Install for development, or from source

```bash
git clone https://github.com/halcyon-past/quarantine
cd quarantine
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"       # the package plus pytest, ruff, mypy
```

Straight from git, without cloning:

```bash
pip install "git+https://github.com/halcyon-past/quarantine"
```

## Verify the install

Two halves get installed — the importable library, and a `quarantine`
command-line tool. Check both:

```bash
$ python -c "import quarantine; print(quarantine.__version__)"
0.1.2
$ quarantine --version
quarantine 0.1.2
```

If the command is not on your `PATH` — usually after a `pip install --user`,
where scripts land somewhere your shell does not look — the module form is
identical in every way:

```bash
python -m quarantine list
```

To find where the script went:

```bash
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

## First run

Nothing needs configuring. Decorate a function, run it, and the folder appears
the first time something fails:

```python
from quarantine import quarantine


@quarantine
def process(item):
    return 100 / item


for item in [1, 2, 0, 4]:
    process(item)
```

```
$ python job.py
✓ 3 processed · ✗ 1 quarantined → .quarantine/  (run `quarantine retry` after fixing)
$ quarantine list
   #  when      function  error                              input preview
   1  09:14:02  process   ZeroDivisionError: division by ...  0
```

The summary line goes to **stderr**, so it never pollutes a pipeline whose
output you are capturing on stdout.

## Choose where the folder lives

In order of precedence:

```python
@quarantine(dir="build/bad-rows")     # 1. explicit, per decorator
```

```bash
export QUARANTINE_DIR=/var/tmp/quarantine   # 2. environment, library + CLI
```

Otherwise `./.quarantine`, relative to the process's working directory.

Add it to your ignore file. Quarantined inputs are **your real data** — rows,
payloads, API responses — and they should not end up in a commit:

```gitignore
.quarantine/
```

## Upgrading and uninstalling

```bash
pip install --upgrade quarantine-py
pip uninstall quarantine-py
```

To hear about new versions: on the
[GitHub repository](https://github.com/halcyon-past/quarantine), choose
**Watch → Custom → Releases** — GitHub then notifies you of each release and
its notes, and nothing else. Every release is also listed in the
[changelog](https://github.com/halcyon-past/quarantine/blob/main/CHANGELOG.md),
and tools like Dependabot and Renovate pick new versions up from PyPI
automatically.

Uninstalling leaves any `.quarantine/` folders alone; they are plain files, so
delete them with `rm -rf` or `quarantine clear --yes` beforehand.

The record format carries a `meta_version` field, and records are read
tolerantly: unknown keys are ignored and missing optional keys get defaults, so
a folder written by an older version stays readable after an upgrade.
