"""Repository rules that are easy to break by accident."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_agent_guides_are_the_same_file() -> None:
    assert (ROOT / "AGENTS.md").read_bytes() == (ROOT / "CLAUDE.md").read_bytes()


def test_every_document_the_readme_links_exists() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for line in readme.splitlines():
        if "](docs/" in line:
            target = line.split("](", 1)[1].split(")", 1)[0]
            assert (ROOT / target).is_file(), target
