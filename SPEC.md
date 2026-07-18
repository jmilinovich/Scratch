# WHOOP Data Platform — Build Spec

Grilled + decided 2026-07-18. This is the source of truth for the hardened
build; it supersedes ad-hoc notes. Mirrors the **nutrition-app** architecture
(the locked template for John's self-hosted health data).

## Mission

Continuously capture **all** of John's WHOOP data — especially true **6-second
overnight HR** — into a durable database, and expose it to Claude (iOS, Desktop,
Code) for physiology analysis (primary use case: alcohol ↔ sleep/HR/HRV).

## What's already proven (this session)

- **Official developer API** (OAuth2) delivers summaries only; the undocumented
  sleep stream is **null-stubbed / partner-gated** — no intraday HR for standard
  apps. Settled empirically.
- **Intraday 6s HR requires the internal web-app API.** It authenticates with a
  **bearer token** from the readable `whoop-auth-token` cookie (the password
  sign-in is Cloudflare/gateway-blocked). Validated: one night pulled clean at
  **4323 samples @ 6.0s** over the exact sleep window.
  - Endpoint: `GET api.prod.whoop.com/metrics-service/v1/metrics/user/{id}?apiVersion=7&name=heart_rate&order=t&step={6|60|600}&start=&end=`
  - Shape: `{name, start, values:[{data:<bpm>, time:<epoch_ms>}]}`
- **Token refresh** is the hard part: gated behind Cloudflare on `app.whoop.com`
  (needs a real browser). `api.prod.whoop.com` is *not* Cloudflare-gated, so
  data pulls work headlessly with a valid bearer token. Token valid ~24h;
  refresh token valid ~weeks.

## Decisions (locked)

| Area | Decision |
|---|---|
| **Data scope** | Everything WHOOP has: 6s/1-min HR, recovery, sleep (stages/resp), cycles, workouts, strain, body measurement |
| **HR resolution** | Full history at **1-min**; true **6s for last ~90d + going forward** (throttled backfill — no big burst) |
| **Storage** | Neon, **raw samples + nightly rollups** (per-night min/avg/max/nadir, time-in-zone, sleep-window stats) |
| **History** | Full backfill once (throttled), then nightly incremental |
| **Events overlay** | `events` table (drinking nights etc.) + easy logging via Hermes Slack / MCP tool — for alcohol↔physiology correlation |
| **Compute host** | **mili-root** box (always-on) runs refresh + ingestion cron |
| **Database** | **Neon `nutrition` project** (`polished-dust-97465559`, us-west-2) — new database/schema `whoop`; reuse existing Neon (PITR, no box-disk durability) |
| **MCP** | **Vercel + Clerk OAuth** (mirror nutrition-app `apps/mcp`) — Claude iOS/Desktop/Code; reads Neon |
| **Refresh** | **Lazy** (refresh on 401 / near-expiry), **Slack alert** on failure |
| **Box login** | **Seed from Mac**: one-time WHOOP login/bootstrap on Mac → transplant session into a persistent Chrome profile on the box |
| **Repo** | New private **`jmilinovich/whoop-data`**, pnpm/turbo monorepo mirroring nutrition-app |
| **Alerts/logging** | Hermes Slack — `#gtd-hermes` (`C0B59EL8119`) or DM John (`U16QMQD25`) |

## Architecture

```
                          ┌─────────────────────────────┐
  WHOOP APIs              │        mili-root box         │
  ─────────               │  (always-on, live-ops rules) │
  official OAuth  ◄───────┤  ingestion (Python)          │
  internal bearer ◄───────┤   • whoop-hr client (built)  │
                          │   • nightly cron + backfill  │
  app.whoop.com   ◄───────┤  token refresher (Playwright │
  (Cloudflare)            │   headed Chrome + Xvfb,      │
                          │   persistent profile)        │
                          │  circuit-breaker, fail-loud  │
                          └───────────────┬──────────────┘
                                          │ writes
                                          ▼
                          ┌─────────────────────────────┐
                          │   Neon (nutrition project)   │
                          │   database/schema `whoop`    │
                          │   raw HR + rollups + events  │
                          └───────────────┬──────────────┘
                                          │ reads
                                          ▼
                          ┌─────────────────────────────┐
                          │   Vercel — apps/mcp (TS)     │
                          │   mcp-handler + Clerk OAuth  │
                          │   Claude iOS / Desktop / Code │
                          └─────────────────────────────┘

  Alerts / event-logging  ──►  Hermes Slack (#gtd-hermes / DM John)
```

**Tech stack = hybrid, decoupled by the DB:** ingestion + refresh stay **Python**
(reuse the validated `whoop-hr` client + Playwright); the MCP is **TypeScript**
on Vercel and only *reads* Neon — it never touches WHOOP. Clean seam.

## Data model (Neon schema `whoop`)

- `hr_samples(user_id, ts timestamptz, bpm smallint, resolution enum(6s,1min), PRIMARY KEY(user_id, ts, resolution))` — partitioned by month; raw series.
- `hr_night_rollup(user_id, night_date, sleep_id, start, end, samples, min_bpm, avg_bpm, max_bpm, nadir_bpm, minutes, coverage_pct, …)` — fast queries.
- `recovery(user_id, cycle_id, date, recovery_score, resting_hr, hrv_rmssd_milli, spo2_pct, skin_temp_c)`
- `sleep(user_id, sleep_id, start, end, resp_rate, perf_pct, eff_pct, disturbances, stage_ms{in_bed,awake,light,sws,rem}, nap bool)`
- `cycle(user_id, cycle_id, start, end, strain, avg_hr, kilojoules)`
- `workout(user_id, workout_id, sport, start, end, strain, avg_hr, max_hr, kilojoules, zones)`
- `body_measurement(user_id, date, height, weight, max_hr)`
- `events(user_id, ts, kind enum(alcohol,…), magnitude, note, source)` — the analysis overlay.
- `ingest_log(run_id, started, finished, kind, window, rows, status, error)` — observability + idempotency.

Idempotent upserts (ON CONFLICT). Multi-user-ready (keyed by `user_id`) though
only John now.

## Token refresh (expert-informed)

Anti-bot expert: vanilla headless Playwright gets Cloudflare-flagged. **Ship:**
- `launch_persistent_context(user_data_dir=~/.whoop-profile, channel="chrome", headless=False)` under **Xvfb** on the box; `playwright-stealth` as cheap insurance. The **aged persistent profile** (carries `cf_clearance`/`__cf_bm`) is the biggest reliability lever.
- Don't trust bare navigation: `goto(app.whoop.com)` → `wait_for_response(**/metrics-service/** | **/users-service/**, 200)` → poll `context.cookies()` until `whoop-auth-token` JWT `exp` advances **> now+2h** → **atomic** write of the token file (temp+rename, chmod 600).
- **Fail loud** on: login redirect (session dead → Slack "re-login needed"), Cloudflare interstitial ("Just a moment" → screenshot + alert), silent stale token (new `exp` not > old → fail). Data pulls **hard-fail on 401** so a stale token can't pass silently.
- Alternative kept in back pocket: CDP into a genuinely-running Chrome (best reliability if a persistent session exists on the box).

## Security non-negotiables (expert-informed)

1. **Secrets at rest**: refresh token + Chrome profile = weeks-long credential. Store under one dir `chmod 700` / files `600`, owned by the run user, on an **encrypted volume** (box is Linux/plaintext by default). Never in git, never synced to cloud storage. Prefer Neon-side data durability; nothing sensitive on box disk unencrypted.
2. **Circuit breaker + backoff** on the refresher — a failed refresh backs off (minutes→hours), never tight-loops the login/refresh endpoint. Lockfile against concurrent cron runs.
3. **No secrets / raw biometrics in logs or commits** — redact `Authorization` everywhere; **pre-commit secret scan**; `data/` and token files gitignored.
4. **ToS reality**: internal API use violates WHOOP ToS; own-data only. Politeness = human-rhythm cadence (1 pull/day post-wake, no burst backfill, honor 429/Retry-After, serial + jitter). Keep the official API as the summary fallback. Accept WHOOP may revoke at will.

## Backfill plan

- Summaries (recovery/sleep/cycles/workouts/body) — full history via official API (paginated, 429-aware).
- 1-min HR — full history via internal `step=60`, **throttled over days** (chunked windows, sleep between, resumable via `ingest_log`).
- 6s HR — last ~90d + nightly forward via internal `step=6`.

## Repo layout (`jmilinovich/whoop-data`, pnpm/turbo)

```
packages/core        # TS shared types + Neon data-access (reads/writes)
apps/mcp             # Next.js MCP on Vercel (mcp-handler + Clerk) — reads Neon
ingestion/           # Python: whoop-hr client (ported from Scratch) + backfill + nightly + refresher
infra/db             # SQL migrations for schema `whoop`
infra/box            # systemd/cron units, Xvfb, deploy notes for mili-root
docs/                # this spec + runbook
```
The validated Python `whoop-hr` package (client, evals, provider, bootstrap)
ports from the current Scratch repo into `ingestion/`.

## Build phases (for the ultracode run)

1. **Scaffold** repo + Neon `whoop` db (via `vercel env pull` → DATABASE_URL) + migrations.
2. **Ingestion** (Python on box): port whoop-hr, add all-data pullers, rollups, idempotent writers, `ingest_log`.
3. **Refresher** (Playwright headed/Xvfb, persistent profile, circuit breaker, Slack alert) + Mac→box seeding.
4. **Backfill** (throttled) + **nightly cron** + monitoring.
5. **MCP** on Vercel + Clerk (mirror apps/mcp): tools over Neon (get_night_hr, compare_nights, recovery_trend, log_event, …).
6. **Alerts/events** via Hermes Slack; **verify** end-to-end; pre-commit secret scan.

## Access still needed from John

- `vercel link` the nutrition MCP project so `vercel env pull` yields the Neon `DATABASE_URL` (John is logged in as `jrmilinovich-6218`).
- One-time WHOOP browser bootstrap on the Mac to seed the box profile.
- Confirm Clerk app to reuse vs new for the WHOOP MCP.
