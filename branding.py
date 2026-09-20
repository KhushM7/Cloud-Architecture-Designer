"""Who this advisor belongs to. Edit this file to rebrand the whole application.

Every piece of company identity the app prints -- the sidebar logo, the cover
page of an exported deliverable, the footer of every sheet, the comment at the
top of a generated Terraform module -- reads from here. Nothing else in the
codebase should carry a company name as a literal.

The one deliberate exception is `advisor.SYSTEM_PROMPT`, which names the product
in a fixed string. That prompt is one byte-stable literal because the Anthropic
prompt cache keys on it, so it cannot interpolate from this module; there are
tests that fail if anything is interpolated into it.

The colours are here too, in the palette section at the bottom. Four values set
the whole scheme; everything else is worked out from them. `server.py` serves
them to the browser as `/brand.css`, `static/app.js` reads them back off the
document for the diagram, and `export.py` imports them for the PDF. There is no
second copy to keep in step.
"""

from typing import Any

# The company the deliverables come from. Printed on the cover page, in every
# sheet footer, in the CLI banner and in generated Terraform.
BRAND_NAME = "Insert Company Name"

# The registered address, printed in the footer of anything that leaves the
# building. A single line; it is not parsed.
BRAND_ADDRESS = "Insert Company Name Ltd, Insert Address Line, Insert City, Insert Postcode"

# Printed beside the address on an exported cover page and footer.
BRAND_DOMAIN = "insert-domain.com"

# The product's own name, as it appears in window titles, the CLI banner and the
# header of a generated Terraform module.
PRODUCT_NAME = "Architecture Advisor"

# The prefix on a generated document reference: AR-2026-0819-01. Keep it short
# -- it is printed on the cover and in every footer so a page torn out of a
# bundle can be traced back.
DOC_PREFIX = "AR"

# The logo files, resolved against `static/assets/`. Replace these two files
# with your own and the app, the exported HTML, the PDF and the favicon all
# follow. SVG keeps them in version control as text; a PNG works too, but
# `export.logo_uri()` names the media type, so change it there as well.
LOGO_FULL = "logo-placeholder.svg"
LOGO_MARK = "mark-placeholder.svg"


# =========================================================================== #
# The palette
#
# Set the four colours below and the whole application follows: the buttons,
# links, tabs and focus rings, the sidebar, the compare grid, the architecture
# diagram, and the cover page of every exported PDF. Nothing else needs editing
# and there is no build step -- save the file and refresh the browser.
#
# The shipped scheme is a deep teal over slate. It is two colour families rather
# than four unrelated colours: the secondary and the accent are one hue at two
# depths, so the sidebar and an export cover read as the same surface, and the
# teal is the only saturated thing on screen, which is what makes it read as
# *the* accent. Every pairing clears WCAG AA against the ground it is actually
# drawn on -- worth re-checking if you change them. README.md has the how.
# =========================================================================== #

# The interactive accent, drawn on the light surfaces: buttons, links, the
# active tab, focus rings, and the compute nodes of a diagram. This is the one
# colour a reader will call "the brand colour". White text is printed on it, so
# it has to stay dark enough to carry it -- this teal is 5.5:1.
BRAND_PRIMARY = "#0F766E"

# The quieter tone: the sidebar behind the conversation list, and the entry
# points of a diagram. It sits behind content rather than in front of it.
BRAND_SECONDARY = "#334155"

# The deep tone: the cover page of an exported deliverable, the hero band at the
# top of an exported web page, and the data stores in a diagram. White text is
# printed on it, so keep it dark.
BRAND_ACCENT = "#1E293B"

# The accent again, for use *on* the dark surfaces -- the eyebrow labels in the
# sidebar, the rule on an export cover. This one is set by hand rather than
# worked out, because it has to stay lighter than the sidebar whatever
# BRAND_PRIMARY is, and that is a judgement about contrast rather than a sum.
# Here it is the primary lightened, so the labels echo the buttons.
BRAND_ON_DARK = "#7DD3C0"

# --------------------------------------------------------------------------- #
# Below here is worked out from the four above. Read it to see what a colour
# turns into; there is no need to edit it.
# --------------------------------------------------------------------------- #

# The neutral chrome: body text, rules, panels and the page behind the app.
# These stay neutral under almost any brand, which is why they are not knobs --
# but they are ordinary values and can be changed if a scheme really needs it.
NEUTRALS = {
    "ink": "#1F1F1F",
    "ink-2": "#464646",
    "ink-3": "#7A7A7A",
    "line": "#E6E6E6",
    "line-strong": "#CFCFCF",
    "surface": "#FFFFFF",
    "surface-3": "#FAFAFA",
    "canvas": "#E9EAE6",
}

# Not a customisation point. These three say what state something is in -- a
# pillar the architecture handles, one that needs a look, an action that
# destroys something -- and a reader knows green, amber and red before they know
# the brand. Recolouring them to match a logo would take the meaning away.
SEMANTIC = {
    "success": "#1F9D55",
    "warning": "#D97706",
    "danger": "#C0392B",
}

_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)


def _rgb(colour: str) -> tuple[int, int, int]:
    """`#1A1A1A` -> `(26, 26, 26)`. Three-digit hex is expanded first."""
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(channel * 2 for channel in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    """`(26, 26, 26)` -> `#1A1A1A`. Upper case, because the browser hands the
    value back to `app.js` exactly as it was written and the tests compare it."""
    return "#" + "".join(f"{channel:02X}" for channel in rgb)


def mix(colour: str, into: tuple[int, int, int], amount: float) -> str:
    """`colour` blended `amount` of the way towards `into` (white or black)."""
    return _hex(
        tuple(  # type: ignore[arg-type]
            round(channel + (target - channel) * amount)
            for channel, target in zip(_rgb(colour), into, strict=True)
        )
    )


def rgba(colour: str, alpha: float) -> str:
    """The same colour at a given opacity, for a ring or a shadow. CSS cannot
    build one of these out of a hex custom property without `color-mix()`, so it
    is composed here instead of in the stylesheet."""
    red, green, blue = _rgb(colour)
    return f"rgba({red}, {green}, {blue}, {alpha})"


def palette() -> dict[str, str]:
    """Every colour in the application, as `token name -> hex`.

    The keys are the CSS custom property names with the `--cs-` prefix dropped.
    `export.py` uses this dict directly for the PDF and the exported web page.
    """
    return {
        # The brand, and the shades worked out from it.
        "primary": BRAND_PRIMARY,
        "primary-ink": mix(BRAND_PRIMARY, _BLACK, 0.25),
        "primary-soft": BRAND_ON_DARK,
        "secondary": BRAND_SECONDARY,
        "secondary-soft": mix(BRAND_SECONDARY, _WHITE, 0.10),
        "accent": BRAND_ACCENT,
        "accent-soft": mix(BRAND_ACCENT, _WHITE, 0.92),
        "accent-muted": mix(BRAND_ACCENT, _WHITE, 0.55),
        **NEUTRALS,
        **SEMANTIC,
    }


def css_root() -> str:
    """The `:root` block the browser gets, served by `server.py` as `/brand.css`.

    Everything `static/app.css` reads as `var(--cs-...)` and does not define
    itself is defined here.
    """
    tokens = palette()
    lines = [f"  --cs-{name}: {value};" for name, value in tokens.items()]
    lines += [
        f"  --cs-grad-primary: linear-gradient(135deg, {tokens['primary']} 0%, "
        f"{mix(BRAND_PRIMARY, _WHITE, 0.15)} 100%);",
        f"  --cs-grad-secondary: linear-gradient(135deg, {tokens['secondary']} 0%, "
        f"{tokens['secondary-soft']} 100%);",
        f"  --cs-focus-ring: 0 0 0 3px {rgba(BRAND_PRIMARY, 0.35)};",
        f"  --cs-shadow-lg: 0 18px 40px {rgba(BRAND_ACCENT, 0.14)};",
        f"  --cs-on-dark-line: {rgba(BRAND_ON_DARK, 0.4)};",
    ]
    body = "\n".join(lines)
    return (
        "/* Generated from branding.py. Edit the four colours there, not this.\n"
        f"   {BRAND_NAME} -- {PRODUCT_NAME}. */\n"
        f":root {{\n{body}\n}}\n"
    )


def node_styles() -> dict[str, dict[str, Any]]:
    """How a diagram draws each kind of node.

    `static/app.js` builds the same six from the served tokens, and `export.py`
    imports this for the SVG it writes into a deliverable, so the diagram on
    screen and the diagram in the PDF are the same drawing.
    """
    ink = palette()
    return {
        "standby": {
            "fill": ink["surface"],
            "stroke": ink["line-strong"],
            "text": ink["secondary"],
            "dashed": True,
        },
        "entry": {
            "fill": ink["secondary"],
            "stroke": ink["secondary"],
            "text": ink["surface"],
        },
        "cache": {
            "fill": ink["accent-soft"],
            "stroke": ink["line-strong"],
            "text": ink["ink"],
        },
        "data": {"fill": ink["accent"], "stroke": ink["accent"], "text": ink["surface"]},
        "compute": {"fill": ink["primary"], "stroke": ink["primary"], "text": ink["surface"]},
        "plain": {"fill": ink["surface"], "stroke": ink["line-strong"], "text": ink["ink"]},
    }
