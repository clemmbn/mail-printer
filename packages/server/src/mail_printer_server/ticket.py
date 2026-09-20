"""
Ticket renderer: turns a submitted message into the exact PNG that the
thermal printer prints.

Purpose
-------
This module is the single source of truth for the printed layout. The admin
preview, the stored "reprint as-is" image and the bytes sent to print-agent
are all produced here, so what an admin sees is byte-for-byte what was
printed.

Main responsibilities
---------------------
- Lay out the ticket described in the Figma design (node 13:2, linked from
  CLAUDE.md): "New message" title between two rules, centred grey timestamp,
  sender name, round avatar + rounded speech bubble, and the decorative
  "Reply ?" pill with its send button.
- Word-wrap the message so a 3000-character message still renders (the
  ticket simply gets very tall).
- Replace glyphs JetBrains Mono cannot render with a placeholder, so the
  printer never spits out tofu boxes.
- Convert the sender photo to 1-bit for thermal printing with a selectable
  dithering method.

Non-obvious constraints
-----------------------
- Everything is drawn with pure Pillow `ImageDraw`. No browser, no SVG
  rasteriser: the server is a small VPS and the renderer must stay cheap.
  The two icons (send button, avatar placeholder) are bundled as PNGs from
  the repo's assets/, rather than pulling in an SVG rasteriser at runtime.
- The Figma mockup is 1080px wide, the printer is 576px wide, so every
  design measurement below is written as its Figma pixel value and
  multiplied by `SCALE`. Keep it that way: it makes diffing against the
  Figma trivial.
- The rendered image is RGB (not 1-bit). Dithering the *whole* ticket is
  print-agent's job; only the photo is dithered here, because that is a
  layout-affecting decision (a dithered avatar must be composited before
  the circle mask).
- Run this module directly to write a preview PNG (see `__main__`).
"""

from __future__ import annotations

import io
import logging
import unicodedata
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Geometry. All *_PX constants are Figma values at 1080px wide; `_s()` scales
# them to the 576px print width.
# --------------------------------------------------------------------------

TICKET_WIDTH = 576
SCALE = TICKET_WIDTH / 1080


def _s(figma_px: float) -> int:
    """
    Convert a Figma measurement (1080px-wide design) to printer pixels.

    Args:
        figma_px (float): Measurement as it appears in the Figma file.

    Returns:
        int: The same measurement at the 576px print width, at least 1px so
        hairlines never vanish to nothing.
    """
    return max(1, round(figma_px * SCALE))


MARGIN = _s(48)  # outer page margin on all four sides

# Header (Figma 19:135). The title sits *between* two rules on one line: each
# rule runs from the margin inwards and stops short of the centred title.
RULE_THICKNESS = _s(4)
RULE_WIDTH = _s(309)
RULE_TITLE_GAP = _s(24)

TITLE_SIZE = _s(48)
TIMESTAMP_SIZE = _s(32)
NAME_SIZE = _s(40)
MESSAGE_SIZE = _s(40)
PILL_TEXT_SIZE = _s(48)

# Vertical gaps, each the distance between two adjacent Figma line boxes.
GAP_TITLE_TIMESTAMP = _s(16)
GAP_TIMESTAMP_NAME = _s(64)
GAP_NAME_BUBBLE = _s(4)  # the name all but touches the bubble it labels
GAP_BUBBLE_REPLY = _s(96)

# Speech bubble (Figma 13:4) and the avatar beside it (13:3). Both x values
# are absolute, straight from the design, rather than derived from the avatar
# width: the gap between them is not a round number.
AVATAR_SIZE = _s(96)
AVATAR_OUTLINE = _s(4)
BUBBLE_LEFT = _s(168)
BUBBLE_RADIUS = _s(96)
BUBBLE_PAD_X = _s(48)
BUBBLE_PAD_Y = _s(40)
BUBBLE_OUTLINE = _s(4)
NAME_LEFT = _s(248)  # the name is indented further than the bubble's text

# Decorative reply row (Figma 13:31): a fixed-width pill plus a filled circle
# holding the send icon. Unlike the bubble, the pill does not hug its text.
PILL_WIDTH = _s(835)
PILL_HEIGHT = _s(125)
PILL_RADIUS = _s(96)  # larger than half the height, so the ends are semicircles
PILL_PAD_X = _s(48)
PILL_OUTLINE = _s(4)
PILL_SEND_GAP = _s(24)
SEND_CIRCLE_SIZE = _s(125)
SEND_ICON_SIZE = _s(80)  # smaller than the Figma's 96: the Phosphor glyph
# carries less internal padding than the design's hand-drawn plane, so at 96
# it crowds the circle.

# JetBrains Mono's "normal" leading: the Figma line boxes are 53px tall for a
# 40px font (53/40), applied to the em size.
LINE_SPACING = 1.325

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREY = (128, 128, 128)  # #808080 in the Figma: timestamp and "Reply ?"

TITLE_TEXT = "New message"
REPLY_TEXT = "Reply ?"
ANONYMOUS_NAME = "Anonymous"
TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"

# Substituted for any character JetBrains Mono has no glyph for (emoji, rare
# CJK, ...). A middle dot is narrow, unambiguous and always present in the font.
GLYPH_PLACEHOLDER = "\u00b7"  # MIDDLE DOT

_FONTS_DIR = Path(__file__).parent / "fonts" / "JetBrainsMono"
_FONT_TITLE = "JetBrainsMonoNL-ExtraBold.ttf"
_FONT_MEDIUM = "JetBrainsMonoNL-Medium.ttf"  # sender name, "Reply ?"
_FONT_REGULAR = "JetBrainsMonoNL-Regular.ttf"  # timestamp, message body

# Phosphor icons, copied from the repo's top-level assets/ at their native
# 96px. Pillow cannot read SVG and the server must stay dependency-light, so
# the PNG variants are bundled. Only the alpha channel is used: the files are
# transparent-black, and each icon is painted in whatever colour it needs.
_ICONS_DIR = Path(__file__).parent / "assets"
_SEND_ICON_PATH = _ICONS_DIR / "paper-plane-tilt-light.png"
_AVATAR_ICON_PATH = _ICONS_DIR / "user-circle-light.png"

# Dithering methods for the avatar photo.
#   "floyd-steinberg": Pillow's error-diffusion. Keeps perceived gradients on
#       faces at the cost of a noisy, slightly "dirty" look on a 70px circle,
#       and error diffusion can smear on a printer with heavy dot gain.
#   "threshold": hard cut at mid-grey. Crisp and predictable on cheap thermal
#       paper but flattens skin tones into blobs.
# Default is Floyd-Steinberg; the final call is pending a real test print
# (tracked as an open decision in CLAUDE.md).
DITHER_FLOYD_STEINBERG = "floyd-steinberg"
DITHER_THRESHOLD = "threshold"
DEFAULT_DITHER = DITHER_FLOYD_STEINBERG
THRESHOLD_LEVEL = 128

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
# Per-(font file, size, char) memo of "does this font have a glyph for it".
# Probing costs a glyph render, and a 3000-char message would otherwise probe
# thousands of times.
_glyph_support_cache: dict[tuple[str, str], bool] = {}


# --------------------------------------------------------------------------
# Fonts and glyph fallback
# --------------------------------------------------------------------------


def _load_font(filename: str, size: int) -> ImageFont.FreeTypeFont:
    """
    Load a bundled JetBrains Mono face, cached by (filename, size).

    Args:
        filename (str): File name inside fonts/JetBrainsMono/.
        size (int): Pixel size.

    Returns:
        ImageFont.FreeTypeFont: The loaded font.

    Raises:
        OSError: If the font file is missing or unreadable.
    """
    key = (filename, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached

    path = _FONTS_DIR / filename
    logger.debug("Loading font %s at size %d", path, size)
    font = ImageFont.truetype(str(path), size)
    _font_cache[key] = font
    return font


def _font_supports(font: ImageFont.FreeTypeFont, char: str) -> bool:
    """
    Report whether `font` has a real glyph for `char`.

    FreeType silently falls back to the font's .notdef glyph (the "tofu" box)
    for unmapped code points, and Pillow exposes no cmap. So we render the
    character and compare the bitmap against the bitmap of U+FFFF, a permanently
    unassigned code point that therefore always renders as .notdef. Identical
    bitmaps mean `char` is unmapped too.

    Args:
        font (ImageFont.FreeTypeFont): Font to probe.
        char (str): Single character to test.

    Returns:
        bool: True if the font can render it, False if it would print tofu.
    """
    cache_key = (font.path, char)
    cached = _glyph_support_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # `bytes(mask)` (not `.tobytes()`) is how an ImagingCore is serialised.
        notdef = bytes(font.getmask("\uffff", mode="L"))
        candidate = bytes(font.getmask(char, mode="L"))
    except Exception:  # pragma: no cover - defensive: a broken glyph is "unsupported"
        logger.warning("Glyph probe failed for %r; treating as unsupported", char)
        _glyph_support_cache[cache_key] = False
        return False

    # Some fonts draw .notdef as nothing at all. In that case a blank glyph is
    # indistinguishable from a missing one, so trust blank glyphs (spaces are
    # filtered out before we get here anyway).
    supported = candidate != notdef or not any(notdef)
    _glyph_support_cache[cache_key] = supported
    return supported


def sanitise_for_font(text: str, font: ImageFont.FreeTypeFont) -> str:
    """
    Replace every character `font` cannot render with GLYPH_PLACEHOLDER.

    Whitespace is passed through untouched (it legitimately renders blank and
    would confuse the .notdef comparison), and so are newlines, which the
    wrapper handles as hard breaks.

    Args:
        text (str): Already normalised/validated text.
        font (ImageFont.FreeTypeFont): Font the text will be drawn with.

    Returns:
        str: Text containing only characters the font can draw.
    """
    out: list[str] = []
    replaced = 0
    for char in text:
        if char.isspace():
            out.append(char)
            continue
        if _font_supports(font, char):
            out.append(char)
            continue
        replaced += 1
        # Collapse a run of unsupported code points (an emoji is often several)
        # into a single placeholder so "👨‍👩‍👧" does not become five dots.
        if out and out[-1] == GLYPH_PLACEHOLDER:
            continue
        out.append(GLYPH_PLACEHOLDER)

    if replaced:
        logger.info("Replaced %d unrenderable character(s) with %r", replaced, GLYPH_PLACEHOLDER)
    return "".join(out)


# --------------------------------------------------------------------------
# Text measurement and wrapping
# --------------------------------------------------------------------------


def _line_height(font: ImageFont.FreeTypeFont) -> int:
    """
    Height of one text line in pixels, including leading.

    Measured off the em size rather than ascent+descent: JetBrains Mono's
    ascent+descent already carries most of its own leading, so multiplying
    that by LINE_SPACING stacked two leadings and made every block ~25%
    taller than the Figma's line boxes.
    """
    return round(font.size * LINE_SPACING)


def _split_long_word(
    draw: ImageDraw.ImageDraw, word: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """
    Hard-break a single word that is wider than the available width.

    Greedy per character rather than binary search: JetBrains Mono is
    monospaced so widths are linear, and a message is capped at 3000 chars,
    which keeps this trivially fast while staying correct if the font is ever
    swapped for a proportional one.

    Args:
        draw (ImageDraw.ImageDraw): Context used for measuring.
        word (str): The oversized word.
        font (ImageFont.FreeTypeFont): Font it will be drawn with.
        max_width (int): Maximum line width in pixels.

    Returns:
        list[str]: Chunks, each fitting within max_width.
    """
    chunks: list[str] = []
    current = ""
    for char in word:
        if current and draw.textlength(current + char, font=font) > max_width:
            chunks.append(current)
            current = char
            continue
        current += char
    if current:
        chunks.append(current)
    return chunks


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """
    Word-wrap `text` to `max_width`, honouring explicit newlines as hard breaks.

    Args:
        draw (ImageDraw.ImageDraw): Context used for measuring.
        text (str): Text to wrap.
        font (ImageFont.FreeTypeFont): Font it will be drawn with.
        max_width (int): Maximum line width in pixels.

    Returns:
        list[str]: Wrapped lines (at least one, possibly empty).
    """
    if not text:
        return [""]

    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = f"{current} {word}" if current else word
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue

            if current:
                lines.append(current)
                current = ""
            # The word alone may still not fit: break it by character.
            if draw.textlength(word, font=font) <= max_width:
                current = word
                continue
            chunks = _split_long_word(draw, word, font, max_width)
            lines.extend(chunks[:-1])
            current = chunks[-1] if chunks else ""

        lines.append(current)

    return lines


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    colour: tuple[int, int, int],
    left_x: int,
    top_y: int,
) -> int:
    """
    Draw left-aligned lines top-down.

    Args:
        draw (ImageDraw.ImageDraw): Draw context.
        lines (list[str]): Lines to draw.
        font (ImageFont.FreeTypeFont): Font.
        colour (tuple[int, int, int]): RGB fill.
        left_x (int): Left edge of every line.
        top_y (int): Top of the first line.

    Returns:
        int: Y immediately below the last line.
    """
    step = _line_height(font)
    y = top_y
    for line in lines:
        draw.text((left_x, y), line, font=font, fill=colour)
        y += step
    return y


# --------------------------------------------------------------------------
# Avatar
# --------------------------------------------------------------------------


def _dither(image: Image.Image, method: str) -> Image.Image:
    """
    Convert a greyscale image to pure black & white for thermal printing.

    Args:
        image (Image.Image): Source image in "L" mode.
        method (str): DITHER_FLOYD_STEINBERG or DITHER_THRESHOLD.

    Returns:
        Image.Image: A 1-bit image ("1" mode).

    Raises:
        ValueError: If `method` is unknown.
    """
    if method == DITHER_FLOYD_STEINBERG:
        return image.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    if method == DITHER_THRESHOLD:
        # point() on "L" then convert with dither=NONE gives a hard threshold.
        return image.point(lambda v: 255 if v >= THRESHOLD_LEVEL else 0, mode="L").convert(
            "1", dither=Image.Dither.NONE
        )
    raise ValueError(f"Unknown dither method: {method!r}")


def _square_crop(image: Image.Image) -> Image.Image:
    """Centre-crop `image` to a square (the largest one that fits)."""
    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _circle_mask(size: int) -> Image.Image:
    """
    Build an anti-aliased circular alpha mask of the given diameter.

    Drawn at 4x and downsampled: Pillow's ellipse has no anti-aliasing, and a
    hard-edged 70px circle looks visibly jagged once printed.
    """
    supersample = 4
    mask = Image.new("L", (size * supersample, size * supersample), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * supersample - 1, size * supersample - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def _default_avatar(size: int) -> Image.Image:
    """
    Draw the placeholder avatar used when the sender sent no photo: Phosphor's
    "user-circle" icon, which supplies its own circle so nothing else needs
    drawing.

    Args:
        size (int): Diameter in pixels.

    Returns:
        Image.Image: RGB image of exactly (size, size), black on white.
    """
    # Cropped to the glyph's own bounds before scaling: Phosphor pads its
    # icons inside the viewBox, and without this the placeholder circle would
    # print visibly smaller than a photo avatar, which fills the box exactly.
    icon = Image.open(_AVATAR_ICON_PATH)
    icon.load()
    alpha = icon.getchannel("A")
    alpha = alpha.crop(alpha.getbbox()).resize((size, size), Image.LANCZOS)

    avatar = Image.new("RGB", (size, size), WHITE)
    avatar.paste(Image.new("RGB", (size, size), BLACK), (0, 0), alpha)
    return avatar


def _load_photo(photo: Image.Image | bytes | str | Path) -> Image.Image:
    """
    Accept the several shapes callers have a photo in and return a PIL image.

    Args:
        photo (Image.Image | bytes | str | Path): Already-open image, raw
            encoded bytes, or a path on disk.

    Returns:
        Image.Image: The opened image (not yet cropped or converted).
    """
    if isinstance(photo, Image.Image):
        return photo
    if isinstance(photo, bytes):
        return Image.open(io.BytesIO(photo))
    return Image.open(str(photo))


def render_avatar(
    photo: Image.Image | bytes | str | Path | None,
    size: int = AVATAR_SIZE,
    dither: str = DEFAULT_DITHER,
) -> Image.Image:
    """
    Produce the round avatar pasted at the bottom-left of the ticket.

    Args:
        photo: Sender photo, or None to use the default avatar icon.
        size (int): Diameter in pixels.
        dither (str): Dithering method for the photo (ignored for the default
            avatar, which is already pure black & white).

    Returns:
        Image.Image: RGB image of (size, size), circular on a white square.

    Side effects:
        Logs which avatar path was taken; falls back to the default avatar
        (never raises) if the photo cannot be decoded.
    """
    if photo is None:
        logger.debug("No photo supplied; using default avatar")
        source = _default_avatar(size)
    else:
        try:
            opened = _load_photo(photo)
            square = _square_crop(opened.convert("L"))
            resized = square.resize((size, size), Image.LANCZOS)
            source = _dither(resized, dither).convert("RGB")
            logger.info("Rendered photo avatar (%dpx, dither=%s)", size, dither)
        except Exception:
            logger.exception("Could not render the sender photo; falling back to default avatar")
            source = _default_avatar(size)

    if photo is None:
        return source  # the icon is already a circle on white

    circular = Image.new("RGB", (size, size), WHITE)
    circular.paste(source, (0, 0), _circle_mask(size))

    # The design strokes the avatar circle, which also hides the mask's
    # anti-aliased edge against white paper.
    ring = Image.new("RGB", (size * 4, size * 4), WHITE)
    inset = AVATAR_OUTLINE * 2
    ImageDraw.Draw(ring).ellipse(
        (inset, inset, size * 4 - 1 - inset, size * 4 - 1 - inset),
        outline=BLACK,
        width=AVATAR_OUTLINE * 4,
    )
    ring = ring.resize((size, size), Image.LANCZOS)
    circular.paste(ring, (0, 0), ImageOps.invert(ring.convert("L")))

    return circular


# --------------------------------------------------------------------------
# Decorative footer (purely visual — "Reply ?" does nothing)
# --------------------------------------------------------------------------


def _icon_mask(path: Path, size: int) -> Image.Image:
    """
    Load a bundled icon and return its alpha channel as a paint mask.

    Args:
        path (Path): PNG to load, one of the _*_ICON_PATH constants.
        size (int): Side of the square icon box in pixels.

    Returns:
        Image.Image: "L" mask, white where the glyph is opaque.

    Side effects:
        Reads the PNG on every call; the files are a couple of KB and a ticket
        is rendered once per message, so no cache is warranted.
    """
    icon = Image.open(path)
    icon.load()
    return icon.getchannel("A").resize((size, size), Image.LANCZOS)


def _draw_send_button(image: Image.Image, left: int, top: int) -> None:
    """
    Draw the filled circle holding the send icon (Figma 13:36).

    Args:
        image (Image.Image): Ticket canvas, drawn on in place.
        left (int): Left edge of the circle's bounding box.
        top (int): Top edge of the circle's bounding box.

    Side effects:
        Mutates `image`.
    """
    circle = Image.new("RGB", (SEND_CIRCLE_SIZE, SEND_CIRCLE_SIZE), BLACK)
    image.paste(circle, (left, top), _circle_mask(SEND_CIRCLE_SIZE))

    # The glyph is white-on-black, so paint white through the icon's alpha.
    inset = (SEND_CIRCLE_SIZE - SEND_ICON_SIZE) // 2
    white = Image.new("RGB", (SEND_ICON_SIZE, SEND_ICON_SIZE), WHITE)
    image.paste(white, (left + inset, top + inset), _icon_mask(_SEND_ICON_PATH, SEND_ICON_SIZE))


def _draw_reply_row(image: Image.Image, draw: ImageDraw.ImageDraw, top_y: int) -> int:
    """
    Draw the decorative "Reply ?" pill and the send button beside it.

    Neither is functional: the ticket is paper. They exist because the design
    frames the message as a chat, and the affordance sells that.

    Args:
        image (Image.Image): Ticket canvas (needed for the pasted icon).
        draw (ImageDraw.ImageDraw): Draw context over `image`.
        top_y (int): Top edge of the row.

    Returns:
        int: Y immediately below the row.

    Side effects:
        Mutates `image`.
    """
    font = _load_font(_FONT_MEDIUM, PILL_TEXT_SIZE)

    draw.rounded_rectangle(
        (MARGIN, top_y, MARGIN + PILL_WIDTH, top_y + PILL_HEIGHT),
        radius=PILL_RADIUS,
        outline=BLACK,
        width=PILL_OUTLINE,
    )

    ascent, descent = font.getmetrics()
    text_y = top_y + (PILL_HEIGHT - (ascent + descent)) // 2
    draw.text((MARGIN + PILL_PAD_X, text_y), REPLY_TEXT, font=font, fill=GREY)

    _draw_send_button(image, MARGIN + PILL_WIDTH + PILL_SEND_GAP, top_y)

    return top_y + PILL_HEIGHT


# --------------------------------------------------------------------------
# The ticket itself
# --------------------------------------------------------------------------


def _draw_rule(draw: ImageDraw.ImageDraw, left: int, centre_y: int, width: int) -> None:
    """
    Draw one of the two horizontal rules flanking the title.

    Args:
        draw (ImageDraw.ImageDraw): Draw context.
        left (int): Left end of the rule.
        centre_y (int): Y the rule is centred on (the title's midline).
        width (int): Rule length in pixels; anything <= 0 draws nothing.
    """
    if width <= 0:
        return
    top = centre_y - RULE_THICKNESS // 2
    draw.rectangle((left, top, left + width - 1, top + RULE_THICKNESS - 1), fill=BLACK)


def _prepare_text(text: str, font: ImageFont.FreeTypeFont) -> str:
    """
    NFC-normalise, strip control characters (keeping newlines) and apply the
    glyph fallback.

    The submit route sanitises input too; doing it again here keeps the
    renderer safe to call from the preview CLI and from tests with raw text.

    Args:
        text (str): Raw text.
        font (ImageFont.FreeTypeFont): Font it will be drawn with.

    Returns:
        str: Text that is safe to draw.
    """
    normalised = unicodedata.normalize("NFC", text)
    stripped = "".join(
        char
        for char in normalised
        if char == "\n" or not unicodedata.category(char).startswith("C")
    )
    return sanitise_for_font(stripped, font)


def render_ticket(
    message: str,
    name: str | None = None,
    photo: Image.Image | bytes | str | Path | None = None,
    timestamp: datetime | None = None,
    dither: str = DEFAULT_DITHER,
) -> Image.Image:
    """
    Render the full ticket.

    Layout, top to bottom (Figma node 13:2, linked from CLAUDE.md):
    rule / "New message" / rule on one line, centred grey timestamp, sender
    name, then the speech bubble with the round avatar at its bottom-left,
    and finally the decorative "Reply ?" pill + send button.

    Args:
        message (str): The message body. 1-3000 characters; longer simply
            produces a taller ticket, it is the submit route that enforces
            the cap.
        name (str | None): Sender name; blank or None prints "Anonymous".
        photo: Sender photo (PIL image, encoded bytes or a path), or None for
            the empty avatar circle.
        timestamp (datetime | None): Time shown on the ticket; defaults to now.
        dither (str): Photo dithering method, see DITHER_* constants.

    Returns:
        Image.Image: RGB image, TICKET_WIDTH wide with a variable height.
    """
    title_font = _load_font(_FONT_TITLE, TITLE_SIZE)
    meta_font = _load_font(_FONT_REGULAR, TIMESTAMP_SIZE)
    name_font = _load_font(_FONT_MEDIUM, NAME_SIZE)
    body_font = _load_font(_FONT_REGULAR, MESSAGE_SIZE)

    display_name = _prepare_text((name or "").strip() or ANONYMOUS_NAME, name_font)
    body_text = _prepare_text(message, body_font)
    stamp = (timestamp or datetime.now()).strftime(TIMESTAMP_FORMAT)

    # Measure on a throwaway 1x1 canvas: we need the wrapped line count before
    # we know how tall the real image must be.
    measuring = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    bubble_right = TICKET_WIDTH - MARGIN
    # 408px, against the design's 409.6. Note the printed line still holds one
    # character fewer than the Figma's 32: at this size Pillow rounds JetBrains
    # Mono's advance up to a whole 13px, and 32 * 13 overflows the bubble. The
    # design's own 12.8px advance is simply not available on a pixel grid.
    bubble_text_width = bubble_right - BUBBLE_LEFT - 2 * BUBBLE_PAD_X

    body_lines = _wrap_text(measuring, body_text, body_font, bubble_text_width)
    bubble_height = len(body_lines) * _line_height(body_font) + 2 * BUBBLE_PAD_Y
    # The avatar hangs off the bubble's bottom-left, so a one-line bubble must
    # still leave room for it or the row would collapse onto the name.
    row_height = max(bubble_height, AVATAR_SIZE)

    title_height = _line_height(title_font)
    meta_height = _line_height(meta_font)
    name_height = _line_height(name_font)

    total_height = (
        MARGIN
        + title_height
        + GAP_TITLE_TIMESTAMP
        + meta_height
        + GAP_TIMESTAMP_NAME
        + name_height
        + GAP_NAME_BUBBLE
        + row_height
        + GAP_BUBBLE_REPLY
        + PILL_HEIGHT
        + MARGIN
    )

    logger.info(
        "Rendering ticket: name=%r, %d chars -> %d lines, photo=%s, %dx%d",
        display_name,
        len(body_text),
        len(body_lines),
        photo is not None,
        TICKET_WIDTH,
        total_height,
    )

    image = Image.new("RGB", (TICKET_WIDTH, total_height), WHITE)
    draw = ImageDraw.Draw(image)

    y = MARGIN

    # Header: the title centred on the page with a rule either side of it,
    # both rules crossing at the title's vertical midpoint.
    title_width = int(draw.textlength(TITLE_TEXT, font=title_font))
    title_left = (TICKET_WIDTH - title_width) // 2
    draw.text((title_left, y), TITLE_TEXT, font=title_font, fill=BLACK)

    rule_y = y + title_height // 2
    _draw_rule(draw, MARGIN, rule_y, min(RULE_WIDTH, title_left - RULE_TITLE_GAP - MARGIN))
    rule_right_left = max(
        title_left + title_width + RULE_TITLE_GAP, TICKET_WIDTH - MARGIN - RULE_WIDTH
    )
    _draw_rule(draw, rule_right_left, rule_y, TICKET_WIDTH - MARGIN - rule_right_left)
    y += title_height + GAP_TITLE_TIMESTAMP

    # Centred grey timestamp.
    stamp_width = int(draw.textlength(stamp, font=meta_font))
    draw.text(((TICKET_WIDTH - stamp_width) // 2, y), stamp, font=meta_font, fill=GREY)
    y += meta_height + GAP_TIMESTAMP_NAME

    # Sender name, indented past the bubble's own left edge as in the design.
    draw.text((NAME_LEFT, y), display_name, font=name_font, fill=BLACK)
    y += name_height + GAP_NAME_BUBBLE

    # Avatar + speech bubble, bottom-aligned so the avatar hugs the bubble's
    # bottom-left corner however long the message is.
    row_bottom = y + row_height

    avatar = render_avatar(photo, AVATAR_SIZE, dither)
    image.paste(avatar, (MARGIN, row_bottom - AVATAR_SIZE))

    bubble_top = row_bottom - bubble_height
    draw.rounded_rectangle(
        (BUBBLE_LEFT, bubble_top, bubble_right, row_bottom),
        radius=BUBBLE_RADIUS,
        outline=BLACK,
        width=BUBBLE_OUTLINE,
    )
    _draw_lines(
        draw, body_lines, body_font, BLACK, BUBBLE_LEFT + BUBBLE_PAD_X, bubble_top + BUBBLE_PAD_Y
    )

    _draw_reply_row(image, draw, row_bottom + GAP_BUBBLE_REPLY)

    return image


def render_ticket_png(
    message: str,
    name: str | None = None,
    photo: Image.Image | bytes | str | Path | None = None,
    timestamp: datetime | None = None,
    dither: str = DEFAULT_DITHER,
) -> bytes:
    """
    Render the ticket and encode it as PNG bytes, ready to store on disk and
    ship over the WebSocket.

    Args: see `render_ticket`.

    Returns:
        bytes: Encoded PNG.
    """
    image = render_ticket(message, name=name, photo=photo, timestamp=timestamp, dither=dither)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    logger.info("Encoded ticket PNG: %d bytes", len(data))
    return data


def build_fallback_text(
    message: str, name: str | None = None, timestamp: datetime | None = None
) -> str:
    """
    Build the plain-text version print-agent prints if image printing fails.

    Args:
        message (str): Message body.
        name (str | None): Sender name; blank prints "Anonymous".
        timestamp (datetime | None): Defaults to now.

    Returns:
        str: Timestamp, name and message on separate lines.
    """
    stamp = (timestamp or datetime.now()).strftime(TIMESTAMP_FORMAT)
    display_name = (name or "").strip() or ANONYMOUS_NAME
    return f"{stamp}\n{display_name}\n\n{message}"


def _preview(output_dir: Path) -> list[Path]:
    """
    Write preview PNGs for the four cases the Figma has to be checked against.

    Args:
        output_dir (Path): Directory to write into; created if missing.

    Returns:
        list[Path]: The files written.

    Side effects:
        Writes PNG files to disk and logs each one.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime(2026, 9, 20, 14, 30)

    cases = {
        "short": ("Hey! Your printer works. See you Saturday?", "Clément"),
        "no-name": ("A message from a stranger, with no name attached.", ""),
        "long": ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 60, "Marathon"),
        "emoji": ("Bravo 👏🏼 for the printer — très beau projet 🎉", "Émilie"),
    }

    written: list[Path] = []
    for label, (message, name) in cases.items():
        path = output_dir / f"ticket-{label}.png"
        render_ticket(message[:3000], name=name, timestamp=stamp).save(path)
        logger.info("Wrote preview %s", path)
        written.append(path)

    # With-photo case: a synthetic gradient stands in for a real upload so the
    # preview needs no fixture file on disk.
    gradient = Image.linear_gradient("L").resize((400, 300))
    path = output_dir / "ticket-photo.png"
    render_ticket(
        "This one has a photo in the avatar circle.",
        name="With photo",
        photo=gradient,
        timestamp=stamp,
    ).save(path)
    logger.info("Wrote preview %s", path)
    written.append(path)

    return written


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    parser = argparse.ArgumentParser(description="Write preview ticket PNGs (no printer needed).")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.cwd() / "ticket-previews",
        help="Directory to write the preview PNGs into.",
    )
    args = parser.parse_args()

    for written_path in _preview(args.out):
        print(f"wrote {written_path}")
