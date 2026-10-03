"""Profile aliases stay observations while saved batch handles remain tasks."""

import json
import tempfile
import unittest
from pathlib import Path

from pyxcom.batch import collect_accounts
from pyxcom.layout import child_dir, source_path
from pyxcom.models import Post, Profile
from pyxcom.profiles import profile_views, save_profiles
from pyxcom.storage import PostStore, _atomic_json
from pyxcom.validate import finalize_collection, validate_collection


def profile(handle, captured, *, identifier="1"):
    return Profile(
        id=identifier,
        handle=handle,
        name=handle,
        created_at_utc="2009-12-25T02:30:23+00:00",
        captured_at_utc=captured,
    ).to_dict()


class ProfileBatchTests(unittest.TestCase):
    def test_finalize_uses_saved_task_handle_and_ignores_alias_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            query = {"kind": "account_batch", "handles": ["oldname"]}
            store = PostStore(output, query=query)
            post = Post(
                id="1234567890",
                author_id="1",
                author_handle="oldname",
                created_at_utc="2026-06-01T00:00:00+00:00",
                text="sample",
                url="https://x.com/oldname/status/1234567890",
            )
            store.append_page([post], "saved-cursor")
            save_profiles(
                output,
                {
                    "oldname": profile(
                        "oldname", "2026-10-01T00:00:00+00:00", identifier=1
                    ),
                    "newname": profile("newname", "2026-10-02T00:00:00+00:00"),
                },
            )
            for handle, pages, complete in (
                ("oldname", 2, True),
                ("newname", 99, False),
            ):
                for kind in ("originals", "replies"):
                    child = PostStore(
                        child_dir(child_dir(output, handle), kind), query={"kind": kind}
                    )
                    child.state.update(
                        pages_fetched=pages, complete=complete, reason="source_end"
                    )
                    _atomic_json(
                        source_path(child.output_dir, "state.json"), child.state
                    )
            result = finalize_collection(output)
            self.assertTrue(result.complete)
            self.assertEqual(result.pages_fetched, 4)
            manifest = json.loads(
                source_path(output, "account_manifest.json").read_text()
            )
            self.assertEqual(
                [row["handle"] for row in manifest["accounts"]], ["oldname"]
            )
            self.assertEqual(manifest["accounts"][0]["user_id"], "1")
            state = json.loads(source_path(output, "state.json").read_text())
            self.assertEqual(state["query"], query)
            self.assertEqual(state["cursor"], "saved-cursor")
            validation = validate_collection(output)
            self.assertTrue(validation["valid"], validation["errors"])
            # The latest handle view may omit the original alias; its ledger
            # still resolves identity while status lookup stays in oldname/.
            save_profiles(
                output,
                {"newname": profile("newname", "2026-10-02T00:00:00+00:00")},
            )
            from pyxcom import IntegrityError
            from pyxcom.batch import _scheduled_profiles, _account_status

            with self.assertRaises(IntegrityError):
                finalize_collection(output)
            # A changed source file cannot be silently re-anchored by finalize,
            # but resolving historic task handles still preserves their directories.
            current = json.loads(source_path(output, "profiles.json").read_text())
            scheduled = _scheduled_profiles(output, query, current)
            self.assertEqual([handle for handle, _ in scheduled], ["oldname"])
            status = _account_status(output, *scheduled[0])
            self.assertTrue(status["complete"])
            self.assertEqual(
                status["originals"]["pages"] + status["replies"]["pages"], 4
            )
            manifest["accounts"].append({"handle": "newname"})
            _atomic_json(source_path(output, "account_manifest.json"), manifest)
            self.assertIn(
                "account_manifest_mismatch", validate_collection(output)["errors"]
            )

    def test_collect_accounts_keeps_existing_profile_observations(self):
        class Client:
            def __init__(self):
                self._profile_cache = {}

            def get_user(self, handle):
                return Profile(**profile("newname", "2026-10-02T00:00:00+00:00"))

            def save_user_activity(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            PostStore(output, query={"kind": "placeholder"})
            save_profiles(
                output, {"oldname": profile("oldname", "2026-10-01T00:00:00+00:00")}
            )
            # No state exists until append/finish; the batch chooses its own query.
            collect_accounts(
                Client(), ["newname"], output, since="2026-01-01", until="2027-01-01"
            )
            selected, snapshots, _ = profile_views(output)
            self.assertEqual(selected["1"]["handle"], "newname")
            self.assertEqual(
                {row["username"] for row in snapshots}, {"oldname", "newname"}
            )
            current = json.loads(source_path(output, "profiles.json").read_text())
            self.assertEqual(list(current), ["newname"])


if __name__ == "__main__":
    unittest.main()
