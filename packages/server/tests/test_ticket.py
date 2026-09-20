"""
Tests for the ticket renderer.

Rendering is inherently visual, so these tests deliberately check only what
can be asserted mechanically: that every case renders without raising, that
the resulting image has plausible dimensions, that wrapping behaves (including
unbreakable words and hard newlines), and that the glyph fallback replaces
characters JetBrains Mono cannot draw. Visual fidelity to the Figma is checked
by eye with the preview PNGs written by `python -m ... ticket --out DIR`.
"""

from datetime import datetime

import pytest
from PIL import Image, ImageDraw

from mail_printer_server import ticket

STAMP = datetime(2026, 9, 20, 14, 30)


@pytest.fixture
def body_font():
    """The font the message body is drawn with."""
    return ticket._load_font(ticket._FONT_REGULAR, ticket.MESSAGE_SIZE)


@pytest.fixture
def draw():
    """A throwaway draw context, used only for text measurement."""
    return ImageDraw.Draw(Image.new("RGB", (1, 1)))


# --------------------------------------------------------------------------
# Word wrapping
# --------------------------------------------------------------------------


def test_short_text_stays_on_one_line(draw, body_font):
    assert ticket._wrap_text(draw, "hello there", body_font, 400) == ["hello there"]


def test_empty_text_yields_one_empty_line(draw, body_font):
    assert ticket._wrap_text(draw, "", body_font, 400) == [""]


def test_every_wrapped_line_fits_the_width(draw, body_font):
    text = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod " * 5
    max_width = 400

    lines = ticket._wrap_text(draw, text, body_font, max_width)

    assert len(lines) > 1
    for line in lines:
        assert draw.textlength(line, font=body_font) <= max_width


def test_newlines_are_hard_breaks(draw, body_font):
    assert ticket._wrap_text(draw, "one\ntwo", body_font, 400) == ["one", "two"]


def test_unbreakable_word_is_split_by_character(draw, body_font):
    max_width = 200
    lines = ticket._wrap_text(draw, "a" * 200, body_font, max_width)

    assert len(lines) > 1
    assert "".join(lines) == "a" * 200
    for line in lines:
        assert draw.textlength(line, font=body_font) <= max_width


# --------------------------------------------------------------------------
# Glyph fallback
# --------------------------------------------------------------------------


def test_supported_characters_pass_through(body_font):
    text = "Café — naïve, très bien!"
    assert ticket.sanitise_for_font(text, body_font) == text


def test_emoji_is_replaced_with_the_placeholder(body_font):
    result = ticket.sanitise_for_font("hi 🎉 there", body_font)

    assert "🎉" not in result
    assert ticket.GLYPH_PLACEHOLDER in result
    assert result.startswith("hi ")


def test_run_of_unsupported_characters_collapses_to_one_placeholder(body_font):
    result = ticket.sanitise_for_font("👨👩👧", body_font)

    assert result == ticket.GLYPH_PLACEHOLDER


def test_whitespace_is_never_replaced(body_font):
    assert ticket.sanitise_for_font("a\nb c", body_font) == "a\nb c"


def test_control_characters_are_stripped_but_newlines_kept(body_font):
    assert ticket._prepare_text("a\x00b\nc", body_font) == "ab\nc"


# --------------------------------------------------------------------------
# Full renders
# --------------------------------------------------------------------------


def test_short_message_renders():
    image = ticket.render_ticket("Hello printer!", name="Clem", timestamp=STAMP)

    assert image.width == ticket.TICKET_WIDTH
    # Header + timestamp + name + a one-line bubble + reply row: a few hundred px.
    assert 300 < image.height < 700


def test_blank_name_prints_anonymous(monkeypatch):
    """The renderer must substitute "Anonymous", so the drawn name is that."""
    drawn: list[str] = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    ticket.render_ticket("hi", name="   ", timestamp=STAMP)

    assert ticket.ANONYMOUS_NAME in drawn


def test_three_thousand_chars_renders_a_tall_ticket():
    message = "word " * 600  # 3000 characters
    assert len(message) == 3000

    image = ticket.render_ticket(message, name="Marathon", timestamp=STAMP)

    assert image.width == ticket.TICKET_WIDTH
    # ~100 wrapped lines at ~30px each: comfortably over a metre of paper.
    assert image.height > 2000


def test_render_with_photo_matches_the_no_photo_height():
    """The avatar is fixed-size, so a photo must not change the layout."""
    photo = Image.linear_gradient("L").resize((300, 200))

    with_photo = ticket.render_ticket("hi", name="A", photo=photo, timestamp=STAMP)
    without = ticket.render_ticket("hi", name="A", timestamp=STAMP)

    assert with_photo.size == without.size
    assert with_photo.tobytes() != without.tobytes()


def test_broken_photo_falls_back_to_the_default_avatar():
    image = ticket.render_ticket("hi", name="A", photo=b"not an image", timestamp=STAMP)

    assert image.width == ticket.TICKET_WIDTH


def test_render_ticket_png_returns_png_bytes():
    data = ticket.render_ticket_png("hello", name="Clem", timestamp=STAMP)

    assert data.startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------------
# Avatar and dithering
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", [ticket.DITHER_FLOYD_STEINBERG, ticket.DITHER_THRESHOLD])
def test_avatar_dither_methods_produce_black_and_white_pixels(method):
    photo = Image.linear_gradient("L").resize((200, 200))

    avatar = ticket.render_avatar(photo, size=ticket.AVATAR_SIZE, dither=method)

    assert avatar.size == (ticket.AVATAR_SIZE, ticket.AVATAR_SIZE)
    # Only pure black and pure white survive dithering (the circle mask's
    # anti-aliased edge is the sole exception, so sample the centre row).
    centre = ticket.AVATAR_SIZE // 2
    middle = [avatar.getpixel((x, centre)) for x in range(centre - 10, centre + 10)]
    assert set(middle) <= {(0, 0, 0), (255, 255, 255)}


def test_the_two_dither_methods_differ():
    photo = Image.linear_gradient("L").resize((200, 200))

    fs = ticket.render_avatar(photo, dither=ticket.DITHER_FLOYD_STEINBERG)
    th = ticket.render_avatar(photo, dither=ticket.DITHER_THRESHOLD)

    assert fs.tobytes() != th.tobytes()


def test_unknown_dither_method_is_rejected():
    with pytest.raises(ValueError):
        ticket._dither(Image.new("L", (4, 4)), "nope")


def test_default_avatar_is_used_when_no_photo():
    avatar = ticket.render_avatar(None)

    assert avatar.size == (ticket.AVATAR_SIZE, ticket.AVATAR_SIZE)
    # The placeholder is Phosphor's "user-circle", cropped to its own bounds:
    # the ring touches the top and bottom of the box, the corners stay white,
    # and the head glyph darkens the upper middle.
    size = ticket.AVATAR_SIZE
    centre = size // 2
    assert avatar.getpixel((centre, 1))[0] < 200
    assert avatar.getpixel((centre, size - 2))[0] < 200
    assert avatar.getpixel((0, 0)) == (255, 255, 255)
    # The head sits somewhere in the upper half; the icon is the light
    # (outline) variant, so don't pin it to an exact row.
    upper_interior = [avatar.getpixel((centre, y))[0] for y in range(size // 8, centre)]
    assert any(value < 200 for value in upper_interior)


# --------------------------------------------------------------------------
# Fallback text
# --------------------------------------------------------------------------


def test_fallback_text_contains_timestamp_name_and_message():
    text = ticket.build_fallback_text("hello", name="", timestamp=STAMP)

    assert "20/09/2026 14:30" in text
    assert ticket.ANONYMOUS_NAME in text
    assert "hello" in text


# --------------------------------------------------------------------------
# Figma fidelity
# --------------------------------------------------------------------------


def test_layout_constants_match_the_figma():
    """
    Lock the measurements taken from Figma node 13:2.

    These are the numbers a careless "tidy-up" would round off, and nothing
    else in the suite would notice: the ticket would still render, just not
    like the design.
    """
    assert ticket.TITLE_TEXT == "New message"
    assert (ticket.TITLE_SIZE, ticket.TIMESTAMP_SIZE) == (ticket._s(48), ticket._s(32))
    assert (ticket.NAME_SIZE, ticket.MESSAGE_SIZE) == (ticket._s(40), ticket._s(40))
    assert ticket.BUBBLE_LEFT == ticket._s(168)
    assert ticket.BUBBLE_RADIUS == ticket._s(96)
    assert ticket.AVATAR_SIZE == ticket._s(96)
    assert (ticket.PILL_WIDTH, ticket.PILL_HEIGHT) == (ticket._s(835), ticket._s(125))
    assert ticket.SEND_CIRCLE_SIZE == ticket._s(125)


def test_ticket_geometry_matches_the_design_frame():
    """
    The design frame is 1080x1010 for its sample message; rendered at print
    width and scaled back up, the ticket must land on that height (within a
    pixel of rounding), or the vertical rhythm has drifted.
    """
    sample = (
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Aliquam a pharetra "
        "elit. Nunc viverra rhoncus orci, quis lobortis purus ullamcorper sit amet. "
        "Nunc quis lacus eget felis sollicitudin venenatis."
    )
    image = ticket.render_ticket(sample, name="Timothée", timestamp=STAMP)

    assert image.width == ticket.TICKET_WIDTH
    assert abs(image.height / ticket.SCALE - 1010) <= 4


@pytest.mark.parametrize(
    "path, size",
    [
        (ticket._SEND_ICON_PATH, ticket.SEND_ICON_SIZE),
        (ticket._AVATAR_ICON_PATH, ticket.AVATAR_SIZE),
    ],
)
def test_bundled_icons_are_present_and_non_empty(path, size):
    """The icons are copied assets; a missing or blank one must fail loudly."""
    assert path.exists()
    mask = ticket._icon_mask(path, size)

    assert mask.size == (size, size)
    assert mask.getextrema()[1] > 0  # something is actually drawn


def test_send_icon_fits_inside_its_button():
    """The glyph must sit inside the circle, not touch its edge."""
    assert ticket.SEND_ICON_SIZE < ticket.SEND_CIRCLE_SIZE
