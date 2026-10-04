# nextdnsctl

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/danielmeint/nextdnsctl/actions/workflows/test.yml/badge.svg)](https://github.com/danielmeint/nextdnsctl/actions/workflows/test.yml)

Manage NextDNS profiles from the command line, or declaratively from a file you keep in git.

**Disclaimer**: This is an unofficial tool, not affiliated with NextDNS. Built by a user, for users.

```bash
nextdnsctl pull          # write your profiles to nextdns.yaml
$EDITOR nextdns.yaml     # change what you want
nextdnsctl plan          # see exactly what would change
nextdnsctl apply         # make NextDNS match the file
```

## Features

- **Declarative profiles**: denylist, allowlist, security, privacy blocklists, parental
  control, settings and rewrites in one YAML file, with `pull` / `plan` / `apply`.
- **One atomic request** for most changes, so a 2 000-domain list doesn't take 2 000 API
  calls (and if anything is invalid, nothing is changed).
- **Strict list imports** in plain, hosts and adblock format: a line that can't be
  represented exactly is an error with its line number, never a guess.
- **Quick edits** without a file: `denylist add`, `allowlist remove`, `rewrites add`, …
- **`why`**: find out which blocklist or setting blocked a domain, and allow it in one step.
- Paced to NextDNS's rate limits, `--json` output and meaningful exit codes for scripts.

## Installation

```bash
# Homebrew (macOS, Linux)
brew tap danielmeint/tap
brew trust danielmeint/tap       # recent Homebrew asks you to trust third-party taps
brew install nextdnsctl

# PyPI (Python 3.10+)
pipx install nextdnsctl          # or: uv tool install nextdnsctl / pip install nextdnsctl

# Nix
nix run github:danielmeint/nextdnsctl -- --help
```

Upgrading from 1.x? See [migrating to 2.0](docs/migrating-to-2.md).

## Authentication

Find your API key at the bottom of https://my.nextdns.io/account, then:

```bash
nextdnsctl auth login      # prompts without echoing; or: pbpaste | nextdnsctl auth login
nextdnsctl auth status     # checks the key works
```

The key is stored in `~/.config/nextdnsctl/config.json`, readable only by you. The
`NEXTDNS_API_KEY` environment variable takes precedence (handy in CI).

## Declarative profiles

`nextdnsctl pull` writes every profile (or the ones you name) to `nextdns.yaml`:

```yaml
version: 1
profiles:
  Home:
    id: abc123                 # pins the profile; the key above is then its name
    denylist:
      domains:
        - bad.example
        - { domain: tracker.example, active: false }
      sources:
        - url: https://example.com/hosts.txt
          format: hosts
        - file: lists/extra.txt
    allowlist: [good.example]
    security:
      nrd: true
      tlds: [zip, mov]
    privacy:
      blocklists: [nextdns-recommended, oisd]
      natives: [apple]
    parentalControl:
      services: [tiktok]
      categories: [gambling]
    settings:
      logs: { enabled: true, retention: 30d }
    rewrites:
      - { name: nas.lan, content: 192.168.1.10 }
```

The rules:

- **A section that is present is managed; a section that is absent is left alone.** A file
  with only `denylist:` never touches your security settings.
- **A managed list is complete.** Entries in NextDNS that aren't in the file are removed on
  `apply`; the plan lists removals and `apply` asks before making them.
- **Sources are merged into the list.** Inline `domains` plus every source, deduplicated.
- **Typos are errors.** Unknown keys, blocklist ids, service ids and so on are reported with
  the file and line (and a "did you mean"). `nextdnsctl catalog blocklists` lists valid ids.
- **No secrets.** The API key never goes in the file, and `pull` never writes the device
  setup section (it contains your linked-IP update token).
- A profile in the file that doesn't exist yet is created by `apply`. YAML anchors work for
  sharing a section between profiles.

```bash
nextdnsctl plan               # exit code 0: no changes, 2: changes, 1: error
nextdnsctl plan Home --json   # machine-readable
nextdnsctl apply              # shows the plan and asks; --yes to skip the question
nextdnsctl -f other.yaml apply Home
```

`apply` sends one atomic update per profile when it can. Only a list too large for a single
request (roughly 2 000+ domains) is applied entry by entry, paced to NextDNS's limit of
about 60 writes per minute; the plan tells you how long that will take. If an apply is
interrupted, run it again: it only sends what's still missing.

## Importing block lists

Sources (in the file or with `denylist import`) are read in one of three exact formats:

| Format | Lines | Rejected |
|---|---|---|
| `plain` | `example.com`, or a URL like `https://example.com/x` | anything else on the line |
| `hosts` | `0.0.0.0 example.com` (also `127.0.0.1`, `::`, `::1`) | other addresses: that's a rewrite, not a block |
| `adblock` | `\|\|example.com^` | exceptions, `$` modifiers, wildcards, paths, cosmetic rules |

The format is detected when every line agrees; a mixed file is an error. Names are
lowercased and internationalized names converted to punycode. An invalid line fails the
import with its line number; pass `--skip-invalid` (or `skip_invalid: true` on a source) to
skip such lines with a warning instead. Note that a hosts entry blocks one exact name, while
a NextDNS denylist entry also covers its subdomains.

For very large lists, prefer NextDNS's built-in blocklists (`privacy.blocklists`) and keep
the denylist for your own overrides.

## Quick edits

Choose the profile with `-p NAME` (name or id) or `NEXTDNS_PROFILE`:

```bash
export NEXTDNS_PROFILE=Home

nextdnsctl denylist list [--active-only | --inactive-only]
nextdnsctl denylist add bad.example https://worse.example/page
nextdnsctl denylist add maybe.example --inactive
nextdnsctl denylist remove bad.example
nextdnsctl denylist import blocklist.txt [--format hosts] [--skip-invalid]
nextdnsctl denylist export [backup.txt]
nextdnsctl denylist clear [--yes]
# the same for allowlist

nextdnsctl rewrites list
nextdnsctl rewrites add nas.lan 192.168.1.10
nextdnsctl rewrites remove nas.lan

nextdnsctl profile list | create NAME | delete NAME
nextdnsctl catalog blocklists | natives | services | categories | tlds
```

`--dry-run` shows the change without making it. Each edit is a single request.

## Logs and `why`

```bash
nextdnsctl logs --blocked --since 1h
nextdnsctl logs --follow
nextdnsctl why ads.example.com            # which blocklist or setting blocked it?
nextdnsctl why ads.example.com --allow    # … and add it to the allowlist
```

These need logging enabled on the profile (`settings.logs.enabled`; NextDNS creates new
profiles with logging off).

## Global options

| Option | |
|---|---|
| `-p, --profile` | Profile name or id (or `NEXTDNS_PROFILE`) |
| `-f, --file` | Profile file (default `nextdns.yaml`, or `NEXTDNS_FILE`) |
| `--json` | Machine-readable output on stdout |
| `-v` / `-q` | Show every entry and request / only errors |
| `--dry-run` | Show what would change without changing anything |
| `--timeout` | Request timeout in seconds |

Data goes to stdout and everything else (progress, warnings) to stderr, so
`nextdnsctl denylist export > backup.txt` is safe.

## Contributing

Pull requests welcome! See [docs/contributing.md](docs/contributing.md). The design and
the observed behaviour of the NextDNS API it relies on are in
[docs/v2-design.md](docs/v2-design.md).

## License

MIT License - see [LICENSE](LICENSE).
