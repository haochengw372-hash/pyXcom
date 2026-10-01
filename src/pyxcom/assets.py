"""Locate public lazy operation assets without evaluating remote JavaScript."""

import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


class _InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self._parts = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._parts = [] if "src" not in dict(attrs) else None

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._parts is not None:
            self.scripts.append("".join(self._parts))
            self._parts = None


_RUNTIME = re.compile(
    r"[\w$]+\.u\s*=\s*(?P<arg>[A-Za-z_$][\w$]*)\s*=>\s*"
    r"\(\s*\(\s*\{(?P<names>[^{}]*)\}\s*\)\s*"
    r"\[\s*(?P=arg)\s*\]\s*\|\|\s*(?P=arg)\s*\)\s*"
    r'\+\s*["\']\.["\']\s*\+\s*\(\s*\{(?P<hashes>[^{}]*)\}\s*\)\s*'
    r"\[\s*(?P=arg)\s*\]\s*\+\s*"
    r'(?P<quote>["\'])(?P<suffix>[A-Za-z0-9_-]*\.js)(?P=quote)'
)
_PAIR = re.compile(
    r"\s*(?P<key>\d+(?:\.\d+)?(?:[eE]\+?\d+)?)\s*:\s*"
    r'(?P<quote>["\'])(?P<value>[^"\'\\]*)(?P=quote)\s*'
)


def _literal_map(source: str) -> dict[str, str]:
    result = {}
    for item in source.split(","):
        match = _PAIR.fullmatch(item)
        if match is None:
            return {}
        try:
            number = Decimal(match["key"])
            if number != number.to_integral_value() or number > 10**12:
                return {}
            key = str(int(number))
        except (InvalidOperation, ValueError, OverflowError):
            return {}
        if key in result:
            return {}
        result[key] = match["value"]
    return result


def lazy_operation_assets(
    homepage_html: str, main_url: str, operation: str
) -> list[str]:
    """Return at most four observed public asset URLs for an activity operation.

    The current X runtime gives a flat chunk-name map and a corresponding hash
    map. Unsupported runtime shapes or operations return no candidates. Only
    literal maps are parsed; no script, expression, or guessed query ID runs.
    """
    if operation not in {"Retweeters", "Favoriters", "TweetEditHistory"}:
        return []
    try:
        main = urlsplit(main_url)
        if (
            main.scheme != "https"
            or main.netloc != "abs.twimg.com"
            or main.query
            or main.fragment
            or not re.fullmatch(
                r"/responsive-web/client-web/main\.[A-Za-z0-9_-]+\.js", main.path
            )
        ):
            return []
    except ValueError:
        return []
    parser = _InlineScripts()
    parser.feed(homepage_html)
    candidates = {}
    base = main.path.rsplit("/", 1)[0] + "/"
    for script in parser.scripts:
        for runtime in _RUNTIME.finditer(script):
            names = _literal_map(runtime["names"])
            hashes = _literal_map(runtime["hashes"])
            for key, name in names.items():
                digest = hashes.get(key, "")
                if (
                    "bundle.TweetActivity" not in name
                    or not re.fullmatch(r"[A-Za-z0-9_.~-]{1,250}", name)
                    or ".." in name
                    or not re.fullmatch(r"[a-fA-F0-9]{8,64}", digest)
                ):
                    continue
                url = urlunsplit(
                    (
                        main.scheme,
                        main.netloc,
                        base + name + "." + digest + runtime["suffix"],
                        "",
                        "",
                    )
                )
                candidates[url] = 0 if name.startswith("shared~") else 1
    return sorted(candidates, key=lambda url: (candidates[url], url))[:4]
