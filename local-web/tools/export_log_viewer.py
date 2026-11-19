"""Export the local web's log viewer or port matrix as self-contained UTF-8 HTML."""

import argparse
from html.parser import HTMLParser
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "static" / "logs.html"
MATRIX_SOURCE = SOURCE.with_name("csv.html")
EMBED_TAGS = (
    "<link rel=stylesheet href=/static/embed.css>",
    "<script src=/static/embed.js defer></script>",
)


class StandaloneViewer(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in {"src", "href", "srcset"} and value and not value.startswith("#"):
                raise ValueError(f"External resource in standalone viewer: <{tag}> {value}")


def render(*, matrix: bool = False) -> bytes:
    source = MATRIX_SOURCE if matrix else SOURCE
    page = source.read_text(encoding="utf-8")
    for tag in EMBED_TAGS:
        if page.count(tag) != 1:
            raise ValueError(f"Expected one iframe integration tag: {tag}")
        page = page.replace(tag, "")
    if matrix:
        tag = '<script src="power-matrix.js"></script>'
        if page.count(tag) != 1:
            raise ValueError("Expected one shared power classifier")
        script = source.with_name("power-matrix.js").read_text(encoding="utf-8")
        if "</script" in script.lower():
            raise ValueError("Classifier cannot be inlined safely")
        page = page.replace(tag, "<script>" + script + "</script>")
    parser = StandaloneViewer()
    parser.feed(page)
    parser.close()
    if not matrix and (
        'id="sourceSummary" role="status">로그 미선택</strong>' not in page
        or ('<tbody id="rows"></tbody>' not in page)
    ):
        raise ValueError("Expected an unselected log source and an empty port table")
    return page.encode("utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--matrix", action="store_true", help="export the CSV port matrix")
    cli.add_argument("output", type=Path, help="standalone HTML destination")
    args = cli.parse_args()
    if args.output.resolve() in (SOURCE.resolve(), MATRIX_SOURCE.resolve()):
        cli.error("The output must be separate from the local web source")
    args.output.write_bytes(render(matrix=args.matrix))
