"""Resumable, single-writer scheduling across manually authenticated sessions."""

import inspect
import json
import re
import math
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
from threading import Lock, Event
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import XClient
from ._pool_locks import FileLock, ScopeLock
from .errors import AuthenticationError
from .layout import source_path
from .provenance import collection_observer
from .storage import _atomic_json
from .transport import now_utc

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_KINDS = {
    "search_query": ("save_search_query", "SearchTimeline"),
    "post_quotes": ("save_post_quotes", "SearchTimeline"),
    "user_posts": ("save_user_posts", "UserOriginalsTimeline"),
    "user_replies": ("save_user_replies", "UserRepliesTimeline"),
    "user_reposts": ("save_user_reposts", "UserTweets"),
    "post_comments": ("save_post_comments", "TweetDetail"),
    "followers": ("save_followers", "Followers"),
    "following": ("save_following", "Following"),
    "post_reposters": ("save_post_reposters", "Retweeters"),
}
_OPTIONS = {
    "search_query": {"since", "until", "limit"},
    "post_quotes": {"since", "until", "limit"},
    "user_posts": {"since", "until", "limit"},
    "user_replies": {"since", "until", "limit"},
    "user_reposts": {"since", "until", "limit"},
    "post_comments": {"since", "until", "max_depth", "max_comments"},
    "followers": {"limit", "snapshot_id"},
    "following": {"limit", "snapshot_id"},
    "post_reposters": {"limit", "snapshot_id"},
}


@dataclass(frozen=True)
class LoginConfig:
    """A browser location, never a serialized credential."""

    login_id: str
    browser: str = "chrome"
    profile: str | None = None
    cookie_db: str | Path | None = None
    expected_observer_id: str | None = None


@dataclass(frozen=True)
class CollectionJob:
    """One atomic output scope, with caller-selected record limits."""

    job_id: str
    kind: str
    target: str
    output_dir: str | Path
    options: dict = field(default_factory=dict)
    priority: int = 0
    login_id: str | None = None
    existing_observer_id: str | None = None


@dataclass(frozen=True)
class _Outcome:
    result: tuple | None = None
    error_type: str | None = None
    metadata: tuple = ()
    requests: tuple = ()


class XClientPool:
    """Concurrent bounded dispatch with sticky observers and endpoint cooldowns.

    Browser sessions stay in memory. The queue, public identities and cooldowns
    survive restarts. This scheduler does not promise independent X quotas.
    """

    def __init__(
        self,
        logins: list[LoginConfig],
        *,
        state_dir: str | Path,
        client_options: dict | None = None,
        rate_limit_scope: str = "auto",
    ):
        if rate_limit_scope not in {"auto", "shared", "per_account"}:
            raise ValueError("rate_limit_scope must be auto, shared, or per_account")
        if not logins or len({item.login_id for item in logins}) != len(logins):
            raise ValueError("Provide distinct login IDs")
        for login in logins:
            if not _ID.fullmatch(login.login_id):
                raise ValueError("Invalid login_id")
        self.directory = Path(state_dir).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.directory / "writer.lock")
        self._output_locks: dict[str, ScopeLock] = {}
        self._clients: dict[str, Any] = {}
        self._run_lock = Lock()
        self._stop = Event()
        self._next_request: dict[str, float] = {}
        self._closed = False
        self._logins = {login.login_id: login for login in logins}
        self._client_options = dict(client_options or {})
        if set(self._client_options) - {"proxy", "delay", "timeout"}:
            self.close()
            raise ValueError("Unsupported client_options")
        for key in ("delay", "timeout"):
            value = self._client_options.get(key, 0 if key == "delay" else 30)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or value < (0 if key == "delay" else 0.001)
            ):
                self.close()
                raise ValueError(
                    f"{key} must be a finite nonnegative delay or positive timeout"
                )
        self.rate_limit_scope = rate_limit_scope
        self.path = self.directory / "state.json"
        try:
            self.state: dict[str, Any] = (
                json.loads(self.path.read_text(encoding="utf-8"))
                if self.path.exists()
                else {"version": 1, "jobs": {}, "logins": {}, "cooldowns": {}}
            )
            for job in self.state["jobs"].values():
                if job["status"] == "running":
                    job["status"] = "pending"
                self._reserve_output(job["spec"]["output_dir"])
                marker_path = (
                    Path(job["spec"]["output_dir"]) / ".pyxcom/pool-owner.json"
                )
                if not marker_path.exists():
                    raise ValueError(
                        "Existing pool job is missing its ownership marker"
                    )
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                expected = {
                    "pool_state_dir": str(self.directory),
                    "job_id": job["spec"]["job_id"],
                    "login_id": job["owner"],
                    "observer_id": job["observer_id"],
                }
                transition = job.get("dispatch_preparing")
                if transition is not None:
                    if marker not in (transition, expected):
                        raise ValueError(
                            "Existing pool job ownership marker has changed"
                        )
                    _atomic_json(marker_path, expected)
                    job.pop("dispatch_preparing")
                elif marker != expected:
                    raise ValueError("Existing pool job ownership marker has changed")
            for login in logins:
                self._open_login(login)
            self._persist()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stop.set()
        # A running coordinator joins all workers before releasing this lock.
        with self._run_lock:
            if self._closed:
                return
            self._closed = True
            try:
                for client in self._clients.values():
                    try:
                        client.close()
                    except Exception:
                        pass
            finally:
                for lock in self._output_locks.values():
                    lock.close()
                self._lock.close()

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Pool is closed")

    def _persist(self):
        _atomic_json(self.path, self.state)

    def _event(self, job_id, event, **metadata):
        job = self.state["jobs"].get(job_id, {})
        entry = {
            "at_utc": now_utc(),
            "job_id": job_id,
            "login_id": job.get("owner") or job.get("spec", {}).get("login_id"),
            "observer_id": job.get("observer_id"),
            "output_dir": job.get("spec", {}).get("output_dir"),
            "event": event,
            **metadata,
        }
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def _open_login(self, login):
        meta = self.state["logins"].setdefault(login.login_id, {})
        previous = meta.get("observer_id")
        try:
            client = XClient.from_login(
                browser=login.browser,
                profile=login.profile,
                cookie_db=login.cookie_db,
                **self._client_options,
            )
            self._clients[login.login_id] = client
            observer = str(client.observer_id)
            if not observer.isdigit():
                raise AuthenticationError("Observer identity unavailable")
            if (previous and previous != observer) or (
                login.expected_observer_id and login.expected_observer_id != observer
            ):
                meta.update(status="identity_changed", reason="identity_changed")
                return
            meta.update(observer_id=observer, browser=login.browser, status="active")
            for other_id, other in self.state["logins"].items():
                if (
                    other_id != login.login_id
                    and other_id in self._clients
                    and other.get("status") == "active"
                    and other.get("observer_id") == observer
                ):
                    meta.update(
                        status="duplicate_observer", reason="duplicate_observer"
                    )
                    return
        except Exception:
            meta.update(status="login_unavailable", reason="login_unavailable")

    def _reserve_output(self, output):
        path = str(Path(output).expanduser().resolve())
        if path not in self._output_locks:
            private = Path(path) / ".pyxcom"
            if not private.resolve().is_relative_to(Path(path)):
                raise ValueError("Private output storage must remain inside its scope")
            self._output_locks[path] = ScopeLock(path)

    def add_job(self, job: CollectionJob):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Cannot change the queue while run is active")
        try:
            self._add_job(job)
        finally:
            self._run_lock.release()

    def _add_job(self, job: CollectionJob):
        self._require_open()
        if not _ID.fullmatch(job.job_id) or job.kind not in _KINDS:
            raise ValueError("Invalid job_id or kind")
        if not isinstance(job.target, str) or not job.target.strip():
            raise ValueError("target must be a nonempty string")
        if set(job.options) - _OPTIONS[job.kind]:
            raise ValueError(
                "Unsupported options; credentials and page caps are forbidden"
            )
        if isinstance(job.priority, bool) or not isinstance(job.priority, int):
            raise ValueError("priority must be an integer")
        if job.login_id and job.login_id not in self._logins:
            raise ValueError("Unknown login_id")
        if job.existing_observer_id is not None and (
            not isinstance(job.existing_observer_id, str)
            or not job.existing_observer_id.isdigit()
        ):
            raise ValueError("existing_observer_id must be a public numeric user ID")
        options = dict(job.options)
        for key, value in options.items():
            if key in {"limit", "max_comments", "max_depth"}:
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 1
                ):
                    raise ValueError(f"{key} must be a positive integer or None")
            elif value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a string or None")
        if options.get("max_depth", 1) is None:
            raise ValueError("max_depth must be a positive integer")
        if options.get("snapshot_id") is not None and not _ID.fullmatch(
            options["snapshot_id"]
        ):
            raise ValueError("Invalid snapshot_id")
        from .client import _dates, _handle, _post_id

        _dates(options.get("since"), options.get("until"))
        if job.kind.startswith("user_"):
            _handle(job.target)
        elif job.kind.startswith("post_"):
            _post_id(job.target)
        elif job.kind in {"followers", "following"} and not job.target.isdigit():
            raise ValueError("Network target must be a numeric user ID")
        output = str(Path(job.output_dir).expanduser().resolve())
        output_path = Path(output)
        if (
            output_path == self.directory
            or output_path.is_relative_to(self.directory)
            or self.directory.is_relative_to(output_path)
        ):
            raise ValueError("Pool state and job output scopes must be separate")
        spec = {
            "job_id": job.job_id,
            "kind": job.kind,
            "target": job.target,
            "output_dir": output,
            "options": options,
            "priority": job.priority,
            "login_id": job.login_id,
            "existing_observer_id": job.existing_observer_id,
        }
        existing = self.state["jobs"].get(job.job_id)
        if existing:
            if existing["spec"] != spec:
                raise ValueError("Cannot change an existing job specification")
            return
        path = Path(output)
        for queued in self.state["jobs"].values():
            other = Path(queued["spec"]["output_dir"])
            if (
                path == other
                or path.is_relative_to(other)
                or other.is_relative_to(path)
            ):
                raise ValueError("Job output scopes must not overlap")
        has_existing = (
            any(
                (path / name).exists()
                for name in (
                    "posts.csv",
                    "users.csv",
                    "manifest.json",
                    "network_manifest.json",
                )
            )
            or source_path(path, "state.json").exists()
            or (path / ".pyxcom/networks").exists()
        )
        if has_existing and not (job.login_id and job.existing_observer_id):
            raise ValueError(
                "Adopting existing output requires named login and existing_observer_id"
            )
        if job.existing_observer_id:
            if not job.login_id:
                raise ValueError("existing_observer_id requires login_id")
            meta = self.state["logins"].get(job.login_id, {})
            if (
                meta.get("status") != "active"
                or meta.get("observer_id") != job.existing_observer_id
            ):
                raise ValueError("Existing output observer does not match the login")
        self._reserve_output(output)
        marker_path = path / ".pyxcom/pool-owner.json"
        if marker_path.exists():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if (
                marker.get("pool_state_dir") != str(self.directory)
                or marker.get("job_id") != job.job_id
            ):
                raise ValueError("Output is owned by another pool job")
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.state["jobs"][job.job_id] = {
            "spec": spec,
            "status": "pending",
            "owner": None,
            "observer_id": None,
            "attempts": 0,
            "reason": "not_started",
        }
        _atomic_json(
            marker_path,
            {
                "pool_state_dir": str(self.directory),
                "job_id": job.job_id,
                "login_id": None,
                "observer_id": None,
            },
        )
        self._persist()

    def add_jobs(self, jobs):
        for job in jobs:
            self.add_job(job)

    def resume_job(self, job_id: str, *, retry_stalled: bool = False):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Cannot change the queue while run is active")
        try:
            self._resume_job(job_id, retry_stalled=retry_stalled)
        finally:
            self._run_lock.release()

    def _resume_job(self, job_id: str, *, retry_stalled: bool = False):
        self._require_open()
        if job_id not in self.state["jobs"]:
            raise ValueError("Unknown job_id")
        job = self.state["jobs"][job_id]
        if job["status"] == "complete":
            return
        if (
            job["reason"] in {"empty_page_limit", "no_progress_limit"}
            and not retry_stalled
        ):
            raise ValueError("Stalled discovery requires retry_stalled=True")
        if not isinstance(retry_stalled, bool):
            raise ValueError("retry_stalled must be boolean")
        if retry_stalled and job["spec"]["kind"] not in {
            "search_query",
            "post_quotes",
            "user_reposts",
        }:
            raise ValueError("This collector does not support retry_stalled")
        job.update(status="pending", retry_stalled=retry_stalled)
        self._persist()

    def _operations(self, job):
        kind = job["spec"]["kind"]
        operations = {
            _KINDS[kind][1],
            "Viewer",
            "AccountSettings",
            "Homepage",
            "WebAsset",
        }
        if job.get("rate_limit_operation"):
            operations.add(job["rate_limit_operation"])
        if kind.startswith("user_"):
            operations.add("UserByScreenName")
        if kind in {"post_quotes", "post_comments", "post_reposters"}:
            operations.update(
                {"TweetResultByRestId", "TweetResultsByRestIds", "TweetDetail"}
            )
        return operations

    def _owners(self, job):
        bound = job["owner"] or job["spec"]["login_id"]
        if bound:
            return [bound]
        return sorted(
            self._logins,
            key=lambda key: (self.state["logins"][key].get("last_turn", 0), key),
        )

    def _ready_at(self, owner, job):
        cooldowns = self.state["cooldowns"]
        return max(
            [0.0]
            + [
                float(cooldowns.get(f"{account}:{operation}", 0))
                for operation in self._operations(job)
                for account in (owner, "*")
            ]
        )

    def _limit(self, owner, job, operation, reset_at):
        endpoint = operation or _KINDS[job["spec"]["kind"]][1]
        now = time.time()
        until = max(now + 2, float(reset_at) + 2) if reset_at else now + 62
        self.state["cooldowns"][f"{owner}:{endpoint}"] = until
        observers = {
            self.state["logins"].get(key.split(":", 1)[0], {}).get("observer_id")
            for key, value in self.state["cooldowns"].items()
            if key.endswith(f":{endpoint}") and not key.startswith("*:") and value > now
        }
        if self.rate_limit_scope == "shared" or (
            self.rate_limit_scope == "auto" and len(observers - {None}) >= 2
        ):
            self.state["cooldowns"][f"*:{endpoint}"] = max(
                [until, self.state["cooldowns"].get(f"*:{endpoint}", 0)]
                + [
                    value
                    for key, value in self.state["cooldowns"].items()
                    if key.endswith(f":{endpoint}")
                ]
            )
        job.update(
            status="pending", reason="rate_limited", rate_limit_operation=endpoint
        )
        self._event(
            job["spec"]["job_id"], "rate_limited", operation=endpoint, retry_at=until
        )

    def _source_metadata(self, output):
        result = {}
        paths = [source_path(output, "state.json")]
        paths.extend(Path(output).glob(".pyxcom/networks/*/*/state.json"))
        for path in paths:
            if not path.exists():
                continue
            state = json.loads(path.read_text(encoding="utf-8"))
            stack = [state]
            while stack:
                node = stack.pop()
                for key in (
                    "rate_reset_at",
                    "error_operation",
                    "error_status_code",
                    "error_codes",
                ):
                    if key in node:
                        result[key] = node[key]
                stack.extend(
                    value for value in node.values() if isinstance(value, dict)
                )
        return result

    def _start(self, job, owner, page_budget):
        """Coordinator commits ownership before a worker can touch the output."""
        marker_path = Path(job["spec"]["output_dir"]) / ".pyxcom/pool-owner.json"
        previous_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        expected_previous = {
            "pool_state_dir": str(self.directory),
            "job_id": job["spec"]["job_id"],
            "login_id": job["owner"],
            "observer_id": job["observer_id"],
        }
        if previous_marker != expected_previous:
            raise ValueError("Existing pool job ownership marker has changed")
        self.state["turn"] = self.state.get("turn", 0) + 1
        job.update(
            owner=owner,
            observer_id=self.state["logins"][owner]["observer_id"],
            status="running",
            last_turn=self.state["turn"],
        )
        self.state["logins"][owner]["last_turn"] = self.state["turn"]
        job["attempts"] += 1
        job["dispatch_preparing"] = previous_marker
        self._persist()
        _atomic_json(
            Path(job["spec"]["output_dir"]) / ".pyxcom/pool-owner.json",
            {
                "pool_state_dir": str(self.directory),
                "job_id": job["spec"]["job_id"],
                "login_id": owner,
                "observer_id": job["observer_id"],
            },
        )
        job.pop("dispatch_preparing")
        self._persist()
        self._event(job["spec"]["job_id"], "dispatch", page_budget=page_budget)
        snapshot = json.loads(json.dumps(job))
        job.pop("retry_stalled", None)
        return snapshot

    def _worker(self, job, owner, page_budget):
        """Only this observer's client and output are touched by this worker."""
        client = self._clients[owner]
        transport = getattr(client, "_x", None)
        requests = []
        old_gate = getattr(transport, "_request_gate", None)
        old_observer = getattr(transport, "_request_observer", None)

        def gate(operation):
            if old_gate:
                old_gate(operation)
            delay = float(
                self._client_options.get("delay", getattr(client, "delay", 1.0))
            )
            remaining = self._next_request.get(owner, 0) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._next_request[owner] = time.monotonic() + delay

        def observe(metadata):
            # Do not store response text, request variables, URLs or headers.
            safe = {}
            for key in (
                "operation",
                "status_code",
                "attempt",
                "elapsed_seconds",
                "response_bytes",
                "reset_at",
                "remaining",
                "error_type",
            ):
                value = metadata.get(key)
                if value is None or isinstance(value, (str, int, float, bool)):
                    safe[key] = value
            requests.append(tuple(safe.items()))
            if old_observer:
                old_observer(metadata)

        if transport is not None:
            transport.last_error = None
            transport._request_gate = gate
            transport._request_observer = observe
        result_tuple = None
        error_type = None
        metadata = {}
        try:
            if str(client.check_login_identity()) != job["observer_id"]:
                return _Outcome(
                    error_type="identity_unavailable", requests=tuple(requests)
                )
            method = getattr(client, _KINDS[job["spec"]["kind"]][0])
            options = dict(job["spec"]["options"])
            options.setdefault(
                "max_comments" if job["spec"]["kind"] == "post_comments" else "limit",
                None,
            )
            options["max_pages"] = page_budget
            if (
                job.get("retry_stalled")
                and "retry_stalled" in inspect.signature(method).parameters
            ):
                options["retry_stalled"] = True
            with collection_observer(
                job["observer_id"],
                owner,
                pool_state_dir=self.directory,
                job_id=job["spec"]["job_id"],
            ):
                result = method(
                    job["spec"]["target"], job["spec"]["output_dir"], **options
                )
            result_tuple = (
                result.post_count,
                result.pages_fetched,
                result.complete,
                result.reason,
            )
            if not result.complete:
                source = (
                    self._source_metadata(job["spec"]["output_dir"])
                    if result.reason
                    in {"rate_limited", "source_error", "api_error", "request_error"}
                    else {}
                )
                for key, value in source.items():
                    metadata[
                        {
                            "rate_reset_at": "reset_at",
                            "error_operation": "operation",
                            "error_status_code": "status_code",
                            "error_codes": "error_codes",
                        }[key]
                    ] = value
                metadata.update(getattr(transport, "last_error", None) or {})
        except Exception as exc:
            error_type = type(exc).__name__
            for key in ("operation", "status_code", "reset_at", "error_codes"):
                value = getattr(exc, key, None)
                if value is not None:
                    metadata[key] = value
        finally:
            if transport is not None:
                transport._request_gate = old_gate
                transport._request_observer = old_observer
        safe_metadata = tuple(
            (key, tuple(value) if key == "error_codes" else value)
            for key, value in metadata.items()
            if key in {"operation", "status_code", "reset_at", "error_codes"}
        )
        return _Outcome(result_tuple, error_type, safe_metadata, tuple(requests))

    def _accept(self, job, owner, outcome):
        """The coordinator alone changes durable queue state and event logs."""
        job_id = job["spec"]["job_id"]
        for request in outcome.requests:
            self._event(job_id, "request", **dict(request))
        job["requests"] = job.get("requests", 0) + len(outcome.requests)
        metadata = dict(outcome.metadata)
        previous = job.get("result", {})
        if outcome.result is not None:
            job["result"] = dict(
                zip(
                    ("post_count", "pages_fetched", "complete", "reason"),
                    outcome.result,
                )
            )
        result = job.get("result", {}) if outcome.result is not None else {}
        reason = result.get("reason")
        error = outcome.error_type
        auth = (
            error == "AuthenticationError"
            or metadata.get("status_code") == 401
            or bool(set(metadata.get("error_codes", ())) & {32, 89, 135, 215})
        )
        if (
            error == "RateLimitError"
            or reason == "rate_limited"
            or metadata.get("status_code") == 429
        ):
            self._limit(owner, job, metadata.get("operation"), metadata.get("reset_at"))
        elif auth or error == "identity_unavailable":
            reason = (
                "identity_unavailable"
                if error == "identity_unavailable"
                else "authentication_failed"
            )
            self.state["logins"][owner].update(status=reason, reason=reason)
            job.update(status="paused", reason=reason)
        elif error:
            reason = (
                "forbidden"
                if metadata.get("status_code") == 403
                else (
                    "source_error"
                    if error in {"APIError", "ParseError"}
                    else "collection_error"
                )
            )
            job.update(status="paused", reason=reason)
        elif result.get("complete"):
            job.update(status="complete", reason=reason)
        elif metadata.get("status_code") == 403:
            job.update(status="paused", reason="forbidden")
        elif reason in {"page_limit", "partial_timelines"}:
            progressed = result.get("pages_fetched", 0) > previous.get(
                "pages_fetched", 0
            ) or result.get("post_count", 0) > previous.get("post_count", 0)
            job.update(
                status="pending" if progressed else "paused",
                reason=reason if progressed else "no_dispatch_progress",
            )
        else:
            job.update(status="paused", reason=reason or "collection_error")
        self._event(
            job_id,
            "result",
            status=job["status"],
            reason=job["reason"],
            result=job.get("result"),
            error_type=error,
        )
        self._persist()

    def run(
        self,
        *,
        page_budget=1,
        max_steps=None,
        max_workers=4,
        wait_on_rate_limit=False,
        max_seconds=None,
    ):
        """Run bounded slices; deadlines stop dispatch and join in-flight workers.

        The time budget is not a hard request cancellation deadline. A running
        slice may finish after it expires; its result and checkpoint are retained.
        """
        self._require_open()
        for name, value in (
            ("page_budget", page_budget),
            ("max_workers", max_workers),
            ("max_steps", max_steps),
        ):
            if value is None and name == "max_steps":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_seconds is not None and (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, (float, int))
            or not math.isfinite(max_seconds)
            or max_seconds <= 0
        ):
            raise ValueError("max_seconds must be positive and finite")
        if not isinstance(wait_on_rate_limit, bool):
            raise ValueError("wait_on_rate_limit must be boolean")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Pool already has an active run")
        try:
            self._require_open()
            return self._run(
                page_budget, max_steps, max_workers, wait_on_rate_limit, max_seconds
            )
        finally:
            self._run_lock.release()

    def _run(
        self, page_budget, max_steps, max_workers, wait_on_rate_limit, max_seconds
    ):
        started = time.monotonic()
        deadline = started + max_seconds if max_seconds is not None else math.inf
        steps = 0
        peak_workers = 0
        futures = {}
        busy = set()
        # shutdown(wait=True) runs even on interruption; completed results are
        # accepted in finally so no finished worker is abandoned as 'running'.
        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pyxcom"
        ) as executor:
            try:
                while True:
                    dispatch = (
                        not self._stop.is_set()
                        and time.monotonic() < deadline
                        and (max_steps is None or steps < max_steps)
                    )
                    waiting = []
                    if dispatch:
                        ordered = sorted(
                            (
                                job
                                for job in self.state["jobs"].values()
                                if job["status"] == "pending"
                            ),
                            key=lambda j: (
                                -j["spec"]["priority"],
                                j.get("last_turn", 0),
                                j["spec"]["job_id"],
                            ),
                        )
                        for job in ordered:
                            if (
                                len(futures) >= max_workers
                                or (max_steps is not None and steps >= max_steps)
                                or time.monotonic() >= deadline
                                or self._stop.is_set()
                            ):
                                break
                            owners = self._owners(job)
                            usable = [
                                key
                                for key in owners
                                if key in self._clients
                                and self.state["logins"][key].get("status") == "active"
                            ]
                            if not usable:
                                reason = (
                                    self.state["logins"]
                                    .get(owners[0], {})
                                    .get("reason", "login_unavailable")
                                    if owners
                                    else "login_unavailable"
                                )
                                job.update(status="paused", reason=reason)
                                self._event(
                                    job["spec"]["job_id"], "paused", reason=reason
                                )
                                continue
                            ready = []
                            for owner in usable:
                                at = self._ready_at(owner, job)
                                if at > time.time():
                                    waiting.append(at)
                                elif owner not in busy:
                                    ready.append(owner)
                            if not ready:
                                continue
                            owner = ready[0]
                            snapshot = self._start(job, owner, page_budget)
                            future = executor.submit(
                                self._worker, snapshot, owner, page_budget
                            )
                            futures[future] = (job, owner)
                            busy.add(owner)
                            steps += 1
                            peak_workers = max(peak_workers, len(futures))
                    if futures:
                        done, _ = wait(futures, return_when=FIRST_COMPLETED)
                        for future in done:
                            job, owner = futures.pop(future)
                            busy.remove(owner)
                            self._accept(job, owner, future.result())
                        continue
                    self._persist()
                    if dispatch and waiting and wait_on_rate_limit:
                        delay = min(
                            30,
                            max(0.01, min(waiting) - time.time()),
                            max(0, deadline - time.monotonic()),
                        )
                        if delay:
                            time.sleep(delay)
                        continue
                    break
            finally:
                for future, (job, owner) in list(futures.items()):
                    try:
                        self._accept(job, owner, future.result())
                    except BaseException:
                        # Persisted running jobs are requeued on the next open.
                        self._persist()
                        raise
        return {
            "steps": steps,
            "max_workers": max_workers,
            "peak_workers": peak_workers,
            "elapsed_seconds": time.monotonic() - started,
            "rate_limit_scope": self.rate_limit_scope,
            "jobs": {
                job_id: {
                    key: job.get(key)
                    for key in (
                        "status",
                        "reason",
                        "owner",
                        "observer_id",
                        "attempts",
                        "result",
                        "requests",
                    )
                }
                for job_id, job in self.state["jobs"].items()
            },
            "logins": {key: dict(value) for key, value in self.state["logins"].items()},
            "cooldowns": dict(self.state["cooldowns"]),
        }
