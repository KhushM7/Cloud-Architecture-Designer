"""The palette lives in one place, and this is what keeps it that way (A5).

`branding.py` is the only file that writes a colour down. `server.py` serves it
to the browser as `/brand.css`, `static/app.js` reads the custom properties back
off the document for the diagram, and `export.py` imports the same dict for the
PDF. The copies this file used to police are gone.

What can still go wrong is a colour creeping back in as a literal, or a
stylesheet reading a `var(--cs-...)` that nothing defines -- which CSS fails at
silently, painting nothing rather than raising. Those are what these tests are
for.
"""

import re
from pathlib import Path

import pytest

import branding
import export

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# A `--cs-name: #HEX;` declaration, which is what app.css must no longer carry.
TOKEN_PATTERN = re.compile(r"^\s*(--cs-[a-z0-9-]+):\s*(#[0-9A-Fa-f]{3,8})\s*;", re.MULTILINE)

# Every `var(--cs-name)` read anywhere in the stylesheet.
VAR_PATTERN = re.compile(r"var\(\s*(--cs-[a-z0-9-]+)\s*\)")

# What a generated `:root` block defines, by token name.
DEFINITION_PATTERN = re.compile(r"^\s*(--cs-[a-z0-9-]+):", re.MULTILINE)

# A six-digit hex colour. `&#10003;`-style HTML numeric entities are shorter, so
# pinning the length keeps them out rather than filtering them afterwards.
HEX_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\b")


@pytest.fixture(scope="module")
def generated() -> str:
    return branding.css_root()


def test_the_stylesheet_declares_no_colour_of_its_own():
    """A token declared here would shadow the generated one and not follow a
    rebrand. The type, easing and layout tokens app.css still owns are not
    colours, so they do not match."""
    declared = TOKEN_PATTERN.findall(CSS)
    assert not declared, f"app.css is declaring colour tokens again: {declared}"


def test_the_renderer_names_no_colour_of_its_own():
    """app.js asks the document what the tokens resolved to. A hex literal in
    there is a colour a rebrand would miss."""
    literals = sorted(set(HEX_PATTERN.findall(JS)))
    assert not literals, f"hex colours are back in app.js: {literals}"


def test_every_token_the_stylesheet_reads_is_defined(generated):
    """The one that catches a typo. `var(--cs-primry)` is not an error in CSS:
    the declaration is dropped and the element paints with nothing."""
    defined = set(DEFINITION_PATTERN.findall(generated)) | set(DEFINITION_PATTERN.findall(CSS))
    used = set(VAR_PATTERN.findall(CSS))

    missing = sorted(used - defined)
    assert not missing, f"app.css reads tokens nothing defines: {missing}"


def test_the_generated_stylesheet_carries_the_whole_palette(generated):
    for name, value in branding.palette().items():
        assert f"--cs-{name}: {value};" in generated, name


def test_the_export_palette_is_the_same_palette():
    """The PDF opens with no stylesheet, so export.py writes the colours in as
    literals. They have to be these literals."""
    assert branding.palette() == export.INK


def test_the_export_diagram_is_styled_like_the_app_diagram():
    assert branding.node_styles() == export.NODE_STYLES


def test_a_derived_shade_follows_the_colour_it_is_derived_from():
    """The point of the four knobs: change one and its shades move with it."""
    ink = branding.palette()

    assert ink["primary-ink"] == branding.mix(branding.BRAND_PRIMARY, (0, 0, 0), 0.25)
    assert ink["accent-soft"] == branding.mix(branding.BRAND_ACCENT, (255, 255, 255), 0.92)
    assert branding.rgba(branding.BRAND_PRIMARY, 0.35) in branding.css_root()


def test_the_semantic_colours_are_not_derived_from_the_brand():
    """They say what state something is in. A rebrand must not move them."""
    for state in ("success", "warning", "danger"):
        assert branding.palette()[state] == branding.SEMANTIC[state]
