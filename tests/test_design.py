"""Design generator tests: determinism, escaping, and self-containment."""

from __future__ import annotations

from sovereign.labor.design import (
    DESIGN_KEYWORDS,
    brand_kit,
    brand_palette,
    landing_page_html,
    logo_svg,
    social_card_svg,
)

HOSTILE = "<script>alert(1)</script>"
KIT_FILES = {"logo.svg", "social_card.svg", "index.html", "brand.md"}


def test_design_keywords_cover_the_offer():
    assert isinstance(DESIGN_KEYWORDS, tuple)
    for keyword in ("logo", "brand", "landing page", "social", "icon set"):
        assert keyword in DESIGN_KEYWORDS


def test_palette_deterministic_and_valid_hex():
    first = brand_palette("Acme Robotics")
    second = brand_palette("Acme Robotics")
    assert first == second
    assert set(first) == {"primary", "secondary", "accent", "ink", "bg"}
    for value in first.values():
        assert value.startswith("#") and len(value) == 7
        int(value[1:], 16)  # parses as hex
    assert brand_palette("Acme Robotics") != brand_palette("Other Brand")


def test_same_input_produces_identical_bytes():
    kit_a = brand_kit("Acme Robotics", "A landing page for a robotics startup.")
    kit_b = brand_kit("Acme Robotics", "A landing page for a robotics startup.")
    assert {name: text.encode() for name, text in kit_a.items()} == {
        name: text.encode() for name, text in kit_b.items()
    }
    assert logo_svg("Acme").encode() == logo_svg("Acme").encode()
    assert (
        social_card_svg("Launch day", "Acme").encode()
        == social_card_svg("Launch day", "Acme").encode()
    )
    assert (
        landing_page_html("Acme", "Head", "Sub", "Go").encode()
        == landing_page_html("Acme", "Head", "Sub", "Go").encode()
    )


def test_hostile_input_is_escaped_in_every_output():
    kit = brand_kit(HOSTILE, HOSTILE + ' & "quoted" brief')
    for name, content in kit.items():
        assert "<script" not in content, name
        assert "</script>" not in content, name
    assert "<script" not in logo_svg(HOSTILE)
    assert "<script" not in social_card_svg(HOSTILE, HOSTILE)
    assert "<script" not in landing_page_html(HOSTILE, HOSTILE, HOSTILE, HOSTILE)


def test_brand_kit_returns_four_nontrivial_files():
    kit = brand_kit("Northline", "Brand kit for an autonomous firm.")
    assert set(kit) == KIT_FILES
    for name, content in kit.items():
        assert len(content) > 200, name
    palette = brand_palette("Northline\nBrand kit for an autonomous firm.")
    for hex_value in palette.values():
        assert hex_value in kit["brand.md"]  # brand.md documents the palette


def test_svg_outputs_look_like_svg():
    kit = brand_kit("Acme", "logo and social card")
    for name in ("logo.svg", "social_card.svg"):
        assert kit[name].startswith(("<svg", "<?xml")), name
    assert logo_svg("Acme").startswith(("<svg", "<?xml"))
    card = social_card_svg("Launch", "Acme")
    assert card.startswith(("<svg", "<?xml"))
    assert 'width="1200"' in card and 'height="630"' in card


def test_landing_page_is_single_file_with_no_external_urls():
    page = landing_page_html("Acme", "Launch fast", "Ship a clean site.", "Get started")
    lowered = page.lower()
    assert lowered.startswith("<!doctype html>")
    assert "</html>" in lowered
    assert "<script" not in lowered
    assert "<link" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered
    assert "http" not in lowered  # inline SVG omits xmlns: zero URLs anywhere

    kit_page = brand_kit("Acme", "site")["index.html"]
    assert "http" not in kit_page.lower()
    assert "<script" not in kit_page.lower()
    assert "@media" in kit_page  # responsive
