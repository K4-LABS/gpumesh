"""One version number, written down in six places, must be the same number.

The failure this prevents is not cosmetic. ``gpumesh --version`` is the field
the bug-report form asks a reporter for, and it is what a maintainer reads to
decide whether that reporter is on the release containing the fix — so a stale
value sends a real investigation down the wrong path. A stale ``CITATION.cff``
is wrong in somebody else's published bibliography. A stale compose tag pulls
an image that is not the one the surrounding docs describe.

It has already happened: ``examples/docker-2node/docker-compose.yaml`` was
missing from ``scripts/bump_version.py``'s file list and sat on 3.0.0 while the
rest of the tree moved to 3.2.0. Nothing failed, because nothing was looking.
This is what looks.

Asked for by ``docs/good-first-issues.md`` ("A small test —
tests/test_version_consistency.py — that reads all four and asserts they
match"), and extended to the two compose files for the reason above.

Deliberately regex rather than ``tomllib``/PyYAML: gpumesh supports Python
3.9, where ``tomllib`` does not exist, and PyYAML is not a dependency. Reading
one line out of each file needs neither.
"""

import re
from pathlib import Path

import pytest

import gpumesh

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _search(pattern: str, relative: str) -> str:
    """The first capture of *pattern* in a file, or a failure naming both."""
    match = re.search(pattern, _read(relative), flags=re.MULTILINE)
    assert match, f"{relative} no longer contains a version matching {pattern!r}"
    return match.group(1)


# (file, pattern, what a stale value costs). The third field is the point of
# the test and belongs where the case is defined, so a failure says why it
# matters rather than only that two strings differ.
VERSION_SITES = [
    (
        "pyproject.toml",
        r'^version\s*=\s*"([^"]+)"',
        "what PyPI and pip see",
    ),
    (
        "CITATION.cff",
        r"^version:\s*(\S+)",
        "what GitHub's 'Cite this repository' panel renders",
    ),
    (
        "Dockerfile",
        r"^ARG VERSION=([0-9][0-9.]*)",
        "the image's version and org.opencontainers.image.version labels",
    ),
    (
        "docker-compose.yaml",
        r"image:\s*[\w.-]+/gpumesh:([0-9][0-9.]*)",
        "the image `docker compose up` pulls",
    ),
    (
        "examples/docker-2node/docker-compose.yaml",
        r"image:\s*[\w.-]+/gpumesh:([0-9][0-9.]*)",
        "the image the two-node example pulls",
    ),
]


@pytest.mark.parametrize("relative,pattern,cost",
                         VERSION_SITES,
                         ids=[site[0] for site in VERSION_SITES])
def test_version_matches_the_package(relative, pattern, cost):
    found = _search(pattern, relative)
    assert found == gpumesh.__version__, (
        f"{relative} says {found}, gpumesh.__version__ says "
        f"{gpumesh.__version__}. This value is {cost}. Run "
        f"`python scripts/bump_version.py {gpumesh.__version__}` to bring the "
        f"tree back into step."
    )


def test_every_compose_image_tag_is_current():
    """Both compose files pin the image twice; one line drifting is enough."""
    for relative in ("docker-compose.yaml",
                     "examples/docker-2node/docker-compose.yaml"):
        tags = re.findall(r"image:\s*[\w.-]+/gpumesh:([0-9][0-9.]*)",
                          _read(relative))
        assert tags, f"{relative} pins no gpumesh image any more"
        assert all(tag == gpumesh.__version__ for tag in tags), (
            f"{relative} pins {sorted(set(tags))}, expected "
            f"{gpumesh.__version__} on every line"
        )


def test_bump_script_covers_every_file_this_test_checks():
    """The test and the writer have to agree on the list of files.

    Without this, adding a site here and forgetting to teach
    ``bump_version.py`` about it produces a test that fails on every release
    and can only be satisfied by hand — which is how a file falls off the list
    in the first place.
    """
    script = _read("scripts/bump_version.py")
    for relative, _pattern, _cost in VERSION_SITES:
        name = Path(relative).name
        assert name in script, (
            f"{relative} is checked here but {name} never appears in "
            f"scripts/bump_version.py, so a release will not update it"
        )


def test_citation_release_date_is_iso():
    """CFF requires YYYY-MM-DD, and consumers parse it rather than display it."""
    date = _search(r'^date-released:\s*"([^"]*)"', "CITATION.cff")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), (
        f"CITATION.cff date-released is {date!r}, not YYYY-MM-DD"
    )
