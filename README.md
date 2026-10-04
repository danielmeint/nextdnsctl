# nextdnsctl

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build Status](https://github.com/danielmeint/nextdnsctl/actions/workflows/test.yml/badge.svg)](https://github.com/danielmeint/nextdnsctl/actions/workflows/test.yml)

A command-line tool for NextDNS: bulk-edit your denylist and allowlist, import block lists
from files or URLs, and back them up.

**Disclaimer**: This is an unofficial tool, not affiliated with NextDNS. Built by a user, for users.

```bash
nextdnsctl auth login
nextdnsctl -p "My Profile" denylist import https://example.com/blocklist.txt
nextdnsctl -p "My Profile" denylist add bad.example worse.example
nextdnsctl -p "My Profile" allowlist remove too-strict.example
nextdnsctl -p "My Profile" denylist export > backup.txt
```

## Features

- **Bulk add, remove and import** for the denylist and allowlist, from the command line or
  from a file or URL, in plain, hosts or adblock format.
- **Fast and safe**: importing 2 000 domains is one API request, not 2 000, and if
  anything is invalid, nothing is changed. Entries that are already there are skipped.
- **Strict imports**: a line that can't be represented exactly in NextDNS is an error with
  its line number, never a guess.
- **Export** a list for backup, and `--dry-run` to preview any change.
- **`why`**: find out which blocklist or setting blocked a domain, and allow it in one step.
- **Whole profiles as code** (new in 2.0): describe denylist, allowlist, security, privacy,
  parental control, settings and rewrites in a YAML file, and `plan` / `apply` it.

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

Upgrading from 1.x? Your commands and stored API key keep working, some with a deprecation
warning; invalid domains are now an error instead of being skipped. See
[migrating to 2.0](docs/migrating-to-2.md).

## Authentication

Find your API key at the bottom of https://my.nextdns.io/account, then:

```bash
nextdnsctl auth login      # prompts without echoing; or: pbpaste | nextdnsctl auth login
nextdnsctl auth status     # checks the key works
```

The key is stored in `~/.config/nextdnsctl/config.json`, readable only by you. The
`NEXTDNS_API_KEY` environment variable takes precedence (handy in CI).

## Choosing a profile

Pass the profile's name or id with `-p`, or set it once for your shell:

```bash
nextdnsctl profile list
export NEXTDNS_PROFILE="My Profile"
```

The examples below assume `NEXTDNS_PROFILE` is set.

## Denylist and allowlist

```bash
nextdnsctl denylist list [--active-only | --inactive-only]
nextdnsctl denylist add bad.example https://worse.example/page   # a URL is reduced to its host
nextdnsctl denylist add maybe.example --inactive                 # listed, but not blocked
nextdnsctl denylist remove bad.example
nextdnsctl denylist import blocklist.txt                         # a file or a URL
nextdnsctl denylist export [backup.txt]                          # stdout by default
nextdnsctl denylist clear [--yes]
```

Everything works the same for `allowlist`. Add `--dry-run` (before the subcommand) to see
what would change without changing anything:

```bash
$ nextdnsctl --dry-run denylist add bad.example evil.example
Profile My Profile (abc123)
  denylist: +2 (14 → 16 entries)
    + bad.example
    + evil.example
  Writes: 1 atomic update
Dry run: nothing was changed.
```

`add` and `import` skip domains that are already in the list. A domain that is there with
the other active/inactive state is left alone unless you pass `--update-existing`.

### Importing block lists

`import` reads one of three exact formats, detected automatically when every line agrees:

| Format | Lines | Rejected |
|---|---|---|
| `plain` | `example.com`, or a URL like `https://example.com/x`; `#` comments | anything else on the line |
| `hosts` | `0.0.0.0 example.com` (also `127.0.0.1`, `::`, `::1`) | other addresses: that's a rewrite, not a block |
| `adblock` | `\|\|example.com^`; `!` comments | exceptions, `$` modifiers, wildcards, paths, cosmetic rules |

```bash
nextdnsctl denylist import https://example.com/hosts.txt
nextdnsctl denylist import mixed.txt --format plain
nextdnsctl denylist import messy.txt --skip-invalid
```

Names are lowercased and internationalized names converted to punycode. An invalid line
fails the import with its line number, and nothing is changed; `--skip-invalid` skips such
lines with a warning instead. Note that a hosts entry blocks one exact name, while a NextDNS
denylist entry also covers its subdomains.

As long as the resulting list stays under roughly 2 000 domains, any change to it is a single
request. Beyond that, changes are applied entry by entry, paced to NextDNS's limit of about
60 writes per minute: importing 10 000 domains takes about three hours. For lists that size, prefer NextDNS's built-in blocklists (under **Privacy**)
and keep the denylist for your own additions and overrides.

## More commands

```bash
nextdnsctl why ads.example.com            # which blocklist or setting blocked it?
nextdnsctl why ads.example.com --allow    # … and add it to the allowlist
nextdnsctl logs --blocked --since 1h
nextdnsctl logs --follow

nextdnsctl rewrites list
nextdnsctl rewrites add nas.lan 192.168.1.10
nextdnsctl rewrites remove nas.lan

nextdnsctl profile list | create NAME | delete NAME
nextdnsctl catalog blocklists | natives | services | categories | tlds
```

`why` and `logs` need logging enabled on the profile (NextDNS creates new profiles with
logging off).

## Managing whole profiles from a file

New in 2.0: describe one or more profiles in a YAML file, keep it in git, and let
nextdnsctl make NextDNS match it.

```bash
nextdnsctl pull          # write your profiles to nextdns.yaml
$EDITOR nextdns.yaml     # change what you want
nextdnsctl plan          # see exactly what would change
nextdnsctl apply         # make NextDNS match the file
```

A file can be as small as one list, or describe everything:

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
- **Sources are merged into the list.** Inline `domains` plus every source, deduplicated,
  read in the formats described [above](#importing-block-lists).
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

If an apply is interrupted, run it again: it only sends what's still missing.

## Global options

| Option | |
|---|---|
| `-p, --profile` | Profile name or id (or `NEXTDNS_PROFILE`) |
| `--dry-run` | Show what would change without changing anything |
| `--json` | Machine-readable output on stdout |
| `-v` / `-q` | Show every entry and request / only errors |
| `-f, --file` | Profile file for `pull`/`plan`/`apply` (default `nextdns.yaml`, or `NEXTDNS_FILE`) |
| `--timeout` | Request timeout in seconds |

Data goes to stdout and everything else (progress, warnings) to stderr, so
`nextdnsctl denylist export > backup.txt` is safe.

## Contributing

Pull requests welcome! See [docs/contributing.md](docs/contributing.md). The design and
the observed behaviour of the NextDNS API it relies on are in
[docs/v2-design.md](docs/v2-design.md).

## License

MIT License - see [LICENSE](LICENSE).
