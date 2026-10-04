# Migrating from nextdnsctl 1.x to 2.0

2.0 adds declarative profiles (`pull` / `plan` / `apply`) and rebuilds the existing
commands on the same engine. Most 1.x commands keep working, some with a deprecation
warning. These are the changes you may notice.

## Changed behaviour

| 1.x | 2.0 | What to do |
|---|---|---|
| Invalid lines in `import` were skipped | The import fails and lists the lines | Fix the source, or pass `--skip-invalid` |
| Invalid domains in `add` were skipped | The command fails and lists them | Remove them from the command |
| `example.com/path` and `example.com:8080` were reduced to `example.com` | Rejected as ambiguous | Pass the domain, or a full URL with a scheme (`https://example.com/path` still works) |
| `denylist add PROFILE domain…` | `denylist add -p PROFILE domain…` (or `NEXTDNS_PROFILE`) | The old form still works in 2.0 with a warning; it will be removed in 2.1 |
| `profile-list` | `profile list` | The old name still works with a warning |
| `auth KEY` | `auth login` (prompts, or reads stdin) | The old form still works but warns that the key ends up in your shell history |
| `--concurrency`, `--retry-attempts`, `--retry-delay` | No effect | Remove them; writes are paced to NextDNS's rate limit automatically |
| `add`/`import` sent one request per domain | One request for the whole change | Nothing; it's faster and atomic |
| Key stored in `~/.nextdnsctl/config.json` | `~/.config/nextdnsctl/config.json` | The old file is still read; `auth login` writes the new one, `auth logout` removes both |
| `list` printed `Total: N` | Still does (on stderr), `--json` for scripts | — |

## Removed

- The `nextdnsctl.api` and `nextdnsctl.nextdnsctl` Python modules. If you imported them,
  use `nextdnsctl.client.Client` instead.

## New

- `pull`, `plan`, `apply` for whole profiles — see the [README](../README.md#declarative-profiles).
- `--format plain|hosts|adblock` for imports.
- `rewrites`, `profile create/delete`, `catalog`, `logs`, `why`.
- `--json` on every command that prints data.
