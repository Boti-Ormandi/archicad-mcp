"""Deterministic checks for public Markdown routes and package rendering."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


def _public_markdown_files() -> list[Path]:
    return [
        _PROJECT_ROOT / "README.md",
        _PROJECT_ROOT / "CONTRIBUTING.md",
        *sorted((_PROJECT_ROOT / "docs").rglob("*.md")),
    ]


def _inline_markdown_destinations(text: str) -> Iterator[str]:
    start = 0
    while (marker := text.find("](", start)) != -1:
        cursor = marker + 2
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1

        if cursor < len(text) and text[cursor] == "<":
            end = text.find(">", cursor + 1)
            if end != -1:
                yield text[cursor + 1 : end]
        else:
            destination_start = cursor
            depth = 0
            while cursor < len(text):
                character = text[cursor]
                if character == "\\":
                    cursor += 2
                    continue
                if character == "(":
                    depth += 1
                elif character == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif character.isspace() and depth == 0:
                    break
                cursor += 1
            if cursor > destination_start:
                yield text[destination_start:cursor]

        start = marker + 2


def _markdown_destinations(text: str) -> Iterator[str]:
    yield from _inline_markdown_destinations(text)
    for match in _REFERENCE_LINK.finditer(text):
        yield match.group(1).strip().strip("<>")


def test_markdown_destination_parser_captures_nested_image_links_and_references() -> None:
    markdown = "[![status](https://img.example/badge.svg)](https://example.test/build)\n[guide][docs]\n[docs]: /guide"

    assert list(_markdown_destinations(markdown)) == [
        "https://img.example/badge.svg",
        "https://example.test/build",
        "/guide",
    ]


def _is_repository_relative(destination: str) -> bool:
    parsed = urlsplit(destination)
    return (
        not parsed.scheme
        and not parsed.netloc
        and bool(parsed.path)
        and not parsed.path.startswith("/")
    )


def test_repository_relative_links_in_public_markdown_resolve() -> None:
    files = _public_markdown_files()
    assert files
    assert all(path.is_file() for path in files)

    for document in files:
        text = document.read_text(encoding="utf-8")
        for destination in _markdown_destinations(text):
            if not _is_repository_relative(destination):
                continue
            relative_path = unquote(urlsplit(destination).path)
            target = (document.parent / relative_path).resolve()
            assert target.is_relative_to(_PROJECT_ROOT), (
                f"{document.relative_to(_PROJECT_ROOT)} links outside the repository: {destination}"
            )
            assert target.exists(), (
                f"{document.relative_to(_PROJECT_ROOT)} has an unresolved link: {destination}"
            )


def test_readme_uses_only_fragment_or_absolute_link_destinations() -> None:
    readme = (_PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    destinations = list(_markdown_destinations(readme))
    assert destinations

    for destination in destinations:
        parsed = urlsplit(destination)
        assert not parsed.path or parsed.scheme in {"http", "https"}, (
            f"README destination must be a fragment or absolute HTTP(S) URL: {destination}"
        )
        if parsed.scheme:
            assert parsed.netloc, f"README absolute URL has no host: {destination}"
