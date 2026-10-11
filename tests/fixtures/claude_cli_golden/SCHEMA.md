# Claude CLI `--output-format json` envelope schema

The `ClaudeCLIModelProvider` (`src/memory_agent/adapters/models/__init__.py`)
parses the JSON envelope that `claude -p --output-format json` writes to
stdout. Three envelope shapes are accepted (see `_extract_candidates`):

1. **Direct** — the envelope carries the candidate list at top level:
   ```json
   {
     "type": "result",
     "is_error": false,
     "model": "claude-3.5-sonnet",
     "candidates": [
       { "candidate_id": "cand_001", "kind": "requirement", ... }
     ]
   }
   ```

2. **Wrapped (string)** — `result` is a JSON-encoded string that decodes
   to an object with a `candidates` list:
   ```json
   {
     "type": "result",
     "is_error": false,
     "model": "claude-3.5-sonnet",
     "result": "{\"candidates\":[{\"candidate_id\":\"cand_001\", ...}]}"
   }
   ```

3. **Wrapped (object)** — `result` is itself an object with a
   `candidates` list (legacy Claude Code JSON shape):
   ```json
   {
     "type": "result",
     "is_error": false,
     "result": { "candidates": [ { ... } ] }
   }
   ```

Error paths the provider recognises:
- `is_error: true` → `status="malformed"`, `error_code="model_error"`
- `returncode != 0` with `auth` / `oauth` in stderr → `status="auth_error"`
- `returncode != 0` without auth hint → `status="malformed"`
- timeout → `status="timeout"`
- `cli_missing` (executable not on PATH) → `status="provider_unavailable"`
- `json.JSONDecodeError` on stdout → `status="malformed"`, `error_code="bad_json"`
- envelope missing any of the three `candidates` paths → `status="malformed"`, `error_code="no_candidates_field"`

## How to record a fresh golden

```bash
# 1. Run the real CLI on a sanitised fixture, capturing stdout + stderr.
#    Do NOT run this against a real user session.
claude -p "$(cat tests/fixtures/claude_cli_golden/_prompt.txt)" \
  --output-format json \
  --tools "" \
  --no-session-persistence \
  --disable-slash-commands \
  > /tmp/live_capture.json 2> /tmp/live_capture.err

# 2. Scrub: replace any session-specific model name with the documented
#    placeholder ``"claude-3.5-sonnet"``; verify no real user text is in
#    the captured JSON. Commit alongside SCHEMA.md.

# 3. Drive ``FixtureReplayModelProvider`` from CI by pointing
#    ``MEMORY_AGENT_MODEL_FIXTURE_DIR`` at this directory.
```

## Why fixtures instead of stub-CLIs

The v0.1 / v1 stub-CLI tests (`tests/unit/test_model_provider.py`) drive
the subprocess path with a fake binary that emits a hand-written
envelope. The replay path uses a *captured* envelope — the schema this
file documents — so CI exercises the parsing path against the same
shape the live CLI would emit, without the OAuth and nested-invocation
constraints that block a real `claude -p` smoke in CI.
