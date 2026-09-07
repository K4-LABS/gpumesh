"""Sphinx configuration for the gpumesh documentation.

The pages under docs/ are read two ways. On github.com they are plain markdown.
On https://gpumesh.readthedocs.io Sphinx renders them. Everything here exists to
add the second way without touching the first. No page was rewritten for Sphinx.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# -- Project metadata -------------------------------------------------------
#
# Read out of gpumesh/__init__.py with a regex rather than by importing the
# package. Importing it would make the docs build depend on every optional
# extra (torch, rich, psutil, ...) being installed on Read the Docs, for the
# sake of one string. autodoc does import the package, but that import runs
# against the mocks listed in autodoc_mock_imports below. This one does not
# have to happen at all, so it does not.
project = "gpumesh"
author = "Samurai007AK"
copyright = "2025, Samurai007AK"  # Sphinx requires this exact name
release = re.search(
    r'^__version__ = "([^"]+)"',
    (_ROOT / "gpumesh" / "__init__.py").read_text(encoding="utf-8"),
    re.M,
).group(1)
version = release

# -- General ----------------------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
]

# good-first-issues.md opens with "This file is a staging area for the
# maintainer, not documentation for users." Taking it at its word.
exclude_patterns = ["_build", "good-first-issues.md"]

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

# -- MyST -------------------------------------------------------------------

myst_enable_extensions = ["colon_fence", "deflist"]

# Anchors for h1-h3. Several pages link at a specific heading
# (../README.md#limitations and friends), and without this MyST generates no
# anchor to aim at.
myst_heading_anchors = 3

# -- HTML -------------------------------------------------------------------

html_theme = "furo"
html_title = f"gpumesh {release}"
html_static_path: list[str] = []

# -- autodoc ----------------------------------------------------------------

# Every optional extra in [project.optional-dependencies]. With these mocked,
# Read the Docs installs `.[docs]` and stops there. Without them it downloads a
# torch wheel to render a docstring.
autodoc_mock_imports = [
    "torch",
    "psutil",
    "rich",
    "questionary",
    "pandas",
    "pyngrok",
    "cryptography",
    "mcp",
]

autodoc_member_order = "bysource"
autodoc_typehints = "description"

# -- Links that leave docs/ -------------------------------------------------

_GITHUB_BLOB = "https://github.com/K4-LABS/gpumesh/blob/master/"

# `](../SECURITY.md)` and the six other targets like it.
_ESCAPING_LINK = re.compile(r"\]\(\.\./([^)\s]+)\)")


def _rewrite_escaping_links(_app, _docname, source):
    """Point links that escape docs/ at the file on github.com.

    docs/*.md links out to the repository root 17 times, at ../SECURITY.md,
    ../THREAT_MODEL.md, ../README.md#limitations, ../CONTRIBUTING.md,
    ../SUPPORT.md, ../CHANGELOG.md and ../examples/README.md. None of those
    files is a Sphinx source, so MyST resolves none of them. Each one becomes a
    warning and renders as dead text, and -W fails the build on all 17 at once.

    Rewriting the source text here, instead of editing the markdown, leaves the
    links relative on github.com. Most people read these pages there, and no
    page should be written for one renderer at the other's expense. Doing it at
    `source-read` also avoids depending on how a given myst-parser version
    represents an unresolved link in the doctree.

    The alternatives were worse. Pulling the root documents in as `{include}`
    stubs costs five more files and the link edits anyway. Moving the source
    directory to the repository root puts a conf.py at the top of a Python
    package and needs an exclude list for .agents/, .continue/, .forge/,
    build/ and dist/.

    This is not fence-aware, on purpose. No code fence in docs/ contains
    `](../` today. If someone adds one, strip fences first, the way the
    docs-links job in .github/workflows/hygiene.yml already does.
    """
    source[0] = _ESCAPING_LINK.sub("](" + _GITHUB_BLOB + r"\1)", source[0])


def setup(app):
    app.connect("source-read", _rewrite_escaping_links)
    return {"parallel_read_safe": True, "parallel_write_safe": True}
