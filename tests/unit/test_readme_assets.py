"""README.md's images (the demo GIF and the fig PNG pairs) exist and stay light.

The README is the product's first impression on GitHub, and its figures get
re-cut and renamed (fig3-parallelism → fig3-tracker-started). These pin that
every local image reference resolves to a tracked file of the right kind, that
every <picture> carries both a light ``src`` and a dark ``srcset``, and that the
demo GIF stays under a first-paint budget.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_README = (_ROOT / "README.md").read_text(encoding="utf-8")

# Markdown images, plus HTML src=/srcset= (srcset takes the first candidate URL).
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_SRC = re.compile(r"\b(?:src|srcset)=\"([^\"\s]+)")
_PICTURE = re.compile(r"<picture>(.*?)</picture>", re.DOTALL)

_MAGIC = {
    ".png": b"\x89PNG\r\n\x1a\n",
    ".gif": b"GIF8",
}

# docs/demo.gif was 1.67 MB, now 3.83 MB; fail before it doubles first paint again.
_DEMO_GIF_BUDGET = 5 * 1024 * 1024


def _local_refs(text: str) -> list[str]:
    refs = _MD_IMAGE.findall(text) + _HTML_SRC.findall(text)
    return [r for r in refs if not re.match(r"^[a-z]+:", r) and not r.startswith("#")]


def test_readme_has_local_images_to_check():
    refs = set(_local_refs(_README))
    assert "docs/demo.gif" in refs
    assert {
        "docs/img/fig1-what-changed.png",
        "docs/img/fig1-what-changed-dark.png",
        "docs/img/fig3-tracker-started.png",
        "docs/img/fig3-tracker-started-dark.png",
    } <= refs


@pytest.mark.parametrize("ref", sorted(set(_local_refs(_README))))
def test_readme_image_resolves_to_a_real_image(ref):
    path = _ROOT / ref
    assert path.is_file(), f"README.md points at missing {ref}"
    magic = _MAGIC.get(path.suffix.lower())
    if magic is not None:
        assert path.read_bytes()[: len(magic)] == magic, f"{ref} is not a {path.suffix}"


def test_readme_drops_the_retired_parallelism_figure():
    assert "fig3-parallelism" not in _README
    assert not list((_ROOT / "docs" / "img").glob("fig3-parallelism*"))


def test_every_picture_has_a_light_src_and_dark_srcset():
    pictures = _PICTURE.findall(_README)
    assert len(pictures) >= 2
    for body in pictures:
        dark = re.search(
            r"<source[^>]*media=\"\(prefers-color-scheme: dark\)\"[^>]*srcset=\"([^\"]+)\"",
            body,
        )
        light = re.search(r"<img[^>]*\bsrc=\"([^\"]+)\"", body)
        assert dark and light, body
        assert re.search(r"<img[^>]*\balt=\"[^\"]+\"", body), "figure needs alt text"
        # The pair is one figure in two themes: <name>.png and <name>-dark.png.
        assert dark.group(1) == light.group(1).replace(".png", "-dark.png")
        assert (_ROOT / light.group(1)).is_file()
        assert (_ROOT / dark.group(1)).is_file()


def test_demo_gif_stays_under_its_size_budget():
    size = (_ROOT / "docs" / "demo.gif").stat().st_size
    assert size <= _DEMO_GIF_BUDGET, f"docs/demo.gif is {size / 1e6:.2f} MB"
