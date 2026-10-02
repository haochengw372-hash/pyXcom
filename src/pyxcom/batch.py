"""Resumable multi-account collection, audit, and summary reporting."""

import json
import time
from collections import Counter
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Callable

from .errors import PyXcomError
from .layout import child_dir, internal_dir, migrate_collection, source_path
from .models import CollectionResult, Post, Profile
from .profiles import profile_views, save_profiles
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
    account_dir = child_dir(output, handle)
    originals = _state(source_path(child_dir(account_dir, "originals"), "state.json"))
    replies = _state(source_path(child_dir(account_dir, "replies"), "state.json"))
    original_count = len(
        _posts(source_path(child_dir(account_dir, "originals"), "posts.jsonl"))
    )
    reply_count = len(
        _posts(source_path(child_dir(account_dir, "replies"), "posts.jsonl"))
    )
    total = len({post.id for post in _posts(source_path(account_dir, "posts.jsonl"))})
    return {
        "handle": handle,
        "user_id": str(profile["id"]),
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


def _scheduled_profiles(
    output: Path, query: dict, profiles: dict
) -> list[tuple[str, dict]]:
    """Resolve saved task handles without turning historical aliases into tasks."""
    selected, snapshots, _ = profile_views(output)
    handles = query.get("handles", list(profiles))
    resolved = []
    for handle in handles:
        profile = profiles.get(handle)
        if profile is None:
            candidates = [
                row["user_id"]
                for row in snapshots
                if row["source_key"] == handle or row["username"] == handle
            ]
            identifiers = set(candidates)
            if len(identifiers) != 1:
                raise ValueError(f"Cannot resolve saved account handle: {handle}")
            profile = selected[identifiers.pop()]
        identifier = str(profile["id"])
        resolved.append((handle, selected[identifier]))
    return resolved


def _write_batch_report(
    output: Path, accounts: list[dict], result: CollectionResult
) -> None:
    posts = _posts(source_path(output, "posts.jsonl"))
    by_author = Counter(post.author_id for post in posts)
    role_counts = Counter((post.author_id, post.post_role) for post in posts)
    month_counts = Counter((post.author_id, post.created_at_utc[:7]) for post in posts)
    months = sorted({post.created_at_utc[:7] for post in posts})
    lines = [
        "# X 多账号采集报告",
        "",
        f"生成时间（UTC）：{now_utc()}",
        f"去重后 **{result.post_count:,}** 条；所有账号分页结束：**{'是' if result.complete else '否'}**（{result.reason}）。",
        "",
        "| 账号 | 主帖 | 评论/回复 | 转发 | 去重条数 | 浏览量中位数 | Posts状态 | Replies状态 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
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
            f"| {role_counts[(account['user_id'], 'main')]:,} "
            f"| {role_counts[(account['user_id'], 'comment')]:,} "
            f"| {role_counts[(account['user_id'], 'repost')]:,} "
            f"| {by_author[account['user_id']]:,} | {median_views} "
            f"| {account['originals']['reason']} | {account['replies']['reason']} |"
        )
    lines += [
        "",
        "分类按每条帖子自身的回复/引用/转发关系，不按 Posts 或 Replies 标签页来源；两种标签页可能显示同一帖。`main` 包含原创和引用帖，`comment` 是有父帖 ID 的回复。",
        "本次采集按目标账号的作者 ID 筛选，纯转发的原作者不是目标账号，因此未纳入表内；`repost=0` 不代表账号从未转发。",
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
    (internal_dir(output) / "report.md").write_text("\n".join(lines), encoding="utf-8")


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
    migrate_collection(output)
    saved_profiles = source_path(output, "profiles.json")
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
    save_profiles(output, profiles)
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
            _state(
                source_path(child_dir(child_dir(output, h), kind), "state.json")
            ).get("pages_fetched", 0)
            for h in canonical
            for kind in ("originals", "replies")
        )
        for handle in canonical:
            try:
                client.save_user_activity(
                    handle,
                    child_dir(output, handle),
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
            for post in _posts(source_path(child_dir(output, handle), "posts.jsonl")):
                collected[post.id] = post
        store.append_page(list(collected.values()), None)
        store.state["pages_fetched"] = sum(
            account[kind]["pages"]
            for account in accounts
            for kind in ("originals", "replies")
        )
        completed_rounds += 1
        _atomic_json(
            internal_dir(output) / "account_manifest.json",
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
