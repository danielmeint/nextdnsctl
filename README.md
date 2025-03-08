# nextdnsctl

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml/badge.svg)](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml)

A community-driven CLI tool for managing NextDNS profiles declaratively.

**Disclaimer**: This is an unofficial tool, not affiliated with NextDNS. Built by a user, for users.

## Features
- Bulk add/remove domains to the NextDNS denylist.
- Import domains from a file or URL to the denylist.
- List all profiles to find their IDs.
- More to come: allowlist support, full config sync, etc.

## Installation
1. Install Python 3.6+.
2. Clone or install:
   ```bash
   pip install git+https://github.com/danielmeint/nextdnsctl.git
   ```

## Usage
1. Set up your API key (find it at https://my.nextdns.io/account):
   ```bash
   nextdnsctl auth <your-api-key>
   ```
2. List profiles:
   ```bash
   nextdnsctl profile-list
   ```
3. Add domains to denylist:
   ```bash
   nextdnsctl denylist add <profile_id> bad.com evil.com
   ```
4. Remove domains from denylist:
   ```bash
   nextdnsctl denylist remove <profile_id> bad.com
   ```
5. Import domains from a file or URL:
   - From a file:
     ```bash
     nextdnsctl denylist import <profile_id> /path/to/blocklist.txt
     ```
   - From a URL:
     ```bash
     nextdnsctl denylist import <profile_id> https://example.com/blocklist.txt
     ```
   - Use `--inactive` to add domains as inactive (not blocked):
     ```bash
     nextdnsctl denylist import <profile_id> blocklist.txt --inactive
     ```

## Contributing
Pull requests welcome! See [docs/contributing.md](docs/contributing.md) for details.

## License
MIT License - see [LICENSE](LICENSE).
