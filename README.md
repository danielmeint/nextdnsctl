# nextdnsctl

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml/badge.svg)](https://github.com/danielmeint/nextdnsctl/actions/workflows/lint.yml)

A community-driven CLI tool for managing NextDNS profiles declaratively.

**Disclaimer**: This is an unofficial tool, not affiliated with NextDNS. Built by a user, for users.

> **Note**: While `nextdnsctl` handles API rate limiting and retries, it is **not recommended for importing very large
blocklists**. For large-scale filtering, prefer using NextDNS's built-in curated blocklists under the **Privacy** tab,
> and use the `denylist` feature for specific overrides or fine-tuning.

## Features

- Bulk add/remove domains to the NextDNS denylist and allowlist
- Import domains from a file or URL
- Export current list to a file for backup
- List and clear all entries in a list
- Parallel API requests for faster bulk operations
- Dry-run mode to preview changes before applying
- Use profile names or IDs interchangeably

## Installation

```bash
pip install nextdnsctl
```

Requires Python 3.6+.

## Quick Start

```bash
# Authenticate (find your API key at https://my.nextdns.io/account)
nextdnsctl auth <your-api-key>

# List your profiles
nextdnsctl profile-list

# Add domains to denylist (using profile name or ID)
nextdnsctl denylist add "My Profile" bad.com evil.com

# Preview changes without applying them
nextdnsctl --dry-run denylist import myprofile blocklist.txt
```

## Authentication

The API key can be provided in two ways (in order of priority):

1. **Environment variable** (recommended for CI/CD):
   ```bash
   export NEXTDNS_API_KEY=your-api-key
   nextdnsctl profile-list
   ```

2. **Config file** (created by `auth` command):
   ```bash
   nextdnsctl auth <your-api-key>
   # Stored in ~/.nextdnsctl/config.json with secure permissions
   ```

## Global Options

| Option               | Description                                           |
|----------------------|-------------------------------------------------------|
| `--concurrency N`    | Number of parallel API requests (1-20, default: 5)    |
| `--dry-run`          | Show what would be done without making changes        |
| `--retry-attempts N` | Number of retry attempts for API calls (default: 4)   |
| `--retry-delay N`    | Initial delay between retries in seconds (default: 1) |
| `--timeout N`        | Request timeout in seconds (default: 10)              |

## Profile Identification

All commands accept either a **profile ID** or **profile name** (case-insensitive):

```bash
# Using profile ID
nextdnsctl denylist list abc123

# Using profile name
nextdnsctl denylist list "My Profile"
```

## Denylist Commands

### List entries

```bash
nextdnsctl denylist list <profile>
nextdnsctl denylist list <profile> --active-only
nextdnsctl denylist list <profile> --inactive-only
```

### Add domains

```bash
nextdnsctl denylist add <profile> domain1.com domain2.com
nextdnsctl denylist add <profile> domain.com --inactive
```

### Remove domains

```bash
nextdnsctl denylist remove <profile> domain1.com domain2.com
```

### Import from file or URL

```bash
nextdnsctl denylist import <profile> /path/to/blocklist.txt
nextdnsctl denylist import <profile> https://example.com/blocklist.txt
nextdnsctl denylist import <profile> blocklist.txt --inactive
```

The import file format supports:
- One domain per line
- Comments starting with `#`
- Inline comments (e.g., `example.com # reason`)
- Empty lines (ignored)

### Export to file

```bash
nextdnsctl denylist export <profile> backup.txt
nextdnsctl denylist export <profile>  # outputs to stdout
nextdnsctl denylist export <profile> --active-only > active.txt
```

### Clear all entries

```bash
nextdnsctl denylist clear <profile>       # asks for confirmation
nextdnsctl denylist clear <profile> --yes # skip confirmation
```

## Allowlist Commands

All denylist commands are available for allowlist with the same syntax:

```bash
nextdnsctl allowlist list <profile>
nextdnsctl allowlist add <profile> good.com trusted.com
nextdnsctl allowlist remove <profile> domain.com
nextdnsctl allowlist import <profile> allowlist.txt
nextdnsctl allowlist export <profile> backup.txt
nextdnsctl allowlist clear <profile> --yes
```

## Parallel Requests

By default, bulk operations run 5 concurrent API requests. Adjust with `--concurrency`:

```bash
# Faster (more concurrent requests)
nextdnsctl --concurrency 10 denylist import myprofile blocklist.txt

# Sequential mode (verbose per-domain output, like v0.2.0)
nextdnsctl --concurrency 1 denylist import myprofile blocklist.txt
```

## Dry-Run Mode

Preview changes before applying them:

```bash
$ nextdnsctl --dry-run denylist add myprofile bad.com evil.com
[DRY-RUN] Would add 2 domain(s):
  - bad.com
  - evil.com

[DRY-RUN] No changes made.
```

## Contributing

Pull requests welcome! See [docs/contributing.md](docs/contributing.md) for details.

## License

MIT License - see [LICENSE](LICENSE).
