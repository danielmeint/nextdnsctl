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

# Upload to PyPI (requires TWINE_USERNAME and TWINE_PASSWORD)
publish: build
    .venv/bin/python -m twine upload dist/*

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
