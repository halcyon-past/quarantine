## Description

<!-- Describe the changes proposed in this Pull Request. Include context, motivation, and references to any relevant issues. -->

Fixes # <!-- Issue number if applicable -->

---

### Type of Change

- [ ] 🐛 Bug fix (non-breaking change fixing an issue)
- [ ] 🔒 Security fix (vulnerability mitigation or hardening)
- [ ] ⚡ Performance improvement (optimization without functional changes)
- [ ] 📝 Documentation update (guides, docstrings, ADRs)
- [ ] 🧹 Code hygiene / refactoring (no functional changes)
- [ ] ✨ New feature (non-breaking enhancement matching library philosophy)

---

### Core Invariants Checklist

Any PR modifying library internals must respect these load-bearing principles:

- [ ] **Never lose a failure:** Any code path that catches an exception must either write a record or re-raise. Silently dropping an item is strictly prohibited.
- [ ] **The folder is the API:** `.quarantine/` contains plain files. Record directories are self-describing, and `index.json` is a rebuildable cache, never the single source of truth.
- [ ] **Writes stay atomic:** Build records in a temporary directory/file, flush/fsync, and rename into place.
- [ ] **Redact before writing:** Redaction runs *before* any data reaches the disk or network, and never mutates caller objects.
- [ ] **Zero runtime dependencies:** Core library runtime dependencies must remain empty in `pyproject.toml`. Remote backends remain optional extras.

---

### Verification Checklist

- [ ] Target branch is `dev` (or `main` strictly for critical production hotfixes).
- [ ] Branch name adheres to repository naming convention (`feature/<target-branch>/<name>` or `hotfix/<name>`).
- [ ] `ruff check .` passes without errors.
- [ ] `ruff format --check .` passes.
- [ ] `mypy` passes in strict mode across all source files.
- [ ] `pytest --cov --cov-report=term-missing` passes with coverage $\ge 90\%$.
- [ ] `mkdocs build --strict` builds successfully without broken links or warnings.
- [ ] Regression suite `pytest -m regression` passes (or updated if public user journey changed).
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`.
