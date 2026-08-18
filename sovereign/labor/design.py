"""Offline design deliverables: deterministic, sellable, dependency-free.

Pure stdlib (html, hashlib), no network, no randomness, no timestamps: every
color, shape, and line of copy derives from a sha256 of the inputs, so the
same brand+brief always reproduces byte-identical files. Every untrusted
input (brand, brief, headline, subhead, cta) is escaped with
html.escape(..., quote=True) before it touches SVG/HTML/markdown output, so
hostile briefs cannot inject markup or scripts into a deliverable.
"""

from __future__ import annotations

import hashlib
import html

DESIGN_KEYWORDS: tuple[str, ...] = (
    "logo",
    "brand",
    "branding",
    "design",
    "graphic",
    "mockup",
    "landing page",
    "social",
    "poster",
    "banner",
    "icon set",
)

_FONT_STACK = "'Avenir Next','Segoe UI',Helvetica,Arial,sans-serif"

_FEATURES: tuple[tuple[str, str], ...] = (
    ("Ready to launch", "A complete, self-contained package: no build step, no external assets, nothing to configure."),
    ("Built to convert", "One clear message, one call to action, and a layout that reads well on any screen size."),
    ("Consistent by design", "Logo, social card, and page share a single palette, so every touchpoint matches."),
    ("Yours to keep", "Plain SVG and HTML files you can edit, host anywhere, and extend without lock-in."),
    ("Fast everywhere", "Inline styles and vector graphics keep the whole page a single lightweight file."),
    ("Accessible defaults", "High-contrast ink on light backgrounds and semantic markup out of the box."),
)


def _digest(seed: str) -> bytes:
    return hashlib.sha256(str(seed).encode("utf-8")).digest()


def _esc(value: str) -> str:
    return html.escape(str(value), quote=True)


def _hsl_hex(hue: float, sat: float, light: float) -> str:
    """HSL -> #rrggbb by hand (import budget is html + hashlib only)."""
    hue = hue % 360.0
    chroma = (1.0 - abs(2.0 * light - 1.0)) * sat
    x = chroma * (1.0 - abs((hue / 60.0) % 2.0 - 1.0))
    if hue < 60:
        rgb = (chroma, x, 0.0)
    elif hue < 120:
        rgb = (x, chroma, 0.0)
    elif hue < 180:
        rgb = (0.0, chroma, x)
    elif hue < 240:
        rgb = (0.0, x, chroma)
    elif hue < 300:
        rgb = (x, 0.0, chroma)
    else:
        rgb = (chroma, 0.0, x)
    m = light - chroma / 2.0
    return "#" + "".join(
        f"{min(255, max(0, round((channel + m) * 255))):02x}" for channel in rgb
    )


def brand_palette(seed: str) -> dict[str, str]:
    """Deterministic five-color palette (hex) derived from a hash of *seed*."""
    d = _digest(seed)
    hue = ((d[0] << 8) | d[1]) % 360
    spread = 24 + d[2] % 24  # 24..47 degrees toward the secondary
    accent_turn = 150 + d[3] % 60  # 150..209 degrees: a contrasting accent
    return {
        "primary": _hsl_hex(hue, 0.62, 0.42),
        "secondary": _hsl_hex(hue + spread, 0.54, 0.34),
        "accent": _hsl_hex(hue + accent_turn, 0.72, 0.52),
        "ink": _hsl_hex(hue, 0.32, 0.13),
        "bg": _hsl_hex(hue, 0.36, 0.97),
    }


def _monogram(brand: str) -> str:
    """First letters of up to two words; alnum only, so inherently markup-safe."""
    letters = ""
    for word in str(brand).split():
        for ch in word:
            if ch.isalnum():
                letters += ch.upper()
                break
        if len(letters) == 2:
            break
    if not letters:
        letters = chr(ord("A") + _digest(brand)[4] % 26)
    return letters[:2]


def _logo_markup(
    brand: str, palette: dict[str, str], d: bytes, *, size: int, standalone: bool
) -> str:
    """Geometric monogram mark. Standalone adds xmlns + <title>; the inline
    variant omits both so an embedding page contains no URLs at all."""
    mono = _monogram(brand)
    font_size = 104 if len(mono) < 2 else 82
    rotation = (d[5] % 4) * 90
    xmlns = ' xmlns="http://www.w3.org/2000/svg"' if standalone else ""
    title = f"<title>{_esc(brand)} logo</title>" if standalone else ""
    return (
        f'<svg{xmlns} width="{size}" height="{size}" viewBox="0 0 256 256" '
        f'role="img" aria-label="{_esc(brand)} logo">'
        f"{title}"
        f'<rect width="256" height="256" rx="56" fill="{palette["primary"]}"/>'
        f'<path d="M128 24 A104 104 0 0 1 232 128 L128 128 Z" '
        f'fill="{palette["accent"]}" opacity="0.9" '
        f'transform="rotate({rotation} 128 128)"/>'
        f'<circle cx="128" cy="128" r="84" fill="none" '
        f'stroke="{palette["bg"]}" stroke-width="10" opacity="0.35"/>'
        f'<text x="128" y="132" text-anchor="middle" dominant-baseline="central" '
        f'font-family="{_FONT_STACK}" font-weight="700" '
        f'font-size="{font_size}" fill="{palette["bg"]}">{_esc(mono)}</text>'
        f"</svg>"
    )


def logo_svg(brand: str, *, seed: str | None = None) -> str:
    """Clean geometric monogram logo as a standalone SVG document."""
    effective = str(brand) if seed is None else seed
    return _logo_markup(
        brand, brand_palette(effective), _digest(effective), size=256, standalone=True
    )


def _wrap_lines(text: str, width: int, max_lines: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines or [""]


def social_card_svg(headline: str, brand: str, *, seed: str | None = None) -> str:
    """1200x630 social/OG card: gradient field, mark, wrapped headline."""
    effective = str(brand) if seed is None else seed
    palette = brand_palette(effective)
    d = _digest(effective)
    lines = _wrap_lines(str(headline)[:120], 30, 3)
    tspans = "".join(
        f'<tspan x="80" dy="{0 if index == 0 else 78}">{_esc(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    mark = _logo_markup(brand, palette, d, size=96, standalone=False).replace(
        "<svg ", '<svg x="80" y="64" ', 1
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" '
        f'viewBox="0 0 1200 630" role="img" aria-label="{_esc(brand)} social card">'
        f"<defs>"
        f'<linearGradient id="field" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{palette["primary"]}"/>'
        f'<stop offset="1" stop-color="{palette["secondary"]}"/>'
        f"</linearGradient>"
        f"</defs>"
        f'<rect width="1200" height="630" fill="url(#field)"/>'
        f"{mark}"
        f'<rect x="80" y="228" width="120" height="10" rx="5" '
        f'fill="{palette["accent"]}"/>'
        f'<text x="80" y="330" font-family="{_FONT_STACK}" font-weight="700" '
        f'font-size="62" fill="{palette["bg"]}">{tspans}</text>'
        f'<text x="80" y="560" font-family="{_FONT_STACK}" font-weight="600" '
        f'font-size="30" fill="{palette["bg"]}" opacity="0.85">{_esc(brand)}</text>'
        f"</svg>"
    )


def landing_page_html(
    brand: str, headline: str, subhead: str, cta: str, *, seed: str | None = None
) -> str:
    """One self-contained responsive page: inline CSS, inline SVG logo, no
    scripts, no external assets or URLs of any kind."""
    effective = str(brand) if seed is None else seed
    palette = brand_palette(effective)
    d = _digest(effective)
    logo = _logo_markup(brand, palette, d, size=44, standalone=False)
    start = d[6] % len(_FEATURES)
    picked = [_FEATURES[(start + offset) % len(_FEATURES)] for offset in range(3)]
    cards = "".join(
        f'<article class="card"><h3>{title}</h3><p>{body}</p></article>'
        for title, body in picked
    )
    b, h, s, c = _esc(brand), _esc(headline), _esc(subhead), _esc(cta)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{b}</title>\n"
        "<style>\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        f"body{{font-family:{_FONT_STACK};color:{palette['ink']};"
        f"background:{palette['bg']};line-height:1.6}}\n"
        ".wrap{max-width:960px;margin:0 auto;padding:0 24px}\n"
        "header{display:flex;align-items:center;gap:12px;padding:20px 0}\n"
        f"header .name{{font-weight:700;font-size:1.1rem;color:{palette['ink']}}}\n"
        f".hero{{background:linear-gradient(135deg,{palette['primary']},"
        f"{palette['secondary']});color:{palette['bg']};"
        "border-radius:16px;padding:72px 32px;text-align:center}\n"
        ".hero h1{font-size:2.4rem;line-height:1.2;margin-bottom:16px}\n"
        ".hero p{font-size:1.15rem;opacity:.92;max-width:640px;margin:0 auto 28px}\n"
        f".cta{{display:inline-block;background:{palette['accent']};"
        f"color:{palette['bg']};text-decoration:none;font-weight:700;"
        "padding:14px 28px;border-radius:10px}\n"
        ".features{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));"
        "gap:20px;padding:48px 0}\n"
        f".card{{background:{palette['bg']};border:1px solid {palette['secondary']}33;"
        "border-radius:12px;padding:24px;box-shadow:0 1px 3px "
        f"{palette['ink']}14}}\n"
        f".card h3{{color:{palette['primary']};margin-bottom:8px}}\n"
        ".contact{text-align:center;padding:24px 0 56px}\n"
        f".contact h2{{color:{palette['primary']};margin-bottom:8px}}\n"
        f"footer{{border-top:1px solid {palette['secondary']}33;"
        "padding:20px 0;text-align:center;font-size:.9rem;opacity:.8}\n"
        "@media (max-width:600px){.hero{padding:48px 20px}"
        ".hero h1{font-size:1.7rem}}\n"
        "</style>\n</head>\n<body>\n"
        '<div class="wrap">\n'
        f'<header>{logo}<span class="name">{b}</span></header>\n'
        '<main>\n<section class="hero">\n'
        f"<h1>{h}</h1>\n<p>{s}</p>\n"
        f'<a class="cta" href="#contact">{c}</a>\n'
        "</section>\n"
        f'<section class="features">{cards}</section>\n'
        '<section id="contact" class="contact">\n'
        f"<h2>Work with {b}</h2>\n"
        "<p>Reply to the proposal thread and the team will take it from there.</p>\n"
        "</section>\n</main>\n"
        f"<footer>{b} &#183; generated as a self-contained single file</footer>\n"
        "</div>\n</body>\n</html>\n"
    )


def _kit_copy(brand: str, brief: str) -> tuple[str, str]:
    """Deterministic headline/subhead derived from the brief's first sentence."""
    flat = " ".join(str(brief).split())
    first = flat
    for stop in (". ", "! ", "? "):
        cut = flat.find(stop)
        if cut != -1:
            first = flat[: cut + 1]
            break
    headline = first.strip().rstrip(".") or f"{str(brand)[:60]} is ready to launch"
    headline = headline[:90]
    remainder = flat[len(first):].strip()
    subhead = (
        remainder[:160]
        or "A complete brand presence — logo, social card, and landing page — "
        "delivered as clean files you own."
    )
    return headline, subhead


def brand_kit(brand: str, brief: str) -> dict[str, str]:
    """Sellable four-file design package, byte-stable for the same inputs."""
    seed = f"{brand}\n{brief}"
    headline, subhead = _kit_copy(brand, brief)
    return {
        "logo.svg": logo_svg(brand, seed=seed),
        "social_card.svg": social_card_svg(headline, brand, seed=seed),
        "index.html": landing_page_html(
            brand, headline, subhead, "Get started", seed=seed
        ),
        "brand.md": _brand_md(brand, brief, seed),
    }


def _brand_md(brand: str, brief: str, seed: str) -> str:
    palette = brand_palette(seed)
    b = _esc(" ".join(str(brand).split())[:80] or "Untitled brand")
    summary = _esc(" ".join(str(brief).split())[:300] or "No brief provided.")
    lines = (
        f"# {b} — brand kit",
        "",
        f"Brief: {summary}",
        "",
        "## Palette",
        "",
        "| Role | Hex | Usage |",
        "| --- | --- | --- |",
        f"| Primary | `{palette['primary']}` | Logo field, headings, hero background |",
        f"| Secondary | `{palette['secondary']}` | Gradients, borders, section accents |",
        f"| Accent | `{palette['accent']}` | Calls to action, highlights, links |",
        f"| Ink | `{palette['ink']}` | Body text on light backgrounds |",
        f"| Background | `{palette['bg']}` | Page and card backgrounds |",
        "",
        "## Files",
        "",
        "- `logo.svg` — standalone monogram logo; scales to any size.",
        "- `social_card.svg` — 1200x630 social/OG card.",
        "- `index.html` — self-contained landing page (inline CSS, no scripts, no external assets).",
        "",
        "## Usage",
        "",
        "- Keep clear space around the logo of at least 25% of its width.",
        "- Use Accent only for interactive or emphasis elements, never body text.",
        "- Set body text in Ink on Background; never set long text on Primary.",
        "- Regenerating with the same brand and brief reproduces identical files.",
        "",
    )
    return "\n".join(lines)
