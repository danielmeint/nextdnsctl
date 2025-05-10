# nextdnsctl

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml/badge.svg)](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml)

A community-driven CLI tool for managing NextDNS profiles declaratively.

**Disclaimer**: This is an unofficial tool, not affiliated with NextDNS. Built by a user, for users.

> ⚠️ **Note**: While `nextdnsctl` now handles API rate limiting and retries, it is **not recommended for importing very large blocklists**. For large-scale filtering, prefer using NextDNS’s built-in curated blocklists under the **Privacy** tab, and use the `denylist` feature for specific overrides or fine-tuning.

## Features
- Bulk add/remove domains to the NextDNS denylist and allowlist.
- Import domains from a file or URL to the denylist and allowlist.
- List all profiles to find their IDs.
- More to come: full config sync, etc.

## Installation
1. Install Python 3.6+.
2. Clone or install:
   ```bash
   pip install nextdnsctl
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

### Denylist Management
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

### Allowlist Management
6. Add domains to allowlist:
   ```bash
   nextdnsctl allowlist add <profile_id> good.com trusted.com
   ```
7. Remove domains from allowlist:
   ```bash
   nextdnsctl allowlist remove <profile_id> good.com
   ```
8. Import domains from a file or URL:
   - From a file:
     ```bash
     nextdnsctl allowlist import <profile_id> /path/to/allowlist.txt
     ```
   - From a URL:
     ```bash
     nextdnsctl allowlist import <profile_id> https://example.com/allowlist.txt
     ```
   - Use `--inactive` to add domains as inactive (not allowed):
     ```bash
     nextdnsctl allowlist import <profile_id> allowlist.txt --inactive
     ```

## Contributing
Pull requests welcome! See [docs/contributing.md](docs/contributing.md) for details.

## License
MIT License - see [LICENSE](LICENSE).
