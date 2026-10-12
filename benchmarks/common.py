"""Shared evidence and wire-level budgets for explicitly bounded comparisons."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch


class BenchmarkStop(BaseException):
    """Bypass SDK retry handlers when an experiment reaches its stop rule."""


def arm_order(arms: list[str], seed: int, round_index: int) -> list[str]:
    ordered = list(arms)
    random.Random(seed).shuffle(ordered)
    return ordered if round_index % 2 == 0 else ordered[::-1]


def sanitize(value, secrets=()):
    sensitive = {
        "authorization",
        "cookie",
        "cookies",
        "auth_token",
        "ct0",
        "password",
        "access_token",
        "refresh_token",
        "set-cookie",
    }
    if isinstance(value, dict):
        return {
            k: "[redacted]" if k.lower() in sensitive else sanitize(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def timestamp(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None


def in_window(value, since, until):
    parsed = timestamp(value)
    return parsed is not None and timestamp(since) <= parsed < timestamp(until)


class WireAudit:
    """Counts every HTTPX data attempt, including SDK retries, before transmission.

    Install only inside one experiment process. Setup traffic has a separate cap.
    Auth and rate-limit responses stop the arm before SDK account rotation/retry.
    Request headers, request URLs/queries, cookies and DBs are never exported.
    """

    def __init__(
        self,
        output,
        *,
        max_attempts,
        seconds,
        secrets=(),
        setup_cap=30,
        data_operations=None,
        per_observer_limit=None,
    ):
        if max_attempts < 1 or seconds <= 0:
            raise ValueError("Positive request and time budgets are required")
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.limit = max_attempts
        self.deadline = time.monotonic() + seconds
        self.setup_cap = setup_cap
        self.data_operations = data_operations
        self.observer_by_token = {}
        self.observer_attempts = {}
        self.per_observer_limit = per_observer_limit
        self.secrets = tuple(secrets)
        self.events = []
        self.data_attempts = 0
        self.setup_attempts = 0
        self._lock = threading.Lock()

    def before(self, request):
        parts = urlsplit(str(request.url))
        data = "/graphql/" in parts.path or "/1.1/" in parts.path or "/2/" in parts.path
        if self.data_operations is not None:
            data = parts.path.rsplit("/", 1)[-1] in self.data_operations
        from http.cookies import SimpleCookie

        cookie = SimpleCookie()
        cookie.load(request.headers.get("cookie", ""))
        token = cookie.get("auth_token")
        observer = self.observer_by_token.get(token.value) if token else None
        with self._lock:
            if data and self.per_observer_limit is not None:
                if observer is None:
                    raise BenchmarkStop("unbound_observer")
                if self.observer_attempts.get(observer, 0) >= self.per_observer_limit:
                    raise BenchmarkStop("observer_request_budget")
            if time.monotonic() >= self.deadline:
                raise BenchmarkStop("time_budget")
            if data and self.data_attempts >= self.limit:
                raise BenchmarkStop("request_budget")
            if not data and self.setup_attempts >= self.setup_cap:
                raise BenchmarkStop("setup_budget")
            if data:
                self.data_attempts += 1
                if observer is not None:
                    self.observer_attempts[observer] = (
                        self.observer_attempts.get(observer, 0) + 1
                    )
            else:
                self.setup_attempts += 1
            event = {
                "index": len(self.events),
                "observer_id": observer,
                "kind": "data" if data else "setup",
                "endpoint": parts.path.rsplit("/", 1)[-1],
                "started": time.monotonic(),
            }
            self.events.append(event)
        return event

    def after(self, event, response=None, error=None):
        event["elapsed_seconds"] = time.monotonic() - event.pop("started")
        if error is not None:
            event["error_type"] = type(error).__name__
            return
        event["status"] = response.status_code
        if event["kind"] == "data":
            try:
                payload = sanitize(response.json(), self.secrets)
            except (ValueError, UnicodeDecodeError):
                payload = {"non_json_response": True}
            path = self.output / f"response_{event['index']:04d}.json"
            write_json(path, payload)
            event["artifact"] = path.name
            event["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if response.status_code in {401, 403, 429}:
            raise BenchmarkStop(f"http_{response.status_code}")

    @contextmanager
    def intercept(self):
        import httpx

        sync_send = httpx.Client._send_single_request
        async_send = httpx.AsyncClient._send_single_request
        audit = self

        def send(client, request):
            event = audit.before(request)
            try:
                response = sync_send(client, request)
                response.read()
            except BaseException as exc:
                audit.after(event, error=exc)
                raise
            audit.after(event, response)
            return response

        async def asend(client, request):
            event = audit.before(request)
            try:
                response = await async_send(client, request)
                await response.aread()
            except BaseException as exc:
                audit.after(event, error=exc)
                raise
            audit.after(event, response)
            return response

        with (
            patch.object(httpx.Client, "_send_single_request", send),
            patch.object(httpx.AsyncClient, "_send_single_request", asend),
        ):
            yield self


def write_results(directory, config, rows):
    """Write a credential-free configuration, CSV, JSON and artifact inventory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "config.json", sanitize(config))
    write_json(directory / "results.json", rows)
    if rows:
        keys = sorted(set().union(*(row.keys() for row in rows)))
        with (directory / "results.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(
                {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
                for row in rows
            )
    hashes = {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.name != "sha256.json"
    }
    write_json(directory / "sha256.json", hashes)
