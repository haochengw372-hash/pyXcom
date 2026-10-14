"""Bounded read-ahead for independent comment branches; no collection writes in threads."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from pathlib import Path

from .errors import IntegrityError, RateLimitError
from .transport import now_utc, XTransport


class _Stopped(Exception):
    pass


def validate_comment_workers(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise ValueError("comment_workers must be an integer from 1 to 8")
    return value


def decode_prefetched(entries, queue, logs, read_raw):
    """Validate retained read-ahead against exact archived request evidence."""
    result = {}
    frontier = {tuple(task) for task in queue}
    for entry in entries:
        task = (entry["focal_id"], entry.get("cursor"))
        name = entry["raw_path"]
        path = Path(name)
        if (
            task not in frontier
            or task in result
            or not name.startswith(".pyxcom/raw/")
            or ".." in path.parts
            or path.is_absolute()
        ):
            raise IntegrityError("Invalid prefetched comment frontier")
        raw = read_raw(name)
        digest = hashlib.sha256(raw).hexdigest()
        variables = {
            "focalTweetId": task[0],
            **({"cursor": task[1]} if task[1] is not None else {}),
        }
        if digest != entry["raw_sha256"] or not any(
            row.get("operation") == "TweetDetailPrefetch"
            and row.get("variables") == variables
            and row.get("raw_path") == name
            and row.get("raw_sha256") == digest
            for row in logs
        ):
            raise IntegrityError(
                "Prefetched comment evidence does not match its request"
            )
        result[task] = (json.loads(raw), entry["captured_at_utc"])
    return result


class CommentRequests:
    def __init__(self, client, state, workers, *, retained=None, retain=None):
        self.client, self.state, self.workers = client, state, workers
        self.retained = retained or {}
        self.retain = retain or (
            lambda task, payload, captured: {
                "focal_id": task[0],
                "cursor": task[1],
                "payload": payload,
                "captured_at_utc": captured,
            }
        )
        self.cache = list(state.get("prefetched_pages", []))
        self.futures = {}
        self.executor = None
        self.stopped = Event()
        self.lock = Lock()
        self.error_lock = Lock()
        self.next_start = 0.0
        self.error = None
        self.warmed = False
        self.transport = client._x if isinstance(client._x, XTransport) else None
        self.old_gate = None
        if self.transport is not None:
            if getattr(self.transport, "_comment_requests", None) is not None:
                raise ValueError(
                    "One active comment traversal per XClient is supported"
                )
            self.transport._comment_requests = self
            if workers > 1:
                self.old_gate = self.transport._request_gate
                self.transport._request_gate = self._gate

    def _pace(self):
        with self.lock:
            if self.stopped.is_set():
                raise _Stopped()
            if self.workers > 1 and self.stopped.wait(
                max(0, self.next_start - time.monotonic())
            ):
                raise _Stopped()
            self.next_start = time.monotonic() + self.client.delay

    def _gate(self, operation):
        self._pace()
        if self.old_gate:
            self.old_gate(operation)
        if self.stopped.is_set():
            raise _Stopped()

    def _fetch(self, task):
        try:
            if self.transport is None or self.workers == 1:
                self._pace()
            payload = self.client._x.conversation_page(task[0], cursor=task[1])
            return payload, now_utc()
        except _Stopped:
            raise
        except Exception as exc:
            with self.error_lock:
                if self.error is None or isinstance(exc, RateLimitError):
                    self.error = exc
                self.stopped.set()
            raise

    def get(self, task, queue, remaining):
        key = tuple(task)
        for entry in self.cache:
            if (entry["focal_id"], entry.get("cursor")) == key:
                self.cache.remove(entry)
                self.state["prefetched_pages"] = self.cache
                return (
                    self.retained[key]
                    if "raw_path" in entry
                    else (entry["payload"], entry["captured_at_utc"])
                )
        if self.error:
            raise self.error
        if self.workers == 1 or not self.warmed:
            result = self._fetch(key)
            self.warmed = True
            return result
        if self.executor is None:
            self.executor = ThreadPoolExecutor(
                max_workers=self.workers, thread_name_prefix="pyxcom-comments"
            )
        busy = {candidate[0] for candidate in self.futures}
        cached = {(entry["focal_id"], entry.get("cursor")) for entry in self.cache}
        for candidate in queue[:remaining] if remaining is not None else queue:
            candidate = tuple(candidate)
            if len(self.futures) >= self.workers or self.stopped.is_set():
                break
            if candidate in cached or candidate in self.futures or candidate[0] in busy:
                continue
            self.futures[candidate] = self.executor.submit(self._fetch, candidate)
            busy.add(candidate[0])
        if key not in self.futures:
            if self.error:
                raise self.error
            raise _Stopped()
        future = self.futures[key]
        try:
            result = future.result()
        except _Stopped:
            if self.error:
                raise self.error
            raise
        del self.futures[key]
        return result

    def close(self):
        try:
            self._close()
        finally:
            if self.transport is not None:
                if self.workers > 1:
                    self.transport._request_gate = self.old_gate
                self.transport._comment_requests = None

    def _close(self):
        self.stopped.set()
        for future in self.futures.values():
            future.cancel()
        if self.executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        for task, future in self.futures.items():
            try:
                payload, captured = future.result()
            except Exception:
                continue
            entry = self.retain(task, payload, captured)
            if entry is not None:
                self.cache.append(entry)
        if self.workers > 1 or "prefetched_pages" in self.state:
            self.state["prefetched_pages"] = self.cache
        if isinstance(self.error, RateLimitError):
            self.state["rate_reset_at"] = self.error.reset_at
        if self.error is not None:
            transport = self.client._x
            if hasattr(transport, "last_error"):
                transport.last_error = {
                    "error_type": type(self.error).__name__,
                    **{
                        name: getattr(self.error, name)
                        for name in (
                            "operation",
                            "status_code",
                            "reset_at",
                            "error_codes",
                        )
                        if getattr(self.error, name, None) is not None
                    },
                }
