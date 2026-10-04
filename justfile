# nextdnsctl development tasks

# Default recipe - show available commands
default:
    @just --list

# Run all tests (live API tests are excluded; see test-live)
test:
    .venv/bin/python -m pytest tests/ -v

# Run the round-trip tests against the real NextDNS API (needs NEXTDNS_API_KEY;
# creates and deletes a temporary profile)
test-live:
    .venv/bin/python -m pytest tests/ -v -m live

# Run tests with coverage
test-cov:
    .venv/bin/python -m pytest tests/ -v --cov=nextdnsctl --cov-report=term-missing

# Run linter (flake8)
lint:
    .venv/bin/python -m flake8 nextdnsctl/ tests/

# Run type checker (mypy)
typecheck:
    .venv/bin/python -m mypy nextdnsctl/

# Run all checks (lint + typecheck + test)
check: lint typecheck test

# Format code with black
fmt:
    .venv/bin/python -m black nextdnsctl/ tests/

# Install package in development mode
install-dev:
    .venv/bin/pip install -e ".[dev]"

# Build distribution packages
build:
    rm -rf dist/
    .venv/bin/python -m build

# Upload to PyPI (uses ~/.pypirc for credentials)
publish: build
    .venv/bin/python -m twine upload dist/*

# Print the Homebrew formula for a released version (CI pushes it to the tap automatically)
formula version:
    python3 scripts/homebrew_formula.py {{version}}

# Create a new release (bump version, tag, push, and publish GitHub release)
# Requires GitHub CLI authentication. Publishing the GitHub release triggers PyPI.
# Usage: just release 1.2.0
release version:
    @echo "Updating version to {{version}}..."
    sed -i '' 's/__version__ = ".*"/__version__ = "{{version}}"/' nextdnsctl/__init__.py
    git add nextdnsctl/__init__.py
    git commit -m "Release {{version}}"
    git tag -a "v{{version}}" -m "Release v{{version}}"
    git push origin main
    git push origin "v{{version}}"
    gh release create "v{{version}}" --title "v{{version}}" --notes "Release v{{version}}"
    @echo "Release v{{version}} tagged and pushed!"
    @echo "GitHub release published. GitHub Actions will publish to PyPI automatically."
    @echo "Or run 'just publish' to publish manually."

# Clean build artifacts
clean:
    rm -rf dist/ build/ *.egg-info/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true

# Create virtual environment
venv:
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip

# Install pre-commit hooks
hooks:
    .venv/bin/pre-commit install

# Run pre-commit on all files
pre-commit:
    .venv/bin/pre-commit run --all-files

# Setup development environment from scratch
setup: venv install-dev hooks
    @echo "Development environment ready!"
