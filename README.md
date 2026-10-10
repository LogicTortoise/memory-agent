# Memory Agent

> **Status: v0.1 baseline.** The keyword recall + conservative reconcile baseline ships.
>
> See [docs/14-v1-development-plan.md](docs/14-v1-development-plan.md) for the v0.1 / v1 scope and [docs/15-v1-acceptance.md](docs/15-v1-acceptance.md) for the v0.1 acceptance report.

A local, file-backed personal memory agent. It scans Claude Code and Codex session logs (with sanitised fixtures, never your real history unless you point it there), extracts explicit user requirements with a conservative reconciler, and serves them back through a keyword recall command. **v0.1 is real, runnable, and self-contained** — no worker, no Telegram, no UI.

This repository implements [docs/14-v1-development-plan.md](docs/14-v1-development-plan.md). v1 work (BM25, worker, Telegram, HTTP/UI, privacy/backup) lands in subsequent commits. The README in the v1.0.0 tag describes the full release.

## Quick start (with sanitised fixtures)

```bash
python3 -m pip install -e ".[dev]"

# Create a sanitised Claude Code session for testing
mkdir -p /tmp/fake_claude/projects/proj_x
cat > /tmp/fake_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u01","message":{"role":"user","content":"use Python for the prototype"},"timestamp":"2026-10-08T10:00:00Z","sessionId":"sess1","parentUuid":null}
{"type":"assistant","uuid":"a01","message":{"role":"assistant","content":"OK, will use Python."},"timestamp":"2026-10-08T10:00:01Z","sessionId":"sess1","parentUuid":"u01"}
EOF

python3 -m memory_agent --vault /tmp/my_vault init --root "claude-code=/tmp/fake_claude"

cat >> /tmp/fake_claude/projects/proj_x/sess1.jsonl <<'EOF'
{"type":"user","uuid":"u02","message":{"role":"user","content":"use Python for the prototype always"},"timestamp":"2026-10-08T10:01:00Z","sessionId":"sess1","parentUuid":"a01"}
EOF

MEMORY_AGENT_MODEL_PROVIDER=fake python3 -m memory_agent --vault /tmp/my_vault maintain --root "claude-code=/tmp/fake_claude"
python3 -m memory_agent --vault /tmp/my_vault recall --query "Python prototype"

python3 -m memory_agent --vault /tmp/my_vault doctor
```

The CLI prints exactly one JSON CommandResponse per invocation on stdout; diagnostics go to stderr.

> **Real Claude auth is required for production.** `cli` is the default provider; until you set `MEMORY_AGENT_ENABLE_CLAUDE_CLI=1` and have `claude` on `PATH`, `maintain` will fail with a real provider-unavailable error rather than silently producing empty memories.

## Tests

```bash
python3 -m pytest tests/ -q
```

v0.1 covers the per-P0 acceptance table in [docs/15-v1-acceptance.md](docs/15-v1-acceptance.md).

## Repository layout

```
memory-agent/
  pyproject.toml
  src/memory_agent/
    contracts/            # versioned, validated wire types (Pydantic v2)
    domain/               # pure DP rules — batching, scope, lifecycle, evidence
    application/          # use-case orchestration; depends on ports only
    adapters/
      sessions/           # Claude Code + Codex readers
      models/             # Fake (test) + Claude CLI providers
    infrastructure/
      sqlite/             # WAL + migrations + repositories
      files/              # atomic write, immutable segments, vault layout
    interfaces/
      cli/                # `memory` / `python -m memory_agent`
  tests/
    fixtures/             # sanitised session/contract fixtures (never real user data)
    unit/                 # contract + domain + model-provider tests
    integration/          # SQLite, scan, plan, source binding, CLI e2e
  docs/                   # 00–15 design + plan + v0.1 acceptance
  .workspace/             # historical research artefacts (gitignored)
```

This local repository has no configured Git remote and no published GitHub repository. `pyproject.toml` therefore declares no `Homepage` / `Source` / `Issues` URLs: those fields would advertise a repository that does not exist. Add them back when a remote actually exists.