# nextdnsctl v2 — design

Status: draft · 2026-10-04

v1 is an imperative wrapper around the denylist and allowlist: you run `add`, `remove`
and `import`. v2 makes the README's first line true — *managing NextDNS profiles
declaratively* — by describing a profile in a file and converging the live profile to it,
while keeping the v1 commands as shortcuts on top of the same engine.

## Goals

- **Declarative profiles.** A YAML file describes one or more profiles; `pull` writes it
  from the live state, `plan` shows the diff, `apply` converges.
- **Cover the profile, not just two lists**: security, privacy (blocklists, natives),
  parental control (services, categories), settings, rewrites.
- **Bulk changes in one request** where the API allows it (see [API facts](#api-facts)),
  instead of one request per domain.
- **Strict, predictable input parsing.** An ambiguous line is an error, never a guess.
- **Scriptable**: `--json` everywhere, meaningful exit codes, no noise on stdout.

## Non-goals (for 2.0)

- Other DNS providers.
- Analytics dashboards. `logs` and `why` are in scope; charts are not.
- Managing `setup` (linked IP, DDNS). It contains a credential (`updateToken`) and is
  device-specific, so it never goes into a file.
- Account-level operations beyond `profile create/delete`.

## API facts

Verified against the live API on 2026-10-04 with throwaway profiles. These are the
constraints the engine is built around; the [test fake](#testing) must reproduce them.
Several contradict or go beyond the [official docs](https://nextdns.github.io/api/), which
document `PUT` for arrays (see A5) and say nothing about rate limits or catalogs.

| # | Fact | Consequence |
|---|---|---|
| A1 | `PATCH /profiles/:id` accepts `denylist`, `allowlist`, `security`, `privacy`, `parentalControl`, `settings`, `name` in one body and applies **all or nothing** (one invalid field → 400, nothing changed). | One atomic request for most of an `apply`. |
| A2 | In a PATCH, **objects deep-merge, arrays replace wholesale**. | A changed list is sent in full; a changed setting is sent alone. |
| A3 | Request bodies over **100 KiB (102 400 bytes)** fail with **500 `internalServerError`**, not 413. Nothing is changed. ≈ 2 250 typical domains in compact JSON. | Check the size client-side; never retry a 500 for an oversize body. |
| A4 | Lists can grow past A3 via single `POST`s (no list cap observed at 2 253). | Lists that don't fit need an incremental path. |
| A5 | `PUT /profiles/:id/denylist` with a **duplicate id wipes the list** and answers `200 {"errors":[{"code":"duplicate"}]}`. The same body via profile `PATCH` is rejected safely. | **Never use PUT for lists.** Deduplicate client-side regardless. |
| A6 | `POST` of an existing domain → `200` + `duplicate` error, no change (not an upsert). `DELETE`/`PATCH` of a missing domain → `404 notFound`. | Incremental writes treat these as "already converged", after a re-check. |
| A7 | `active` may be omitted; it defaults to `true`. | Omit it to save ~14 B/entry under A3. |
| A8 | Domain ids must be lowercase ASCII (punycode OK, `_` OK, punycode TLDs OK). Rejected: uppercase, Unicode, wildcards, trailing dot, IPs, single labels. | Client normalises case and IDNA; validation regex matches the server's. |
| A9 | Errors carry a JSON pointer (`/1/id`) for format errors, but **not** for unknown catalog ids (bare `invalid`). | Map pointers back to file:line; for catalog ids, bisect or validate against a catalog. |
| A10 | Rewrites are **not** patchable via the profile (`extraneous`), have no PUT, get server-assigned ids, and allow several records per name. | Separate incremental diff keyed on `(name, content)`. |
| A11 | Writes: **60 per fixed 60 s window per API key** (shared across all profiles). A `PATCH` costs one write regardless of size. Pacing at 1 write/s ran 150 s without a 429. Reads have a separate budget that **depends on the endpoint**: 300/min for `GET …/denylist`, but full-profile `GET /profiles/:id` was throttled after 30 requests in 7 s (and `GET /profiles` after 3 when the API was slow), recovering within seconds. Exhausting writes doesn't block reads. **No `Retry-After`** header, ever. | Pace writes at ≤1/s; per-domain mode ≈ 60 domains/min; concurrency buys nothing. On a 429, back off 2, 4, 8, 16, 30, 30 s: short for reads, still spanning a write window. Keep full-profile GETs to one per profile per command. |
| A12 | `GET /profiles/:id` includes `setup.linkedIp.updateToken` and read-only metadata (blocklist `entries`/`updatedOn`, service `website`, `fingerprint`, `role`). | `pull` strips these; diffs ignore them. |
| A13 | `POST /profiles {"name"}` and `DELETE /profiles/:id` work. New profiles have **logging disabled** by default. | Ephemeral profiles for live tests; enable logs on them first. |
| A14 | `/logs`: `limit` 10–1000, cursor pagination, `status=blocked` and `search=` filters work, `meta.stream.id` for stitching. Blocked entries carry `reasons: [{id, name}]` (e.g. `denylist`). `/logs/stream` is SSE (`id:` + `data:` JSON, resume with `?id=`) but **sends no headers until the first event** and delivered only 2 of 10 queries that `/logs` had. | `why` uses `/logs?search=&status=blocked`. `logs --follow` polls `/logs` (cheap under A11) rather than trusting the stream. |
| A15 | Undocumented **public catalogs** (no API key needed): `/privacy/blocklists` (83), `/privacy/natives` (8), `/parentalControl/services` (43), `/parentalControl/categories` (7), `/security/tlds` (~1 500). | Validate catalog ids locally before sending (fixes A9); offer `nextdnsctl catalog …` and shell completion. Fall back gracefully if they disappear. |
| A16 | The server does **not** check TLDs against a list: `foo.notarealtld`, `.lan`, `.local`, `.internal` are accepted. `tld` errors only mean a single-label name. | The client regex is the whole check; no TLD list needed. |
| A17 | Rewrites: the record type is **inferred** from `content` (IPv4 → A, IPv6 → AAAA, name → CNAME); sending `type` is rejected. Single-label names (`nas`) are allowed. Invalid content → `200` + `invalid` error. | Rewrite files don't carry a type; validate content client-side. |

## Command surface

```
nextdnsctl auth login | logout | status
nextdnsctl profile list | create NAME | delete PROFILE

nextdnsctl pull  [PROFILE...] [-f FILE] [--stdout]
nextdnsctl plan  [-f FILE] [-p PROFILE]
nextdnsctl apply [-f FILE] [-p PROFILE] [--yes]

nextdnsctl denylist|allowlist list|add|remove|import|export|clear   # v1 shortcuts
nextdnsctl rewrites list|add|remove
nextdnsctl logs [--blocked] [--since 1h] [--follow]
nextdnsctl why DOMAIN [--allow]
nextdnsctl catalog blocklists|natives|services|categories|tlds

global: --json  -v/--verbose  -q/--quiet  -p/--profile (or NEXTDNS_PROFILE)
```

- `auth login` reads the key from a hidden prompt or stdin — never argv (v1 leaks it to
  shell history and `ps`). `NEXTDNS_API_KEY` still wins. Config moves to
  `$XDG_CONFIG_HOME/nextdnsctl/`; the v1 path is read as a fallback.
- `--profile` becomes a global option with an env default, so the shortcuts lose their
  positional `PROFILE` argument. See [Compatibility](#compatibility).
- `why DOMAIN` queries `/logs?search=DOMAIN&status=blocked` (A14) and prints each
  distinct reason (blocklist, security feature, denylist); `--allow` adds it to the
  allowlist. If logging is disabled on the profile it says so instead of "not blocked".
- `catalog blocklists|natives|services|categories|tlds` lists valid ids (A15).
- Exit codes: `0` success / no changes, `1` error, `2` `plan` found changes
  (`--detailed-exitcode` semantics, always on for `plan`), `3` partial apply.

## Config file

```yaml
# nextdns.yaml
version: 1

profiles:
  home:                       # key = profile name (case-insensitive) …
    id: e55311                # … or pin by id; id wins and catches renames
    denylist:
      domains:
        - bad.com
        - { domain: tracker.example, active: false }
      sources:
        - url: https://example.com/hosts.txt
          format: hosts
        - file: lists/extra.txt          # relative to this file
    allowlist:
      domains: [good.com]
    security:
      nrd: true
      cryptojacking: true
      tlds: [zip, mov]
    privacy:
      blocklists: [nextdns-recommended, oisd]
      natives: [apple, samsung]
      disguisedTrackers: true
    parentalControl:
      services: [tiktok]
      categories: [gambling]
    settings:
      logs: { enabled: true, retention: 30d }
    rewrites:
      - { name: nas.lan, content: 192.168.1.10 }
```

### Rules

- **Present = managed, absent = untouched.** Each top-level section (and each key inside
  an object section) is only managed if it appears in the file. A file with just
  `denylist:` never touches security settings. This is what makes partial adoption and
  coexistence with the web UI safe.
- **A managed list is authoritative.** Entries live but not in the file are removed.
  This matches the API (A2: arrays replace) and is what "declarative" means. The
  safety net is in `apply`, not in the format: removals are listed separately in the plan
  and need confirmation (or `--yes`).
- **Sources merge into the list.** `domains` ∪ all `sources`, deduplicated. If the same
  domain appears with different `active` values, that's an error, not last-wins.
- **Shorthands.** A bare string is `{domain: X, active: true}` (or `{id: X}` for catalog
  sections). Durations like `30d` for retention.
- **No secrets.** The API key never appears in the file; the loader rejects an
  `api_key` field rather than ignoring it.
- **Unknown keys are errors**, with the YAML line number. A typo must not silently make
  a section "unmanaged".

### `pull`

Writes the normalised live state (A12 fields stripped, `active: true` elided, catalog
entries as bare ids). Refuses to overwrite an existing file without `--force`; `--stdout`
for piping. `pull` emits inline `domains` only — it cannot know which entries came from a
source — so pulling over a file that uses `sources` is refused with a hint to use `plan`
instead.

## Engine

```
load file ──► validate & normalise ──► fetch sources ──► desired state
                                                              │
GET /profiles/:id ──► normalise (A12) ──► live state ─────────┤
                                                              ▼
                                                   diff per section
                                                              ▼
                                         plan: [atomic PATCH] + [incremental ops]
                                                              ▼
                                       print / --json  ──►  confirm  ──►  execute
                                                                              ▼
                                                            re-fetch & verify convergence
```

### Planning the writes

1. Diff each managed section. Unchanged sections are dropped.
2. Build one PATCH body (A1) from every changed patchable section: changed arrays in
   full (A2), changed object keys alone. Compact JSON, `active` omitted when true (A7).
3. **If the body fits in 100 KiB minus a safety margin** (say 96 KiB): the whole apply is
   one atomic request. This is the common case.
4. **If not**: move the largest list sections out of the PATCH one at a time until it
   fits. Each moved list becomes an incremental delta (POST adds, PATCH `active` changes,
   DELETE removals). Note a list whose *target* fits can always be PATCHed, however big
   the *live* list is, because only the body size matters (A3).
5. Rewrites are always incremental (A10): diff on `(name, content)`; DELETE by server id,
   POST new.

The plan prints which path each section takes and, for incremental work, an estimate
from the limiter ("~1 840 writes, ~32 min at the API's rate limit").

### Executing

- Atomic PATCH first, so most of the profile converges in one step even if incremental
  work later fails.
- Incremental ops go through **one sequential worker paced at 1 write/s** (A11). The
  budget is per key and the windows are fixed, so concurrency can't go faster. It only
  burns the window sooner. v1's thread pool and `--concurrency` go away.
- On a 429 (another tool or a second nextdnsctl using the same key), back off 2, 4, 8, 16,
  30, 30 s before giving up. Read limits clear within seconds; the total spans a write window.
  There is no `Retry-After` to read.
- Treat `200` with an `errors` body as a failure (A5/A6). A6 cases (`duplicate` on POST,
  `404` on DELETE) count as converged.
- Retries: network errors and 5xx on *small* requests only; an oversize body is
  impossible by construction.
- Ctrl-C or a fatal error mid-incremental leaves the profile partially applied. That's
  acceptable because `apply` is idempotent: re-running converges. Exit code `3` and the
  message say so.
- Finally re-fetch and diff again. A non-empty diff means drift (someone edited in the
  UI mid-apply, or an API quirk) and is reported, not retried.

### Mapping errors back

A 400 with a JSON pointer (`/denylist/17/id`) is mapped back through the plan to the
source: `lists/extra.txt:42: "foo..com" rejected by NextDNS (format)`. Catalog errors
(A9) carry no pointer, so catalog ids are validated locally against the public catalogs
(A15) before anything is sent, with a "did you mean" from the same list. If a catalog
endpoint is unavailable, skip local validation and report the whole section on error.

## Input parsing

Strict by default, per the principle that an unclear line is an error, not a guess.

Formats, chosen with `format:` in the file or `--format` on `import`:

| Format | Accepted lines | Errors |
|---|---|---|
| `plain` | one domain; URL **with a scheme** (`https://a.com/x` → `a.com`, kept from v1.2) | anything with whitespace inside, wildcards, bare `a.com/path`, IPs |
| `hosts` | `0.0.0.0 d`, `127.0.0.1 d`, `:: d`, `::1 d` — one or more hostnames. The standard boilerplate names (`localhost`, `localhost.localdomain`, `local`, `broadcasthost`, `ip6-*`) are skipped by an exact-name list | any other address (that's a rewrite, not a block) |
| `adblock` | exactly `\|\|domain^` | `@@` exceptions, `$` modifiers, wildcards, paths, `##` cosmetic rules, `!` is a comment |

All formats: `#` comments (plus `!` in adblock), blank lines, surrounding whitespace.

- **`auto`** (the default for `import`) accepts a source only when every non-comment line
  matches the *same* format. A file mixing `0.0.0.0 x` and `||y^` is an error listing the
  first line of each.
- **Normalisation is limited to exact transformations**: lowercase, IDNA/UTS-46 to
  punycode (the `idna` package is already a dependency via `requests`), strip a single
  trailing dot (an FQDN is the same name). Everything else rejects.
- **One bad line fails the whole source**, reporting up to 20 `file:line: reason` entries
  and the total. `--skip-invalid` (or `skip_invalid: true` per source) downgrades this to a
  warning with the same report.
- Semantics note for the docs: a hosts entry blocks one exact name, while a NextDNS
  denylist entry also blocks subdomains. `||d^` already means "d and subdomains", so
  adblock lines map exactly. `import --format hosts` prints this once.
- Validation regex is aligned with A8: labels `[a-z0-9_-]`, no leading/trailing `-`, TLD
  letters or `xn--…`, ≤ 253 chars, at least two labels.

## Output

- Data on stdout, everything else (progress, retries, warnings) on stderr through
  `logging`. Fixes v1 printing retry messages into `export` output.
- `--json` for `list`, `profile list`, `plan`, `apply` (the plan plus per-op results),
  `logs`. The plan JSON is the same structure the text renderer uses.
- Progress bar only when stderr is a TTY; `-v` prints per-operation lines (replaces the
  v1 coupling of verbose output to `--concurrency 1`).

## Package layout

```
nextdnsctl/
  cli/            # click commands, one module per group; thin
  client.py       # HTTP, limiter, error mapping (no CLI imports)
  model.py        # Profile / sections / normalisation (A12)
  config.py       # file schema, loading, line numbers
  sources.py      # plain/hosts/adblock parsers, fetch
  planner.py      # diff + write strategy
  executor.py     # runs a plan, verifies convergence
  domains.py      # domain validation and normalisation (A8, A16)
  pull.py         # live profiles → YAML
  auth.py         # key storage, XDG paths
```

Packaging: `pyproject.toml` (hatchling), drop `setup.py` and the `requirements*.txt`
duplication, Python 3.10–3.13 in CI, add PyYAML (or ruamel.yaml if we want `pull` to
preserve comments — not needed for 2.0). Optional `flake.nix` for nix users.

## Testing

- **Unit**: parsers (table-driven, including every rejection), normaliser, planner
  (pure: live + desired → plan), size-split logic at the 100 KiB boundary.
- **Fake API**: an in-memory server (e.g. a `responses`/`httpx` mock) that implements
  A1–A11 *including the bad behaviours*: duplicate-wipe on PUT, 500 above 100 KiB,
  60-write window with no `Retry-After`. Executor tests run against it.
- **Live, opt-in** (`pytest -m live`, needs `NEXTDNS_API_KEY`): create an ephemeral
  profile (A13, with logging enabled), run pull/plan/apply round-trips, delete it in a
  finaliser. The suite has to stay inside A11's 60 writes/min, so it goes through the same
  limiter as the product. Not in CI by default. Run it before each release, because the
  facts above are observations and not documented guarantees. Ideally the suite re-checks
  A3, A5 and A11 directly.

## Compatibility

~25 stars and some real users, so breakage should be deliberate and announced, not
avoided at all costs.

**1.4 (bugfix release, no breaking changes)** — ship first, independent of v2
(implemented on branch `release-1.4`):
retry messages to stderr, validation regex aligned with A8 (`_`, punycode TLDs), the
`clear` exit-code bug, `Retry-After` HTTP-date crash, duplicate profile names error,
deprecation warnings for what 2.0 changes.

**2.0 breaking changes:**

| v1 | v2 | Mitigation |
|---|---|---|
| invalid import lines silently skipped | error | `--skip-invalid`; 1.4 warns |
| `denylist add PROFILE d…` | `denylist add -p PROFILE d…` | 2.0 still accepts a leading positional that matches a profile, with a deprecation warning, removed in 2.1 |
| `--concurrency`, `--retry-*` | removed | accepted and ignored with a warning in 2.0 |
| `auth KEY` | `auth login` | `auth KEY` still works in 2.0 with a warning about shell history |
| `~/.nextdnsctl/config.json` | XDG path | read as fallback, migrated on `auth login` |
| `nextdnsctl.api` module functions | removed | no known external users; noted in release notes |

## Milestones

1. **1.4** bugfixes (above). ✅ released 2026-10-04.
2. **Engine core**: client + limiter, model/normaliser, fake API, planner, atomic PATCH
   path, v1 shortcuts rebuilt on it. ✅
3. **Parsers**: plain/hosts/adblock/auto with strict errors. ✅
4. **Declarative**: config file, `pull`/`plan`/`apply` for all sections and rewrites. ✅
5. **Large lists**: size split + paced incremental path; estimates in `plan`. ✅
6. **Observability**: `logs` (polling, A14), `why`, `catalog`. ✅
7. **2.0 release**: docs, migration notes ([migrating-to-2.md](migrating-to-2.md)), live
   test pass (`just test-live`). ✅ live round trip passing; release pending.

## Decisions (formerly open questions)

1. **Catalog validation**: yes, against the public catalogs (A15).
2. **Multiple profiles from one file**: `apply` without `-p` applies every profile in the
   file, sequentially, with one combined plan and one confirmation.
3. **Shared sections across profiles**: YAML anchors. No `extends:`.
4. **Source pinning**: `apply` re-fetches sources and recomputes the plan. If the result
   differs from what `plan` showed in the same invocation, it shows the new plan and asks
   again. A saved plan file (`plan -o` / `apply plan.json`) is a possible later addition.
5. **Rate limits**: measured (A11).

## Worth reporting upstream

- A5: `PUT` on a list with a duplicate id empties the list and returns 200.
- A3: an oversized body returns 500 rather than 413.
- A14: the log stream delivers far fewer events than `/logs` shows.
