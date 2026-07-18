# whoop-hr

Per-sample overnight **heart-rate** from WHOOP, plus a thin **MCP server** so a
Claude instance can query it. Built to prefer the **official, consented API**
and only fall back to the reverse-engineered web-app API if the official path
provably can't deliver intraday HR.

> **Status:** the code (all three access paths, the runtime selector, the MCP
> server, and the validation/eval harness) is complete and unit-tested. The
> live decision — *does the official sleep stream actually return usable
> per-sample HR?* — requires **your** WHOOP developer app + account and is run
> with one command (`whoop-hr-validate`). It has not been run here because this
> build environment has no WHOOP credentials.

## The three access paths

| # | Path | Auth | Intraday HR? | Cost |
|---|------|------|--------------|------|
| 1 | Official developer API (`/developer/v2/...`) | OAuth2 auth-code | summary only | none — consented, stable |
| 2 | **Undocumented sleep stream** (`/developer/v2/activity/sleep/{id}/stream`) | same OAuth2 | **maybe — the key test** | none if it works |
| 3 | Internal "BFF" web-app API (`/metrics-service/v1/...`) | account password | yes, true 6s HR | ⚠️ against ToS, fragile |

The whole strategy hinges on **path #2**. If `get_sleep_stream(sleep_id,
["hr"])` returns real per-sample overnight HR on a normal developer app, the
problem is solved on the safe path and the scraper is unnecessary. If it
404s / 403s / returns zero-stubbed data, we fall back to path #3.

`get_recovery` and `get_sleep_summary` **always** use the official API (path #1)
— they're consented and always useful regardless of how the HR question
resolves.

## Setup (Phase 0)

1. Create a developer app at <https://developer-dashboard.whoop.com> →
   `client_id`, `client_secret`, and register a redirect URI
   (default `http://localhost:8080/callback`).
2. Configure:
   ```bash
   cp .env.example .env      # then fill in client_id / client_secret
   uv venv && source .venv/bin/activate
   uv pip install -e .
   ```
   `.env` and all token files are gitignored — nothing secret is committed.

## Run it

```bash
# Phase 1 — OAuth handshake (opens a URL, captures the redirect locally)
whoop-hr-auth

# Phase 2 — THE KEY TEST + data-quality evals. Prints a verdict:
#   "PATH #2 WINS" | falls through to Path #3 | "no intraday HR available"
whoop-hr-validate --days 7
```

`whoop-hr-validate` runs the decision gate end-to-end: authenticates, lists
recent sleeps, calls the sleep stream on the latest `sleep_id`, records the
exact HTTP outcome, and — if data comes back — runs the six evals below before
declaring a winner. It never declares success on a bare HTTP 200.

### The six evals (data quality, not HTTP status)

1. **Coverage** — % of expected samples present across the sleep window.
2. **Cadence** — median inter-sample gap (flags "advertised dense, returns 5-min buckets").
3. **Physiology** — overnight HR sits in a plausible band, dips to a nadir, no `0`/`>200`-at-rest; rejects zero-stubbed streams.
4. **Cross-check** — stream's average roughly matches the official cycle `average_heart_rate` and recovery `resting_heart_rate`.
5. **Reproducibility** — same night pulled twice is identical.
6. **Token lifecycle** — a pull >1h after auth confirms refresh fires (token auto-refreshes 60s before expiry; run `whoop-hr-validate` again the next day to exercise it).

### Enabling the fallback (path #3) — only if Phase 2 fails

The internal API uses your real WHOOP **password**, is undocumented, can break
without notice, and **violates WHOOP's ToS**. It stays off unless you opt in:

```bash
# in .env
WHOOP_ALLOW_INTERNAL=true
WHOOP_USERNAME=you@example.com
WHOOP_PASSWORD=...
```

It's isolated in `src/whoop_hr/internal.py` behind an `Endpoints` table, so a
WHOOP change is a one-line fix and the whole module is rip-and-replaceable.

## MCP server

```bash
whoop-hr-mcp        # stdio transport
```

Tools exposed:

- `get_sleep_hr(date)` — intraday HR for the night ending `YYYY-MM-DD`. The
  response's `source` field says which path served it (`official_stream` vs
  `internal_bff`) and `used_fallback` flags path #3.
- `get_recovery(start, end)` — recovery_score, resting_heart_rate,
  hrv_rmssd_milli, spo2_percentage, skin_temp_celsius.
- `get_sleep_summary(start, end)` — stage durations, respiratory_rate,
  performance/efficiency %, disturbances.

Register with Claude Desktop / Claude Code (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "whoop-hr": {
      "command": "whoop-hr-mcp",
      "env": {
        "WHOOP_CLIENT_ID": "…",
        "WHOOP_CLIENT_SECRET": "…",
        "WHOOP_TOKEN_FILE": "/absolute/path/.whoop_token.json"
      }
    }
  }
}
```

(Run `whoop-hr-auth` once first so the token file exists.)

## Architecture

```
config.py     env/.env loading, no dep for parsing
models.py     HRSample/HRSeries/Recovery/SleepSummary + coverage/cadence math (stdlib only)
official.py   path #1/#2: OAuth2 + auto-refresh + collections + get_sleep_stream
internal.py   path #3: BFF sign-in + metrics-service 6s HR (isolated, ToS-flagged)
evals.py      the six data-quality checks + the selector's usability gate
provider.py   WhoopHR: unified interface + runtime selector (prefer stream, fall back)
server.py     FastMCP server (3 tools)
cli.py        whoop-hr-auth, whoop-hr-validate
```

~150 lines of glue own the selection logic; the endpoint knowledge mirrors the
two maintained clients called out in the brief (`hedgertronic/whoop` for the
official surface incl. the stream, `jjur/whoop-data` for the internal surface),
re-implemented on `httpx` so all three paths share one coherent interface.

## Guardrails

- Secrets/tokens live in `.env` / a local token file (chmod 600), never logged,
  never committed. The internal client never echoes the password.
- Official API 429s are honored (`Retry-After`); official token refreshes 60s
  early and retries once on a 401.
- The consented path is preferred at every fork; you only pay the
  credential/fragility/ToS tax if Phase 2 proves you must.

## Tests

```bash
uv pip install -e '.[dev]'
pytest            # 14 tests, no network — exercises parsing + all six evals
```
