# Contributing to nextdnsctl

Thanks for your interest in contributing!

## How to Contribute
1. Fork the repository.
2. Create a branch: `git checkout -b feature/your-feature`.
3. Make your changes and commit: `git commit -m "Add your feature"`.
4. Push to your fork: `git push origin feature/your-feature`.
5. Open a pull request against `main`.

## Development Setup
- Install Python 3.10+.
- Clone your fork: `git clone https://github.com/<your-username>/nextdnsctl.git`.
- Set up: `just setup` (or `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`).
- Run locally: `.venv/bin/nextdnsctl --help`.
- Checks: `just check` (lint, types, tests). Tests run against an in-memory fake of the
  NextDNS API (`tests/fake_api.py`) that reproduces its observed behaviour; if you find
  the real API behaving differently, update the fake and `docs/v2-design.md` together.
- Releases: `just release X.Y.Z` tags and publishes a GitHub release; GitHub Actions then
  publishes to PyPI and regenerates, tests and pushes the Homebrew formula
  (`.github/workflows/homebrew.yml`; run it by hand from the Actions tab if needed).
  `flake.nix` reads the version from `nextdnsctl/__init__.py`.
- Live round trip: `NEXTDNS_API_KEY=… just test-live` creates and deletes a temporary profile.

## Ideas
- Support other DNS services (e.g., ControlD).
- See "Open questions" and "Milestones" in [the design doc](v2-design.md).

## Code Style
- Follow PEP 8 for Python.
- Keep error messages clear and user-friendly.

Questions? Open an issue or join the Discussions tab!
