import unittest

from pyxcom.assets import lazy_operation_assets


MAIN = "https://abs.twimg.com/responsive-web/client-web/main.abcdef12a.js"


def runtime(names, hashes, *, arg="e", suffix="a.js"):
    return (
        f'h.u={arg}=>(({{{names}}})[{arg}]||{arg})+"."+({{{hashes}}})[{arg}]+"{suffix}"'
    )


def html(script):
    return f"<html><script>{script}</script></html>"


class AssetTests(unittest.TestCase):
    def test_dynamic_names_and_hashes_shared_first(self):
        script = runtime(
            '1:"bundle.TweetActivity",2:"shared~bundle.QuoteTweetActivity~bundle.TweetActivity",3:"bundle.HomeTimeline"',
            '1:"abcdef1234567890",2:"1234567890abcdef",3:"1111111111111111"',
        )
        self.assertEqual(
            lazy_operation_assets(html(script), MAIN, "Retweeters"),
            [
                "https://abs.twimg.com/responsive-web/client-web/shared~bundle.QuoteTweetActivity~bundle.TweetActivity.1234567890abcdefa.js",
                "https://abs.twimg.com/responsive-web/client-web/bundle.TweetActivity.abcdef1234567890a.js",
            ],
        )

    def test_numeric_shorthand_variable_name_and_observed_suffix(self):
        script = runtime(
            '9e3:"bundle.TweetActivity"',
            '9000:"abcdef1234567890"',
            arg="chunk",
            suffix="b.js",
        )
        self.assertEqual(
            lazy_operation_assets(html(script), MAIN, "Favoriters"),
            [
                "https://abs.twimg.com/responsive-web/client-web/bundle.TweetActivity.abcdef1234567890b.js"
            ],
        )

    def test_whitespace_and_single_quoted_literals(self):
        script = "h.u = e => (({ 1: 'bundle.TweetActivity' })[ e ] || e) + '.' + ({ 1: 'abcdef12' })[ e ] + 'a.js'"
        self.assertEqual(
            len(lazy_operation_assets(html(script), MAIN, "Retweeters")), 1
        )

    def test_unsupported_shapes_are_empty_without_evaluating_expressions(self):
        for script in (
            "h.u=e=>'guessed.js'",
            runtime('1:"bundle.TweetActivity"', '1:fetch("https://example.com")'),
            runtime('1:"bundle.TweetActivity"', '2:"abcdef12"'),
            runtime(
                '1:"bundle.TweetActivity",1:"bundle.TweetActivity"', '1:"abcdef12"'
            ),
        ):
            with self.subTest(script=script):
                self.assertEqual(
                    lazy_operation_assets(html(script), MAIN, "Retweeters"), []
                )

    def test_only_inline_script_runtime_is_read(self):
        script = runtime('1:"bundle.TweetActivity"', '1:"abcdef12"')
        for page in (
            f"<p>{script}</p>",
            f'<script src="https://example.com/asset.js">{script}</script>',
        ):
            self.assertEqual(lazy_operation_assets(page, MAIN, "Retweeters"), [])

    def test_external_urls_and_traversal_are_rejected(self):
        script = runtime('1:"bundle.TweetActivity"', '1:"abcdef12"')
        for main in (
            "http://abs.twimg.com/responsive-web/client-web/main.abcdef.js",
            "https://abs.twimg.com.evil.test/responsive-web/client-web/main.abcdef.js",
            "https://name@abs.twimg.com/responsive-web/client-web/main.abcdef.js",
            "https://abs.twimg.com:443/responsive-web/client-web/main.abcdef.js",
            MAIN + "?external=1",
            MAIN + "#fragment",
            "https://abs.twimg.com/another/main.abcdef.js",
        ):
            self.assertEqual(
                lazy_operation_assets(html(script), main, "Retweeters"), []
            )
        for name in (
            "../bundle.TweetActivity",
            "bundle.TweetActivity/other",
            "bundle.TweetActivity..other",
            "bundle.TweetActivity%2Fother",
        ):
            self.assertEqual(
                lazy_operation_assets(
                    html(runtime(f'1:"{name}"', '1:"abcdef12"')), MAIN, "Retweeters"
                ),
                [],
            )

    def test_candidate_budget_deduplication_and_unknown_operations(self):
        script = runtime(
            ",".join(f'{i}:"shared~bundle.TweetActivity~part{i}"' for i in range(8)),
            ",".join(f'{i}:"abcdef1234567890"' for i in range(8)),
        )
        page = html(script) + html(script)
        assets = lazy_operation_assets(page, MAIN, "TweetEditHistory")
        self.assertEqual(len(assets), 4)
        self.assertEqual(len(set(assets)), 4)
        self.assertEqual(lazy_operation_assets(page, MAIN, "UnknownOperation"), [])


if __name__ == "__main__":
    unittest.main()
