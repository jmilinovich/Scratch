"""Command-line entry points: OAuth auth flow and the validation harness."""

from __future__ import annotations

import argparse
import http.server
import secrets
import socket
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

from . import evals
from .config import Config
from .internal import InternalClient
from .official import OfficialAPIError, OfficialClient
from .provider import _pick_sleep_for_date  # noqa: F401  (re-exported convenience)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def _capture_code(redirect_uri: str, timeout: int = 300) -> str | None:
    """Spin up a one-shot loopback server to catch the OAuth redirect."""
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 8080
    captured: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            if "code" in params:
                captured["code"] = params["code"][0]
                captured["state"] = params.get("state", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"WHOOP auth complete. You can close this tab.")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *args):  # silence
            pass

    try:
        server = http.server.HTTPServer((host, port), Handler)
    except OSError as e:
        print(f"  (could not bind {host}:{port}: {e}; use manual paste)")
        return None
    server.timeout = timeout
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    t.join(timeout)
    server.server_close()
    return captured.get("code")


def auth_main(argv: list[str] | None = None) -> int:
    cfg = Config.load()
    if not cfg.has_official:
        print("ERROR: set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET in .env")
        return 2
    client = OfficialClient(cfg)
    state = secrets.token_urlsafe(16)
    url = client.authorization_url(state)
    print("\n1. Open this URL in a browser and approve access:\n")
    print(f"   {url}\n")
    print(f"2. Waiting for redirect to {cfg.redirect_uri} ...")

    code = _capture_code(cfg.redirect_uri)
    if not code:
        print("\n   Did not auto-capture. Paste the full redirect URL (or just the code):")
        raw = input("   > ").strip()
        if "code=" in raw:
            code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [""])[0]
        else:
            code = raw
    if not code:
        print("No code received.")
        return 1
    try:
        client.exchange_code(code)
    except OfficialAPIError as e:
        print(f"Token exchange failed: {e}")
        return 1
    try:
        prof = client.profile()
        who = prof.get("email") or prof.get("first_name") or "ok"
        print(f"\n✅ Authenticated ({who}). Token saved to {cfg.token_file}")
    except OfficialAPIError as e:
        print(f"\nToken saved, but profile fetch failed: {e}")
    return 0


# ---------------------------------------------------------------------------
# validate — Phase 2 decision gate + the six evals
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print_reports(reports: list[evals.EvalReport]) -> bool:
    all_ok = True
    for r in reports:
        mark = "✅" if r.passed else "❌"
        all_ok = all_ok and r.passed
        print(f"   {mark} {r.name:<16} {r.detail}")
    return all_ok


def validate_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate WHOOP HR access paths.")
    ap.add_argument("--days", type=int, default=7, help="look-back window (days)")
    ap.add_argument("--sleep-id", help="test a specific sleep_id instead of the latest")
    args = ap.parse_args(argv)

    cfg = Config.load()
    if not cfg.has_official:
        print("ERROR: official API not configured (WHOOP_CLIENT_ID/SECRET).")
        return 2
    client = OfficialClient(cfg)
    if not client.has_token:
        print("ERROR: no token. Run `whoop-hr-auth` first.")
        return 2

    now = datetime.now(timezone.utc)
    start, end = _iso(now - timedelta(days=args.days)), _iso(now)

    # -- Phase 1: prove auth + list sleeps -----------------------------------
    print("== Phase 1: official API auth ==")
    try:
        prof = client.profile()
        print(f"   profile ok: {prof.get('email') or prof.get('user_id') or 'ok'}")
    except OfficialAPIError as e:
        print(f"   ❌ profile failed: {e}")
        return 1
    sleeps = client.sleep_collection(start, end)
    print(f"   {len(sleeps)} sleep records in last {args.days}d")
    if not sleeps:
        print("   no sleeps to test; widen --days")
        return 1
    target = None
    if args.sleep_id:
        target = next((s for s in sleeps if s.get("id") == args.sleep_id), None)
    target = target or sleeps[0]
    sleep_id = target["id"]
    print(f"   target sleep_id={sleep_id} ({target.get('start')} -> {target.get('end')})")

    # official reference numbers for cross-check
    recovery = None
    cycle_avg = None
    try:
        recs = client.recovery_collection(start, end)
        recovery = next((r for r in recs if r.sleep_id == sleep_id), recs[0] if recs else None)
    except OfficialAPIError:
        pass

    # -- Phase 2: THE KEY TEST — sleep stream --------------------------------
    print("\n== Phase 2: official sleep stream (get_sleep_stream) ==")
    stream_wins = False
    try:
        raw = client.get_sleep_stream(sleep_id, types=["hr"])
        entries = raw.get("stream") or raw.get("data") or raw.get("samples") or []
        print(f"   HTTP 200; top-level keys={list(raw.keys())}; {len(entries)} raw entries")
        series = client.sleep_hr_stream(sleep_id)
        if target.get("start"):
            series.window_start = datetime.fromisoformat(target["start"].replace("Z", "+00:00"))
        if target.get("end"):
            series.window_end = datetime.fromisoformat(target["end"].replace("Z", "+00:00"))
        usable, why = evals.stream_is_usable(series)
        print(f"   parsed {series.count} hr samples — {'usable' if usable else 'UNUSABLE'}: {why}")
        if usable:
            # reproducibility pull
            repro = client.sleep_hr_stream(sleep_id)
            reports = evals.run_all(series, cycle_avg_hr=cycle_avg, recovery=recovery, repro=repro)
            print("   data-quality evals:")
            stream_wins = _print_reports(reports)
    except OfficialAPIError as e:
        print(f"   ❌ stream unavailable: HTTP {e.status} — {e.body[:160]}")

    if stream_wins:
        print("\n🎯 VERDICT: PATH #2 WINS. Official sleep stream delivers usable "
              "intraday HR on the consented path. The internal scraper is unnecessary.")
        return 0

    # -- Phase 3: internal fallback ------------------------------------------
    print("\n== Phase 3: internal BFF fallback ==")
    if not cfg.has_internal:
        print("   internal path disabled. To test it, set WHOOP_ALLOW_INTERNAL=true "
              "plus WHOOP_USERNAME/PASSWORD in .env, then re-run.")
        print("\n⚠️  VERDICT: Path #2 did not deliver and Path #3 is disabled. "
              "No intraday HR available under current config.")
        return 1
    try:
        ic = InternalClient(cfg)
        s_start = target.get("start", start)
        s_end = target.get("end", end)
        series = ic.get_heart_rate(s_start, s_end, step=6, sleep_id=sleep_id)
        usable, why = evals.stream_is_usable(series)
        print(f"   internal 6s HR: {series.count} samples — {'usable' if usable else 'UNUSABLE'}: {why}")
        if usable:
            repro = ic.get_heart_rate(s_start, s_end, step=6, sleep_id=sleep_id)
            reports = evals.run_all(series, cycle_avg_hr=cycle_avg, recovery=recovery, repro=repro)
            print("   data-quality evals:")
            ok = _print_reports(reports)
            if ok:
                print("\n✅ VERDICT: PATH #3 (internal) delivers usable 6s HR. "
                      "Use the fallback — note ToS/fragility costs.")
                return 0
    except Exception as e:  # noqa: BLE001 — surface any failure mode
        print(f"   ❌ internal path failed: {type(e).__name__}: {e}")

    print("\n⚠️  VERDICT: neither path delivered usable intraday HR under current config.")
    return 1


if __name__ == "__main__":
    sys.exit(validate_main())
