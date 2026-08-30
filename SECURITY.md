# Security policy

## Supported versions

The latest minor release receives security fixes. Older versions are fixed by
upgrading — the on-disk format is forward-tolerant, so upgrading does not
strand existing quarantine folders.

| Version | Supported |
|---|---|
| latest release | ✅ |
| anything older | upgrade |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem. Instead, use
GitHub's private reporting: **Security → Report a vulnerability** on
[the repository](https://github.com/halcyon-past/quarantine/security/advisories/new).

You can expect an acknowledgement within a week. Please include a minimal
reproduction; a failing test is perfect.

## The threat model, honestly

Two things are worth understanding before reporting:

- **Quarantine folders are trusted data.** Records are pickled by *your own
  process* and reloaded by `quarantine retry`/`debug` at your request.
  Loading a `.quarantine/` folder you did not produce is equivalent to
  running code from it — this is documented, deliberate
  ([ADR 0003](docs/adr/0003-pickle-first-serialization.md)), and reports that
  amount to "pickle can execute code" will be closed as by-design. A way to
  make quarantine *write* dangerous data from a benign run, or to escape
  `redact=`, is absolutely in scope.
- **The dashboard binds to all interfaces on your machine.** `quarantine ui`
  is a local debugging tool. Bugs that make it reachable or exploitable
  beyond that expectation are in scope.

## Supply chain

- Releases are built in CI and published to PyPI via
  [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) with
  attestations — no long-lived API keys exist.
- CI actions are pinned to commit SHAs, not mutable tags.
- The package has zero runtime dependencies
  ([ADR 0004](docs/adr/0004-zero-runtime-dependencies.md)), so `pip install
  quarantine-py` pulls in exactly one project's code: this one.
