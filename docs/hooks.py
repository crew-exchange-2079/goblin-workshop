"""MkDocs hook: build one page per tool from that tool's README.md.

The READMEs stay the single source of truth; nothing is copied into docs/.
Relative links in a README point at files next to it in the repository, which
are not part of the site, so they are rewritten to GitHub URLs (images to raw
URLs so they still render). Links to another tool's README go to its page.

TOOLS is the one registry: it drives the tool pages, the "Tools" part of the
nav and the tables on the landing page. A listed tool whose README is not on the
branch is left out of all three, so a tool can be registered before the PR that
adds it lands. The build fails if a top-level directory has a README.md that is
not in TOOLS, so a new tool cannot silently miss the site.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from mkdocs.exceptions import PluginError
from mkdocs.structure.files import File, Files

REPO = "crew-exchange-2079/goblin-workshop"
BRANCH = "main"

TAPE, OTHER, EXAMPLES = "Tape tools", "Other tools", "Examples"
SECTIONS = (TAPE, OTHER, EXAMPLES)

# README directory (relative to the repo root) -> (nav section, landing-page blurb).
# The page is tools/<last path component>.md. Nav and table order follow this dict.
TOOLS = {
    "tape-exporter": (TAPE, "Read-only Prometheus exporter for Linux LTO/SCSI tape drives."),
    "tapefec": (TAPE, "Streaming Reed–Solomon forward-error-correction framer for tar-to-tape pipelines; pure-Python educational prototype."),
    "tape-library-playground": (TAPE, "Browser-based teaching simulator of a robotic tape library and its scheduling policies; not an operational controller."),
    "ltfs-index-validator": (TAPE, "Offline schema-only checker for LTFS 2.4.0 full index XML; not compliance or certification."),
    "tape-regression-lab/mhvtl-status": (TAPE, "Regression kit for mhvtl's optional log pages, run against a fresh upstream checkout."),
    "lto-capacity-planner": (TAPE, "Static calculator for LTO cartridges, library slots and media cost per generation; arithmetic, not a vendor sizing tool."),
    "tape-label-gen": (TAPE, "Code 39 LTO cartridge barcode labels as print-ready PDF sheets (CLI) or a static web page."),
    "tape-regression-lab/mhvtl-scenarios": (TAPE, "Scripted mhvtl scenarios that drive an emulated LTO-8 drive through media states and assert on tape-exporter metrics."),
    "hsm-sim": (TAPE, "Deterministic command-line teaching simulator of a disk cache in front of a tape archive, driven by YAML scenarios; models no product."),
    "rack-parts": (OTHER, "Parametric OpenSCAD generator for printable rack parts (brackets, shelves, panels, PDU mounts)."),
    "timer2dagu": (OTHER, "Converts systemd .timer + .service pairs into Dagu DAG YAML and reports what could not be translated."),
    "examples": (EXAMPLES, "End-to-end walkthroughs that chain the workshop's tools on synthetic data; CI runs each one."),
}

# Top-level directories that are not tools and have no page of their own.
NOT_TOOLS = {"docs", "tests", "tape-regression-lab"}

# docs/index.md marks where each section's table goes: <!-- tools: Tape tools -->
TABLE_MARKER = re.compile(r"<!-- tools: (.+?) -->")

FENCE = re.compile(r"^\s*(```|~~~)")
# [text](target) and ![alt](target); target may carry a "title".
MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?((?:\s+\"[^\"]*\")?)\s*\)")
HTML_ATTR = re.compile(r"""\b(src|href)=(["'])([^"']+)\2""")


def _page(readme_dir: str) -> str:
    return f"tools/{posixpath.basename(readme_dir)}.md"


def _present(root: Path) -> list[str]:
    return [d for d in TOOLS if (root / d / "README.md").is_file()]


def _is_external(target: str) -> bool:
    return target.startswith(("#", "/", "mailto:")) or "://" in target


def _rewrite(target: str, readme_dir: str, root: Path, page: str, image: bool) -> str:
    if _is_external(target):
        return target
    path, _, frag = target.partition("#")
    repo_path = posixpath.normpath(posixpath.join(readme_dir, path))
    frag = "#" + frag if frag else ""
    if posixpath.basename(repo_path) == "README.md":
        other = posixpath.dirname(repo_path)
        if other in TOOLS:
            return posixpath.relpath(_page(other), posixpath.dirname(page)) + frag
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
    present = _present(root)
    for readme_dir in present:
        page = _page(readme_dir)
        readme = (root / readme_dir / "README.md").read_text(encoding="utf-8")
        files.append(File.generated(config, page, content=render(readme, readme_dir, root, page)))
    # mkdocs.yml lists only the hand-written pages; the tool pages are appended here,
    # before MkDocs builds the nav from config. Sections added by an earlier build of
    # the same config (mkdocs serve) are dropped first so they are not doubled.
    base = [e for e in config["nav"] or [] if not (isinstance(e, dict) and e.keys() <= set(SECTIONS))]
    config["nav"] = base + [
        {section: [{posixpath.basename(d): _page(d)} for d in present if TOOLS[d][0] == section]}
        for section in SECTIONS
        if any(TOOLS[d][0] == section for d in present)
    ]
    return files


def on_page_markdown(markdown: str, page, config, files) -> str:
    if page.file.src_uri != "index.md":
        return markdown
    present = _present(Path(config.config_file_path).parent)

    def table(m: re.Match) -> str:
        if m[1] not in SECTIONS:
            raise PluginError(f"docs/index.md: unknown tools section {m[1]!r}")
        rows = [f"| [{posixpath.basename(d)}]({_page(d)}) | {TOOLS[d][1]} |" for d in present if TOOLS[d][0] == m[1]]
        return "\n".join(["| Tool | What it is |", "| --- | --- |", *rows])

    return TABLE_MARKER.sub(table, markdown)
