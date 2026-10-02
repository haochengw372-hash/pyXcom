"""Post-role classification and field coverage reports for saved datasets."""

import gzip
import json
import os
from collections import Counter
from dataclasses import fields
from pathlib import Path

from ._persistence import read_jsonl

from .layout import internal_dir, migrate_collection, source_path
from .models import Post, Profile
from .profiles import profile_views
from .storage import PostStore, _atomic_json
from .validate import finalize_collection, validate_collection

FIELD_GROUPS = {
    "in_reply_to_user_id": ("关系", "直接回复对象的用户 ID；缺失时保持未知"),
    "quoted_author_id": ("关系", "被引用帖作者 ID"),
    "reposted_author_id": ("关系", "被原生转发帖作者 ID"),
    "reposted_created_at_utc": ("时间与正文", "原帖发布时间；不是转发动作时间"),
    "raw_json": ("溯源", "完整返回帖子对象；旧记录可能缺失"),
    "text_source": ("时间与正文", "正文来自 note_tweet 或 legacy_full_text"),
    "text_complete": ("时间与正文", "正文完整性，无法核验时保持未知"),
    "observation_role": ("分类", "analysis / seed / context，窗口外祖先不作为分析样本"),
    "id": ("身份", "X 帖子 ID；以字符串保存"),
    "author_id": ("身份", "作者 X ID"),
    "author_handle": ("身份", "作者账号名"),
    "url": ("身份", "帖子永久链接"),
    "created_at_utc": ("时间与正文", "发布时间，UTC ISO 8601"),
    "text": ("时间与正文", "完整可取得的正文"),
    "language": ("时间与正文", "X 返回的语言代码"),
    "post_role": ("分类", "main 主帖 / comment 评论回复 / repost 转发"),
    "post_type": ("分类", "original / quote / reply / repost"),
    "conversation_id": ("关系", "会话根帖 ID"),
    "in_reply_to_id": ("关系", "直接回复的父帖 ID"),
    "quoted_post_id": ("关系", "被引用帖子 ID"),
    "reposted_post_id": ("关系", "被转发帖子 ID"),
    "reply_count": ("互动", "收到的回复数"),
    "repost_count": ("互动", "转发数"),
    "like_count": ("互动", "点赞数"),
    "view_count": ("互动", "公开浏览量"),
    "quote_count": ("互动", "被引用次数"),
    "bookmark_count": ("互动", "书签数"),
    "media_urls": ("内容元素", "图片、视频预览或视频链接数组"),
    "outbound_urls": ("内容元素", "正文外链数组"),
    "hashtags": ("内容元素", "话题标签数组"),
    "mentions": ("内容元素", "提及账号数组"),
    "discovery_source": ("来源", "发现帖子 ID 的方式"),
    "discovery_url": ("来源", "发现页面链接"),
    "captured_at_utc": ("来源", "该条响应的抓取时间；旧数据可能为空"),
}

RAW_OPTIONAL = [
    (
        "in_reply_to_user_id_str / in_reply_to_screen_name",
        "回复目标账号；当前总表尚未标准化",
    ),
    (
        "extended_entities.media[].type / sizes / video_info",
        "媒体类型、尺寸、视频信息；当前只导出链接",
    ),
    ("possibly_sensitive", "X 的敏感内容标记；并非研究者自定义编码"),
    ("source / display_text_range", "客户端来源与显示文本范围"),
    (
        "quoted_status_result / note_tweet",
        "引用帖嵌套对象及长文结构；正文已优先取长文文本",
    ),
]


def _read_posts(path: Path) -> list[Post]:
    return [Post(**record) for record in read_jsonl(path)]


def schema_summary(output_dir: str | Path) -> dict:
    output = Path(output_dir).expanduser()
    if (output / "network_manifest.json").exists() or (
        output / ".pyxcom" / "networks"
    ).exists():
        raise ValueError(
            "Post schema reports do not apply to network snapshot datasets"
        )
    posts = _read_posts(source_path(output, "posts.jsonl"))
    post_fields = []
    for item in fields(Post):
        values = [getattr(post, item.name) for post in posts]
        available = sum(value is not None and value != "" for value in values)
        nonempty = sum(bool(value) for value in values if isinstance(value, list))
        group, description = FIELD_GROUPS[item.name]
        post_fields.append(
            {
                "name": item.name,
                "group": group,
                "description": description,
                "available": available,
                "missing": len(posts) - available,
                "nonempty_array": nonempty
                if any(isinstance(v, list) for v in values)
                else None,
                "example_type": next(
                    (type(v).__name__ for v in values if v is not None), "None"
                ),
            }
        )
    selected_profiles, _, _ = profile_views(output)
    profiles = [Profile(**value) for value in selected_profiles.values()]
    profile_fields = [
        {
            "name": item.name,
            "available": sum(
                getattr(profile, item.name) is not None for profile in profiles
            ),
            "missing": len(profiles)
            - sum(getattr(profile, item.name) is not None for profile in profiles),
        }
        for item in fields(Profile)
    ]
    roles = Counter(post.post_role for post in posts)
    kinds = Counter(post.post_type for post in posts)
    by_account = {}
    for handle in sorted({post.author_handle for post in posts}):
        own = [post for post in posts if post.author_handle == handle]
        by_account[handle] = {
            "total": len(own),
            "roles": dict(Counter(post.post_role for post in own)),
            "types": dict(Counter(post.post_type for post in own)),
        }
    return {
        "output_dir": str(output),
        "post_count": len(posts),
        "post_field_count": len(post_fields),
        "role_counts": dict(roles),
        "type_counts": dict(kinds),
        "by_account": by_account,
        "post_fields": post_fields,
        "profile_count": len(profiles),
        "profile_fields": profile_fields,
        "raw_optional_fields": RAW_OPTIONAL,
    }


def write_schema_report(output_dir: str | Path) -> dict:
    output = Path(output_dir).expanduser()
    summary = schema_summary(output)
    internal_dir(output).mkdir(parents=True, exist_ok=True)
    _atomic_json(internal_dir(output) / "schema.json", summary)
    lines = [
        "# pyXcom 数据字段与主帖/评论分类",
        "",
        f"当前去重帖子：**{summary['post_count']:,}** 条；标准化帖子字段：**{summary['post_field_count']}** 个。",
        "",
        "## 分类规则",
        "",
        "- `reposted_post_id` 有值 → `post_role=repost`、`post_type=repost`。",
        "- 否则 `in_reply_to_id` 有值 → `post_role=comment`、`post_type=reply`。回复自己的帖也按评论/回复记录。",
        "- 否则 `quoted_post_id` 有值 → `post_role=main`、`post_type=quote`。",
        "- 其余 → `post_role=main`、`post_type=original`。若多种关系同时存在，分类优先级为转发、回复、引用。原始关系 ID 保留在各列。",
        "- 本次六账号数据只收作者本人生成的帖子；纯转发通常显示原作者 ID，未被纳入。因此此处 `repost=0` 是采集口径，不代表这些账号从未转发。",
        "",
        "| 主帖/评论角色 | 条数 |",
        "| --- | ---: |",
    ]
    lines += [
        f"| {key} | {value:,} |"
        for key, value in sorted(summary["role_counts"].items())
    ]
    lines += ["", "| 帖子类型 | 条数 |", "| --- | ---: |"]
    lines += [
        f"| {key} | {value:,} |"
        for key, value in sorted(summary["type_counts"].items())
    ]
    lines += [
        "",
        "## 各账号分类",
        "",
        "| 账号 | 主帖 | 评论/回复 | 转发 | 合计 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for handle, value in summary["by_account"].items():
        roles = value["roles"]
        lines.append(
            f"| @{handle} | {roles.get('main', 0):,} | {roles.get('comment', 0):,} "
            f"| {roles.get('repost', 0):,} | {value['total']:,} |"
        )
    lines += [
        "",
        "## 当前帖子表的字段覆盖",
        "",
        "| 分组 | 字段 | 含义 | 有值条数 | 空值条数 | 非空数组条数 |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for item in summary["post_fields"]:
        lines.append(
            f"| {item['group']} | `{item['name']}` | {item['description']} "
            f"| {item['available']:,} | {item['missing']:,} "
            f"| {item['nonempty_array'] if item['nonempty_array'] is not None else '—'} |"
        )
    lines += ["", "## 账号资料字段", ""]
    lines.append(", ".join(f"`{item['name']}`" for item in summary["profile_fields"]))
    lines += [
        "",
        "## X 原始返回中还可能取得、目前尚未标准化的字段",
        "",
        "| 原始字段 | 说明 |",
        "| --- | --- |",
    ]
    lines += [f"| `{name}` | {description} |" for name, description in RAW_OPTIONAL]
    lines += [
        "",
        "这些可选字段依帖子类型和 X 当前返回格式而异；不能把不存在的字段当作零。登录账号自己的 `favorited`、`bookmarked` 等状态不是公开互动总数，不建议混入公共帖子表。",
        "",
        "CSV 中数组字段是 JSON 字符串；JSONL 中保留数组、整数及 null。`captured_at_utc` 的旧记录为空时，不能用批次导出时间冒充每条帖子的抓取时间。",
        "",
    ]
    (internal_dir(output) / "schema_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    return summary


def apply_role_schema(output_dir: str | Path) -> dict:
    """Backfill role/type in saved JSONL; preserve gzipped originals once."""
    output = Path(output_dir).expanduser()
    migrate_collection(output)
    changed: list[str] = []
    paths = sorted(
        (
            p
            for p in output.rglob("posts.jsonl")
            if "legacy" not in p.relative_to(output).parts
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in paths:
        folder = path.parent.parent if path.parent.name == ".pyxcom" else path.parent
        state_path = source_path(folder, "state.json")
        if not state_path.exists():
            continue
        raw = list(read_jsonl(path))
        posts = [Post(**record) for record in raw]
        if all(
            record.get("post_role") == post.post_role
            and record.get("post_type") == post.post_type
            for record, post in zip(raw, posts)
        ):
            continue
        backup = path.parent / "posts.jsonl.before-role-schema.gz"
        if not backup.exists():
            with gzip.open(backup, "wb") as file:
                file.write(path.read_bytes())
        temporary = path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            for post in posts:
                file.write(json.dumps(post.to_dict(), ensure_ascii=True) + "\n")
        os.replace(temporary, path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        store = PostStore(folder, query=state["query"])
        store.finish(
            complete=state.get("complete", False),
            reason=state.get("reason", "migrated"),
        )
        changed.append(str(folder))
    if source_path(output, "profiles.json").exists():
        finalize_collection(output)
    summary = write_schema_report(output)
    validations = [
        validate_collection(
            path.parent.parent if path.parent.name == ".pyxcom" else path.parent
        )
        for path in paths
    ]
    if not all(result["valid"] for result in validations):
        raise ValueError("One or more migrated collections failed validation")
    return {
        "changed_directories": changed,
        "validated_directories": len(validations),
        **summary,
    }
