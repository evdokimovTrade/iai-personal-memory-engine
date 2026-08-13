# Contributing to iai-pme

Thanks for taking the time. This is a solo-maintained project, so the fastest
way to get a change merged is to make it easy to review: small, scoped, with
the checks already green.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Scope

Changes that are always welcome:

- **Bug fixes**, especially with a failing test that the fix turns green.
- **Windows support.** The runtime is ported and validated on Windows 11, but
  the test suite is not. Anything that moves that forward is high value.
- **Host integrations.** Ambient capture ships for Claude Code and Codex.
  Wiring native hooks for other MCP CLIs (Gemini, Cursor, Zed, Continue.dev …)
  is the single best first contribution.
- **Documentation** that corrects something wrong or stale. Prose fixes count.
- **Benchmarks** that make an existing claim more honest or more reproducible.

Changes worth opening an issue for *before* you write the code:

- New MCP tools or changes to the tool surface — it is a committed public API
  and stays stable across `2.x`.
- On-disk store or schema changes — they need a migration path.
- New runtime dependencies. The dependency floor is deliberate: permissive
  licences only (MIT/Apache-2.0/BSD), no GPL, no ML stack in the hot path.
- Anything touching the crypto path. The primitives are not ours to reinvent.

## Development setup

```bash
git clone https://github.com/CodeAbra/iai-personal-memory-engine.git
cd iai-personal-memory-engine
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

`pip install` builds the native Rust engine via `setuptools-rust`, so a Rust
toolchain is required. On Linux you also need `libssl-dev` and `pkg-config`.
To rebuild the extension by hand after editing Rust sources:

```bash
iai-mcp build-native
```

## Tests

The correctness gate — the same one CI runs, and the one your PR must pass:

```bash
pytest -m "not perf and not slow and not live"
```

Three opt-in suites stay out of that gate because they are slow or need a real
daemon:

```bash
pytest -m slow --runslow      # subprocess-heavy resolution tests
pytest -m perf --perf         # wall-clock latency benches
pytest -m live --live         # real daemon-subprocess end-to-end gate
```

Lint:

```bash
ruff check .
```

CI runs the correctness gate and a wheel build on macOS for every pull request,
plus a secret scan. Linux tests and lint run weekly in
[extended checks](.github/workflows/extended-checks.yml); if your change is
Linux-specific, run them locally and say so in the PR.

## Benchmarks

If your change touches **retrieval, capture, or consolidation**, include
before/after numbers from the relevant harness. Every claim in the README ships
with the harness that proves it, and that only holds if changes are measured:

```bash
python -m bench.longmemeval_blind            # LongMemEval-S (raw)
python -m bench.contradiction_longitudinal   # Rescue@10 / longitudinal
python -m bench.personal_fact_drift          # drift / retention
python bench/sleep_ablation.py               # sleep-consolidation recall
python -m bench.tokens                       # session-start token cost
python -m bench.neural_map                   # recall latency
python -m bench.memory_footprint             # RAM footprint
```

Latency and footprint numbers are hardware-dependent — report your machine
alongside them. Retrieval-correctness numbers are not: a regression there is a
regression everywhere.

Report what you measured, including a regression. A PR that says "R@5 dropped
0.004, here is why that is acceptable" is far more useful than one that omits
the number.

## Pull requests

- One logical change per PR.
- Fill in the [PR template](.github/PULL_REQUEST_TEMPLATE.md) — it asks for the
  affected areas, the tests you ran, and bench numbers where they apply.
- Add tests for changed behaviour, or say in the PR why none apply.
- Match the surrounding code: the codebase favours explicit names, docstrings
  that explain *why*, and comments only where the reason is not obvious.
- Update the docs in the same PR when behaviour changes. A `doctor` check, a
  CLI subcommand, or an environment variable that is not in the README is a
  bug in the README.
- Never commit secrets, store contents, keys, or captured memory. The store is
  personal data by construction.

## Reporting bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md) and include
the output of:

```bash
iai-mcp doctor
iai-mcp daemon status
```

Redact anything personal from log excerpts before posting — capture logs can
contain conversation content.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
