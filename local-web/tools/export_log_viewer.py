"""Export the local web's log viewer as self-contained UTF-8 HTML."""

import argparse
from html.parser import HTMLParser
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "static" / "logs.html"
EMBED_TAGS = (
    "<link rel=stylesheet href=/static/embed.css>",
    "<script src=/static/embed.js defer></script>",
)


class StandaloneViewer(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href", "srcset"} and value and not value.startswith("#"):
                raise ValueError(f"External resource in standalone viewer: <{tag}> {value}")


def render() -> bytes:
    page = SOURCE.read_text(encoding="utf-8")
    for tag in EMBED_TAGS:
        if page.count(tag) != 1:
            raise ValueError(f"Expected one iframe integration tag: {tag}")
        page = page.replace(tag, "")
    parser = StandaloneViewer()
    parser.feed(page)
    parser.close()
    if 'id="sourceSummary" role="status">로그 미선택</strong>' not in page or (
        '<tbody id="rows"></tbody>' not in page
    ):
        raise ValueError("Expected an unselected log source and an empty port table")
    return page.encode("utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output", type=Path, help="standalone HTML destination")
    args = cli.parse_args()
    if args.output.resolve() == SOURCE.resolve():
        cli.error("The output must be separate from the local web source")
    args.output.write_bytes(render())
