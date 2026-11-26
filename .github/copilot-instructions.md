# GitHub Copilot Instructions for quarantine-py

See `AGENTS.md` for complete architectural rules and guidelines.

## Key Rules
1. Zero runtime dependencies: Never add external packages to `dependencies` in `pyproject.toml`.
2. Atomic writes: Always write new records to temporary `.tmp-*` staging directories and commit with `os.rename`.
3. Never lose a failure: Always record or re-raise exceptions caught in wrapper methods.
4. Redaction before serialization: Always scrub secrets with `redact_call()` before saving to disk.
5. Strict typing: All code must satisfy `mypy` strict mode and Python 3.10+ typing standards.
