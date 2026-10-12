"""Run one real SDK arm. Credentials are loaded only when explicitly executed.

Execute in an isolated process/venv; source overrides distinguish baseline/new.
No browser login, account rotation, or mutation operation is performed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import hashlib
import subprocess
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from common import BenchmarkStop, WireAudit, in_window, write_results

PINS = {"twikit": "2.3.3", "twscrape": "0.20.1", "pyxcom-baseline": "1.0.1"}


def collect_pyxcom(cookies, args, records):
    from pyxcom import XClient

    # Exercise actual public iterator, replacing only browser credential lookup.
    with patch("pyxcom.client.load_x_cookies", return_value=cookies):
        with XClient(delay=0, timeout=args.timeout, proxy=args.proxy) as client:
            for item in client.iter_search_query(
                args.query, since=args.since, until=args.until, max_pages=args.attempts
            ):
                records.append({"id": str(item.id), "created_at": item.created_at_utc})


async def collect_twikit(cookies, args, records):
    from twikit import Client

    client = Client(proxy=args.proxy, timeout=args.timeout)
    client.set_cookies(cookies)
    try:
        page = await client.search_tweet(args.effective_query, "Latest", count=20)
        for index in range(args.attempts):
            records.extend({"id": str(p.id), "created_at": p.created_at} for p in page)
            if not page.next_cursor or index + 1 >= args.attempts:
                break
            page = await page.next()
    finally:
        await client.http.aclose()


async def collect_twscrape(cookies, args, records):
    from twscrape import API, AccountsPool
    from twscrape.logger import logger
    from twscrape.models import parse_tweets

    logger.disable("twscrape")
    # This SDK requires SQLite session storage; it exists only in a private temp
    # directory and is removed even when the request/deadline guard interrupts.
    with tempfile.TemporaryDirectory(prefix="pyxcom-benchmark-session-") as private:
        os.chmod(private, 0o700)
        pool = AccountsPool(
            str(Path(private) / "session.db"), raise_when_no_account=True
        )
        await pool.add_account(
            "benchmark", "", "", "", cookies=json.dumps(cookies), proxy=args.proxy
        )
        api = API(pool, proxy=args.proxy, raise_when_no_account=True)
        async for response in api.search_raw(args.effective_query):
            records.extend(
                {"id": str(p.id), "created_at": p.date.isoformat()}
                for p in parse_tweets(response)
            )


def _deadline(signum, frame):
    raise BenchmarkStop("time_budget")


def run(args, cookies):
    import httpx

    if args.source:
        sys.path.insert(0, str(Path(args.source).resolve()))
    package = "pyXcom" if args.arm.startswith("pyxcom") else args.arm
    version = importlib.metadata.version(package)
    source_fingerprint = None
    if args.source:
        source_root = Path(args.source).resolve()
        code_hashes = {
            str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((source_root / "pyxcom").rglob("*.py"))
        }
        code_sha = hashlib.sha256(
            json.dumps(code_hashes, sort_keys=True).encode()
        ).hexdigest()
        commit = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(source_root), "status", "--porcelain", "--", "."],
                capture_output=True,
                text=True,
            ).stdout
        )
        import pyxcom

        version = pyxcom.__version__
        source_fingerprint = {
            "commit": commit,
            "source_dirty": dirty,
            "python_code_sha256": code_sha,
            "python_files": code_hashes,
        }
    if args.arm in PINS and version != PINS[args.arm]:
        raise ValueError(f"Expected {package} {PINS[args.arm]}, installed {version}")
    args.effective_query = f"{args.query} since:{args.since} until:{args.until}"
    records = []
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    audit = WireAudit(
        output / "raw",
        max_attempts=args.attempts,
        seconds=args.seconds,
        secrets=cookies.values(),
    )
    started = time.perf_counter()
    reason = "returned"
    error_frames = []
    diagnostic = None
    previous = signal.signal(signal.SIGALRM, _deadline)
    signal.setitimer(signal.ITIMER_REAL, args.seconds)
    # Disable connection-layer retries for every arm so one measured HTTPX send
    # equals one wire attempt. Higher SDK retries remain counted by WireAudit.
    sync_init = httpx.HTTPTransport.__init__
    async_init = httpx.AsyncHTTPTransport.__init__
    client_init = httpx.AsyncClient.__init__

    def transport_init(self, *a, **kw):
        kw["retries"] = 0
        sync_init(self, *a, **kw)

    def atransport_init(self, *a, **kw):
        kw["retries"] = 0
        async_init(self, *a, **kw)

    def aclient_init(self, *a, **kw):
        kw["timeout"] = args.timeout
        client_init(self, *a, **kw)

    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(httpx.HTTPTransport, "__init__", transport_init)
            )
            stack.enter_context(
                patch.object(httpx.AsyncHTTPTransport, "__init__", atransport_init)
            )
            stack.enter_context(
                patch.object(httpx.AsyncClient, "__init__", aclient_init)
            )
            stack.enter_context(patch.dict(os.environ, {"TWS_HTTP_BACKEND": "httpx"}))
            stack.enter_context(audit.intercept())
            if args.arm.startswith("pyxcom"):
                collect_pyxcom(cookies, args, records)
            elif args.arm == "twikit":
                asyncio.run(collect_twikit(cookies, args, records))
            else:
                asyncio.run(collect_twscrape(cookies, args, records))
    except BenchmarkStop as exc:
        reason = str(exc)
    except Exception as exc:
        reason = "error:" + type(exc).__name__
        error_frames = [
            {"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
            for f in traceback.extract_tb(exc.__traceback__)
        ]
        known_errors = {
            "Couldn't get KEY_BYTE indices": "transaction_indices_missing",
            "Couldn't get key from the page source": "transaction_verification_key_missing",
            "invalid response": "transaction_invalid_html",
        }
        diagnostic = known_errors.get(str(exc))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    ids = sorted({r["id"] for r in records})
    usable = sorted(
        {r["id"] for r in records if in_window(r["created_at"], args.since, args.until)}
    )
    result = {
        "arm": args.arm,
        "version": version,
        "source_fingerprint": source_fingerprint,
        "observer_id": args.observer_id,
        "observer_binding": "caller_verified",
        "query": args.effective_query,
        "elapsed_seconds": time.perf_counter() - started,
        "data_attempts": audit.data_attempts,
        "setup_attempts": audit.setup_attempts,
        "stop_reason": reason,
        "diagnostic": diagnostic,
        "error_frames": error_frames,
        "returned_record_count": len(records),
        "returned_unique_ids": ids,
        "usable_unique_ids": usable,
        "usable_unique_count": len(usable),
        "events": audit.events,
    }
    config = {
        k: v for k, v in vars(args).items() if k not in {"proxy", "profile", "source"}
    }
    config.update(
        {
            "connection_retries": 0,
            "per_request_timeout_seconds": args.timeout,
            "mode": "live",
            "pins": PINS,
        }
    )
    write_results(output, config, [result])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm",
        choices=["pyxcom-baseline", "pyxcom-current", "twikit", "twscrape"],
        required=True,
    )
    parser.add_argument("--query", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument(
        "--observer-id",
        required=True,
        help="ID independently verified by the coordinating process",
    )
    parser.add_argument("--browser", default="chrome")
    parser.add_argument("--profile")
    parser.add_argument(
        "--cookies-stdin",
        action="store_true",
        help="Read JSON credentials from private pipe instead of browser",
    )
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=90)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--source",
        help="Absolute src directory for current pyXcom; omit for installed baseline",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.source:
        sys.path.insert(0, str(Path(args.source).resolve()))
    if args.cookies_stdin:
        cookies = json.load(sys.stdin)
    else:
        from pyxcom.auth import load_x_cookies

        cookies = load_x_cookies(args.browser, profile=args.profile)
    result = run(args, cookies)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "arm",
                    "stop_reason",
                    "data_attempts",
                    "usable_unique_count",
                    "elapsed_seconds",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
