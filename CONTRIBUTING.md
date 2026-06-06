# Contributing to LionAGI

Thank you for considering contributing to LionAGI! This document provides
guidelines and instructions for contributing to this project.

## Getting Started

1. **Fork the Repository**: Begin by forking the repository to your GitHub
   account. This creates your own copy of the project where you can make
   changes.

2. **Clone the Forked Repository**: Clone the repository to your local machine
   to start working on the changes.

3. **Set Up Your Development Environment**: Ensure you have a suitable Python
   development environment. Any IDE that supports Python and package
   installation should be sufficient.

## Making Changes

1. **Creating Branches**: For new features or bug fixes, create a new branch off
   the main branch. Branch names should be descriptive and reflect the feature
   or fix you are working on.

2. **Commit Messages**: Write clear and descriptive commit messages. While
   there’s no strict format, ensure your messages convey the purpose of the
   commit.

## Submitting Contributions

1. **Pull Requests**: Once you are ready with your changes, push your branch to
   your fork and open a pull request against the main repository.

2. **Pull Request Description**: Provide a detailed description of the changes
   in your pull request. Link it to any relevant issues.

3. **Code Review Process**: Your pull request will be reviewed by the project
   maintainers. Feedback may be provided for improvements.

4. **Merging**: After approval, one of the maintainers will merge your pull
   request. No direct merges into the main branch are allowed without approval.

## Testing

1. **Writing Tests**: Include unit and integration tests for your code. Tests
   mirror the package layout (`lionagi/<area>/x.py` → `tests/<area>/test_x.py`)
   and are tagged with markers (`unit`, `integration`, `slow`, `performance`,
   `property`, `migration`, `network`). Every behavioral change needs a test in
   the same patch; changes to a trust boundary (auth, paths, URLs, untrusted
   input) need a fail-closed regression test.

2. **Running Tests Locally**: Run `uv run pytest` (parallel, `-n auto`) before
   submitting. Use `uv run pytest tests/<file>.py -v` for a focused run and
   `uv run pytest -n0 -s ...` to debug. Performance-sensitive changes should be
   checked against the benchmarks in `benchmarks/` (`ci_compare.py` gates
   regressions).

## Coding Standards

1. **Formatting & linting are automated by [ruff](https://docs.astral.sh/ruff/).**
   Run `uv run ruff format . && uv run ruff check --fix .`, or
   `pre-commit run -a` for the full pipeline (ruff-format, ruff, pyupgrade,
   markdownlint). `[tool.ruff]` in `pyproject.toml` is the source of truth —
   line length **100**, target `py310` (CI runs 3.10–3.13).

2. **PEP 8 / PEP 257**: follow standard style and docstring conventions; ruff's
   `E F W B I UP N S A` rule set enforces most of this (incl. import sorting,
   pyupgrade, naming, and bandit security checks).

3. **Conventions**: start every module with `from __future__ import annotations`
   and the Apache-2.0 SPDX header; declare the public surface with `__all__`.
   This is an async-first SDK — no blocking I/O in async paths.

4. **Reuse before you create**: prefer existing abstractions
   (`lionagi.ln` utilities, `Pile`/`Progression`/`Element`, `iModel`) over
   introducing parallel ones. Keep changes surgical.

## Dependencies

1. **Managing Dependencies**: Do not add or update dependencies without prior
   approval from the maintainers.

## Documentation

1. **Update Documentation**: Accompany your code changes with corresponding
   updates in the documentation.

2. **Versioning Documentation**: Documentation should be versioned alongside the
   code.

## Community Interaction

1. **Using Discord**: Use the project’s Discord for discussions, questions, and
   collaboration.

## Acknowledging Contributions

1. **Recognition**: We value your contributions and will acknowledge significant
   contributions in our social media and release notes.

2. **GitHub Sponsors**: We are considering setting up a GitHub Sponsor button to
   enable sponsorship for our contributors.

## Questions?

If you have any questions or need further clarification about contributing, feel
free to reach out on our Discord server.

Thank you for contributing to LionAGI, and we look forward to your
contributions!
