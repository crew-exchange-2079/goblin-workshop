"""MkDocs hook: build one page per tool from that tool's README.md.

The READMEs stay the single source of truth; nothing is copied into docs/.
Relative links in a README point at files next to it in the repository, which
are not part of the site, so they are rewritten to GitHub URLs (images to raw
URLs so they still render). Links to another tool's README go to its page.

The build fails if a top-level directory has a README.md that is not in TOOLS,
so a new tool cannot silently miss the site.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File, Files

REPO = "crew-exchange-2079/goblin-workshop"
BRANCH = "main"

# README directory (relative to the repo root) -> page path under docs/.
TOOLS = {
    "tape-exporter": "tools/tape-exporter.md",
    "tapefec": "tools/tapefec.md",
    "tape-library-playground": "tools/tape-library-playground.md",
    "ltfs-index-validator": "tools/ltfs-index-validator.md",
    "rack-parts": "tools/rack-parts.md",
    "tape-regression-lab/mhvtl-status": "tools/mhvtl-status.md",
}

# Top-level directories that are not tools and have no page of their own.
NOT_TOOLS = {"docs", "tests", "tape-regression-lab"}

FENCE = re.compile(r"^\s*(```|~~~)")
# [text](target) and ![alt](target); target may carry a "title".
MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?((?:\s+\"[^\"]*\")?)\s*\)")
HTML_ATTR = re.compile(r"""\b(src|href)=(["'])([^"']+)\2""")


def _is_external(target: str) -> bool:
    return target.startswith(("#", "/", "mailto:")) or "://" in target


def _rewrite(target: str, readme_dir: str, root: Path, page: str, image: bool) -> str:
    if _is_external(target):
        return target
    path, _, frag = target.partition("#")
    repo_path = posixpath.normpath(posixpath.join(readme_dir, path))
    frag = "#" + frag if frag else ""
    if posixpath.basename(repo_path) == "README.md":
        other = TOOLS.get(posixpath.dirname(repo_path))
        if other:
            return posixpath.relpath(other, posixpath.dirname(page)) + frag
    if repo_path.startswith(".."):
        raise PluginError(f"{readme_dir}/README.md links outside the repository: {target}")
    if image:
        return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{repo_path}"
    kind = "tree" if (root / repo_path).is_dir() else "blob"
    return f"https://github.com/{REPO}/{kind}/{BRANCH}/{repo_path}{frag}"


def render(readme: str, readme_dir: str, root: Path, page: str) -> str:
    out, in_fence = [], False
    for line in readme.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence:
            line = MD_LINK.sub(
                lambda m: f"{m[1]}[{m[2]}]("
                + _rewrite(m[3], readme_dir, root, page, image=bool(m[1]))
                + f"{m[4]})",
                line,
            )
            line = HTML_ATTR.sub(
                lambda m: f"{m[1]}={m[2]}"
                + _rewrite(m[3], readme_dir, root, page, image=m[1] == "src")
                + m[2],
                line,
            )
        out.append(line)
    source = f"https://github.com/{REPO}/blob/{BRANCH}/{readme_dir}/README.md"
    return "".join(out) + f"\n\n---\n\n*Generated from [`{readme_dir}/README.md`]({source}).*\n"


def on_files(files: Files, config) -> Files:
    root = Path(config.config_file_path).parent
    unlisted = sorted(
        p.parent.name
        for p in root.glob("*/README.md")
        if p.parent.name not in TOOLS and p.parent.name not in NOT_TOOLS
    )
    if unlisted:
        raise PluginError(f"README.md without a docs page; add to TOOLS in docs/hooks.py: {unlisted}")
    for readme_dir, page in TOOLS.items():
        readme = (root / readme_dir / "README.md").read_text(encoding="utf-8")
        files.append(File.generated(config, page, content=render(readme, readme_dir, root, page)))
    return files
