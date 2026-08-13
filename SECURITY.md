# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub Security Advisories:

→ [Report a vulnerability](https://github.com/CodeAbra/iai-personal-memory-engine/security/advisories/new)

That channel is private between you and the maintainers until an advisory is
published.

What to include:

- What the issue is and which component it affects (daemon, store, crypto path,
  MCP wrapper, capture hooks, CLI).
- Version (`iai --version`) and platform.
- Reproduction steps, or a proof of concept if you have one.
- What an attacker gains — read of stored memory, key recovery, code execution,
  denial of service.

Please redact your own memory contents from any excerpt you attach.

### What to expect

- Acknowledgement within **7 days**.
- An initial assessment — accepted, needs more information, or out of scope —
  within **14 days**.
- A fix for an accepted vulnerability in a patch release, credited to you in
  the advisory and the changelog unless you ask otherwise.

This is a solo-maintained project with no enterprise SLA. The timelines above
are what the maintainers aim for, not a contractual commitment.

## Supported versions

| Version | Supported |
|---|---|
| 2.4.x | ✅ Current — fixes land here |
| 2.3.x and older | ❌ Upgrade to the current release |
| 1.x | ❌ End of life |

## Scope

The threat model is a **single-user machine**: iai-pme stores one person's
memory locally, listens on a Unix socket, and makes no network calls of its
own. In scope:

- Anything that reads or decrypts store contents without the key.
- Key material leaking to disk, logs, process listings, or another user account.
- Weak file permissions on `~/.iai-mcp/` — the key file must stay mode `0600`.
- Privilege escalation or code execution through the daemon socket, the MCP
  wrapper, the capture hooks, or the installed launchd/systemd unit.
- A local unprivileged user, or another process on the same machine, reaching
  data or an interface it should not.
- Injection of attacker-controlled content into the recall prefix in a way that
  crosses a trust boundary.

Out of scope:

- An attacker who already has root, or your login session, on your machine.
  They have your key file, and no application-level control changes that.
- Losing the encryption key. There is no recovery path — that is the design.
- Vulnerabilities in upstream dependencies with no exploitable path through
  this project. Report those upstream; tell us if we should pin around them.
- The model provider your MCP host talks to. iai-pme does not proxy those calls.
- Anything requiring a store you deliberately handed to someone else.

## Handling of your data

Stated here so a reporter knows what a compromise would expose: all records are
encrypted at rest with AES-256-GCM; the key lives at `~/.iai-mcp/.crypto.key`
(mode `0600`) or is derived from `IAI_MCP_CRYPTO_PASSPHRASE`. Embeddings are
computed locally. There is no telemetry and no account. The only data leaving
the machine is the normal model call your MCP client already makes.
