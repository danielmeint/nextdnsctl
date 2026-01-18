# nextdnsctl development tasks

# Default recipe - show available commands
default:
    @just --list

# Run all tests
test:
    .venv/bin/python -m pytest tests/ -v

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

# Install dependencies
install-deps:
    .venv/bin/pip install -r requirements.txt
    .venv/bin/pip install -r requirements-dev.txt

# Build distribution packages
build:
    rm -rf dist/
    .venv/bin/python -m build

# Upload to PyPI (uses ~/.pypirc for credentials)
publish: build
    .venv/bin/python -m twine upload dist/*

# Create a new release (bump version, tag, and push)
# Usage: just release 1.2.0
release version:
    @echo "Updating version to {{version}}..."
    sed -i '' 's/__version__ = ".*"/__version__ = "{{version}}"/' nextdnsctl/__init__.py
    git add nextdnsctl/__init__.py
    git commit -m "Bump version to {{version}}"
    git tag -a "v{{version}}" -m "Release v{{version}}"
    git push origin main
    git push origin "v{{version}}"
    @echo "Release v{{version}} tagged and pushed!"
    @echo "GitHub Actions will publish to PyPI automatically."
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

# Setup development environment from scratch
setup: venv install-deps install-dev
    @echo "Development environment ready!"
