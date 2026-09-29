"""Resumable multi-account collection, audit, and summary reporting."""

import json
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Callable

from .errors import PyXcomError
from .models import CollectionResult, Post, Profile
from .storage import PostStore, _atomic_json
from .transport import now_utc

if TYPE_CHECKING:
    from .client import XClient


def _state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _posts(path: Path) -> list[Post]:
    if not path.exists():
        return []
    return [
        Post(**json.loads(line))
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    ]


def _account_status(output: Path, handle: str, profile: dict) -> dict:
    account_dir = output / handle
    originals = _state(account_dir / "originals" / "state.json")
    replies = _state(account_dir / "replies" / "state.json")
    original_count = len(_posts(account_dir / "originals" / "posts.jsonl"))
    reply_count = len(_posts(account_dir / "replies" / "posts.jsonl"))
    total = len({post.id for post in _posts(account_dir / "posts.jsonl")})
    return {
        "handle": handle,
        "user_id": profile["id"],
        "originals": {
            "count": original_count,
            "pages": originals.get("pages_fetched", 0),
            "complete": originals.get("complete", False),
            "reason": originals.get("reason", "not_started"),
            "rate_reset_at": originals.get("rate_reset_at"),
        },
        "replies": {
            "count": reply_count,
            "pages": replies.get("pages_fetched", 0),
            "complete": replies.get("complete", False),
            "reason": replies.get("reason", "not_started"),
            "rate_reset_at": replies.get("rate_reset_at"),
        },
        "unique_posts": total,
        "complete": originals.get("complete", False) and replies.get("complete", False),
    }


def _write_batch_report(
    output: Path, accounts: list[dict], result: CollectionResult
) -> None:
    posts = _posts(output / "posts.jsonl")
    by_author = Counter(post.author_id for post in posts)
    month_counts = Counter((post.author_id, post.created_at_utc[:7]) for post in posts)
    months = sorted({post.created_at_utc[:7] for post in posts})
    lines = [
        "# X 多账号采集报告",
        "",
        f"生成时间（UTC）：{now_utc()}",
        f"去重后 **{result.post_count:,}** 条；所有账号分页结束：**{'是' if result.complete else '否'}**（{result.reason}）。",
        "",
        "| 账号 | Posts页记录 | Replies页记录 | 去重条数 | 浏览量中位数 | Posts状态 | Replies状态 |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for account in accounts:
        views = [
            post.view_count
            for post in posts
            if post.author_id == account["user_id"] and post.view_count is not None
        ]
        median_views = f"{median(views):,.0f}" if views else "—"
        lines.append(
            f"| [@{account['handle']}](https://x.com/{account['handle']}) "
            f"| {account['originals']['count']:,} | {account['replies']['count']:,} "
            f"| {by_author[account['user_id']]:,} | {median_views} "
            f"| {account['originals']['reason']} | {account['replies']['reason']} |"
        )
    lines += [
        "",
        "## 每月可见帖子数（UTC）",
        "",
        "| 月份 | "
        + " | ".join("@" + account["handle"] for account in accounts)
        + " |",
        "| --- | " + " | ".join("---:" for _ in accounts) + " |",
    ]
    for month in months:
        lines.append(
            "| "
            + month
            + " | "
            + " | ".join(
                str(month_counts[(account["user_id"], month)]) for account in accounts
            )
            + " |"
        )
    lines += [
        "",
        "## 覆盖边界",
        "",
        "`passed_since` 表示时间线已越过指定起始日；`source_end` 表示没有下一页；`empty_timeline_end` 表示连续两个只有游标的空页。最后一种是平台分页耗尽信号，不能单独证明历史全量。`rate_limited` 和 `page_limit` 表示尚未完成，可用同一命令续抓。",
        "",
        "删除、受保护、地区限制或未索引的帖子无法据此恢复。互动数是抓取时快照。原始 JSONL、CSV、账号资料、各账号游标及 SHA-256 哈希与本报告一同保存；登录 cookie 不写入输出。",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def collect_accounts(
    client: "XClient",
    handles: list[str],
    output_dir: str | Path,
    *,
    since: str,
    until: str,
    pages_per_round: int = 5,
    rounds: int = 1,
    wait_on_rate_limit: bool = False,
    progress: Callable[[dict], None] | None = None,
) -> CollectionResult:
    """Round-robin collection; rounds=0 continues until complete or stalled."""
    if pages_per_round < 1 or rounds < 0:
        raise ValueError("pages_per_round must be positive and rounds nonnegative")
    if not handles:
        raise ValueError("At least one account is required")
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    saved_profiles = output / "profiles.json"
    if saved_profiles.exists():
        for handle, record in json.loads(
            saved_profiles.read_text(encoding="utf-8")
        ).items():
            client._profile_cache[handle.lower()] = Profile(**record)
    profiles = {
        client.get_user(handle).handle: client.get_user(handle).to_dict()
        for handle in handles
    }
    canonical = list(dict.fromkeys(profiles))
    _atomic_json(output / "profiles.json", profiles)
    query = {
        "kind": "account_batch",
        "handles": canonical,
        "since": since,
        "until": until,
    }
    store = PostStore(output, query=query)
    if store.complete:
        result = store.finish(complete=True, reason="all_accounts_complete")
        _write_batch_report(
            output, [_account_status(output, h, profiles[h]) for h in canonical], result
        )
        return result
    completed_rounds = 0
    reason = "round_limit"
    while rounds == 0 or completed_rounds < rounds:
        before = sum(
            _state(output / h / kind / "state.json").get("pages_fetched", 0)
            for h in canonical
            for kind in ("originals", "replies")
        )
        for handle in canonical:
            try:
                client.save_user_activity(
                    handle,
                    output / handle,
                    since=since,
                    until=until,
                    max_pages=pages_per_round,
                )
            except PyXcomError as exc:
                if progress:
                    progress(
                        {
                            "account": handle,
                            "error": type(exc).__name__,
                            "detail": str(exc),
                        }
                    )
            if progress:
                progress(
                    {
                        "account": handle,
                        **_account_status(output, handle, profiles[handle]),
                    }
                )
        accounts = [_account_status(output, h, profiles[h]) for h in canonical]
        collected: dict[str, Post] = {}
        for handle in canonical:
            for post in _posts(output / handle / "posts.jsonl"):
                collected[post.id] = post
        store.append_page(list(collected.values()), None)
        store.state["pages_fetched"] = sum(
            account[kind]["pages"]
            for account in accounts
            for kind in ("originals", "replies")
        )
        completed_rounds += 1
        _atomic_json(
            output / "account_manifest.json",
            {"query": query, "accounts": accounts, "updated_at_utc": now_utc()},
        )
        checkpoint = store.finish(complete=False, reason="in_progress")
        _write_batch_report(output, accounts, checkpoint)
        if all(account["complete"] for account in accounts):
            reason = "all_accounts_complete"
            break
        after = store.state["pages_fetched"]
        if after <= before:
            rate_states = [
                account[kind]
                for account in accounts
                for kind in ("originals", "replies")
                if account[kind]["reason"] == "rate_limited"
            ]
            if not rate_states:
                reason = "stalled"
                break
            if not wait_on_rate_limit or rounds != 0:
                reason = "rate_limited"
                break
            resets = [
                state["rate_reset_at"]
                for state in rate_states
                if state.get("rate_reset_at") and state["rate_reset_at"] > time.time()
            ]
            wait = max(5, min(resets) - time.time() + 5) if resets else 900
            if progress:
                progress({"status": "rate_limited", "wait_seconds": round(wait)})
            time.sleep(wait)
    complete = reason == "all_accounts_complete"
    result = store.finish(complete=complete, reason=reason)
    accounts = [_account_status(output, h, profiles[h]) for h in canonical]
    _write_batch_report(output, accounts, result)
    return result
