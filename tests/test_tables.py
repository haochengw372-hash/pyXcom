import csv
import json

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import contextmanager


from pyxcom.models import Post
from pyxcom.tables import export_tables


@contextmanager
def raises(error, match):
    with unittest.TestCase().assertRaisesRegex(error, match):
        yield


def record(post_id, parent=None, root=None, quote=None, repost=None):
    return Post(
        id=post_id,
        author_id="1234567890123456789",
        author_handle="alice",
        created_at_utc="2026-01-01T00:00:00+00:00",
        text="你好\u2028world",
        url=f"https://x.com/alice/status/{post_id}",
        in_reply_to_id=parent,
        conversation_id=root,
        quoted_post_id=quote,
        reposted_post_id=repost,
        media_urls=["https://example.org/image"],
    )


def save(tmp_path, records):
    source = tmp_path / ".pyxcom" / "posts.jsonl"
    source.parent.mkdir(exist_ok=True)
    source.write_text(
        "\n".join(json.dumps(p.to_dict(), ensure_ascii=False) for p in records),
        encoding="utf-8",
    )
    return source


def rows(tmp_path, table):
    with (tmp_path / f"{table}.csv").open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def case_relations_and_missing_ancestry(tmp_path):
    source = save(
        tmp_path,
        [
            record("1"),
            record("2", "1", "1"),
            record("3", "2", "1", quote="99"),
            record("4", "99", "98"),
            record("5", "97", "97"),
            record("6", quote="100"),
            record("7", repost="1"),
        ],
    )
    original = source.read_bytes()
    result = export_tables(tmp_path)
    assert source.read_bytes() == original
    assert result["counts"] == {
        "users": 1,
        "posts": 2,
        "comments": 4,
        "interactions": 1,
        "source_records": 7,
    }
    comments = {r["comment_id"]: r for r in rows(tmp_path, "comments")}
    assert comments["2"]["depth"] == "1"
    assert comments["3"]["depth"] == "2"
    assert comments["3"]["quoted_post_id"] == "99"
    assert comments["4"]["depth"] == ""
    assert comments["4"]["depth_status"] == "missing_parent"
    assert comments["5"]["depth"] == "1"
    assert comments["5"]["root_in_dataset"] == "False"
    assert result["missing_root_count"] == 2
    assert result["missing_parent_count"] == 2
    assert result["unknown_depth_count"] == 1
    assert rows(tmp_path, "users")[0]["profile_available"] == "False"
    assert rows(tmp_path, "users")[0]["user_id"] == "1234567890123456789"
    assert json.loads(rows(tmp_path, "posts")[0]["media_urls"]) == [
        "https://example.org/image"
    ]
    assert rows(tmp_path, "posts")[0]["text"] == "你好\u2028world"


def case_profiles(tmp_path, shape):
    save(tmp_path, [record("1")])
    profile = {
        "id": "1234567890123456789",
        "handle": "alice",
        "name": "Alice",
        "followers_count": 12,
    }
    payload = (
        {"alice": profile}
        if shape == "map"
        else [profile]
        if shape == "list"
        else profile
    )
    (tmp_path / "profiles.json").write_text(json.dumps(payload))
    export_tables(tmp_path)
    user = rows(tmp_path, "users")[0]
    assert user["profile_available"] == "True"
    assert user["display_name"] == "Alice"
    assert user["followers_count"] == "12"


def case_cycle_fails_before_writing(tmp_path):
    save(tmp_path, [record("2", "3", "1"), record("3", "2", "1")])
    with raises(ValueError, match="cycle"):
        export_tables(tmp_path)
    assert not (tmp_path / "tables").exists()


def case_conflicting_duplicate_ids(tmp_path):
    save(tmp_path, [record("1"), record("1", quote="2")])
    with raises(ValueError, match="Conflicting duplicate"):
        export_tables(tmp_path)


def case_empty_and_stale_interactions(tmp_path):
    save(tmp_path, [record("1", repost="2")])
    export_tables(tmp_path)
    old_export = (tmp_path / "interactions.csv").read_bytes()
    save(tmp_path, [])
    from pyxcom import IntegrityError

    with raises(IntegrityError, match="integrity"):
        export_tables(tmp_path)
    assert (tmp_path / "interactions.csv").read_bytes() == old_export
    # A fresh, explicit dataset can legitimately be empty.
    empty = tmp_path / "empty"
    empty.mkdir()
    save(empty, [])
    result = export_tables(empty)
    assert result["counts"]["source_records"] == 0
    assert rows(empty, "posts") == []
    assert rows(empty, "comments") == []
    assert rows(empty, "users") == []
    assert not (empty / "interactions.csv").exists()


def case_deep_thread_is_iterative(tmp_path):
    save(
        tmp_path,
        [record("0")] + [record(str(i), str(i - 1), "0") for i in range(1, 1200)],
    )
    export_tables(tmp_path)
    by_id = {r["comment_id"]: r for r in rows(tmp_path, "comments")}
    assert by_id["1199"]["depth"] == "1199"


def case_profile_without_posts_retained(tmp_path):
    save(tmp_path, [])
    (tmp_path / "profiles.json").write_text(
        json.dumps({"alice": {"id": "9", "handle": "alice", "name": "Alice"}})
    )
    manifest = export_tables(tmp_path)
    assert manifest["counts"]["users"] == 1
    assert rows(tmp_path, "users")[0]["user_id"] == "9"


def case_inconsistent_roots_do_not_infer_depth(tmp_path):
    save(tmp_path, [record("1"), record("2", "1", "1"), record("3", "2", "99")])
    export_tables(tmp_path)
    by_id = {r["comment_id"]: r for r in rows(tmp_path, "comments")}
    assert by_id["3"]["depth"] == ""
    assert by_id["3"]["depth_status"] == "inconsistent_root"


class TablesTests(unittest.TestCase):
    def test_export_cases(self):
        cases = [
            case_relations_and_missing_ancestry,
            case_cycle_fails_before_writing,
            case_conflicting_duplicate_ids,
            case_empty_and_stale_interactions,
            case_deep_thread_is_iterative,
            case_profile_without_posts_retained,
            case_inconsistent_roots_do_not_infer_depth,
        ]
        for case in cases:
            with self.subTest(case=case.__name__), TemporaryDirectory() as directory:
                case(Path(directory))

    def test_profile_shapes(self):
        for shape in ("map", "list", "single"):
            with self.subTest(shape=shape), TemporaryDirectory() as directory:
                case_profiles(Path(directory), shape)
