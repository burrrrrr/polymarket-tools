import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"\s*```")


def pages():
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def prose(page):
    """The lines of a page outside its code blocks."""
    fenced = False
    for line in page.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield line


def anchors(page):
    """The anchor GitHub gives each heading of a page."""
    seen, found = {}, set()
    for line in prose(page):
        heading = re.match(r"#+ +(.*)", line)
        if heading:
            slug = re.sub(r"[^\w\- ]", "", heading[1].lower()).replace(" ", "-")
            seen[slug] = seen.get(slug, -1) + 1
            found.add(f"{slug}-{seen[slug]}" if seen[slug] else slug)
    return found


def broken_links():
    """Each relative link whose file or heading does not exist."""
    broken = []
    for page in pages():
        for line in prose(page):
            for target in LINK.findall(line):
                if re.match(r"[a-z]+:", target):
                    continue
                path, _, anchor = target.partition("#")
                dest = (page.parent / path).resolve() if path else page
                if not dest.is_file() or (anchor and anchor not in anchors(dest)):
                    broken.append(f"{page.relative_to(ROOT).as_posix()}: {target}")
    return broken


class DocsLinks(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        self.assertEqual(broken_links(), [])


if __name__ == "__main__":
    unittest.main()
