"""
Central style -> motion registry (Oct 25 2026).

This is the ONE place that answers "which font, and which motion-preset
class, does style X use" — the architecture change requested after it was
found that `edl.text_animation` reached every renderer call site but was
never actually read by render/block_pipeline.py, so every style rendered
with the exact same calm pop-in regardless of what the entrepreneur picked
(see the module docstrings in render/text_fx.py's new preset classes for
the full before/after).

Nothing outside this module should hardcode a style key, a style's font, or
`if style == "..."` branching — webapp/app.py's style picker, edl/format_001.py
(resolving the font for an EDL), and render/block_pipeline.py (choosing the
preset class for HOOK/USP) all import from here instead. Adding a 5th style
later means adding one entry to STYLE_DEFS below, nowhere else.

Every preset class referenced here shares ONE constructor/draw interface
(see render/text_fx.py's `_StackedWordPreset` docstring and `CapturePreset`'s
own docstring):
    __init__(text, font_path, fill=WHITE, *, accent=ACCENT, compact=False)
    draw(canvas, elapsed, span, cx=None, cy=None, *, target_h=None, target_w=None)
`compact=False` is the full-impact HOOK degree of a style family;
`compact=True` is the smaller/faster/more-readable USP degree of the SAME
family — same motion DNA, different intensity (HOOK = attention, USP =
readability), never a different, unrelated animation.
"""

from __future__ import annotations

from render.text_fx import BoldPreset, CapturePreset, CleanPreset, PremiumPreset

# font is a bare key into assets/fonts/ — either "<file>.ttf" (loose files,
# e.g. Anton/ArchivoBlack live straight in assets/fonts/) or
# "<categorie>/<file>.ttf" (assets/fonts/<categorie>/...) — same convention
# webapp/app.py's own font picker and render/pipeline.py's _resolve_punch_font
# already use. Verified against the actual files on disk (not guessed) —
# ArchivoBlack-Regular.ttf in particular lives LOOSE in assets/fonts/, not
# under stoer/, even though the webapp's dropdown groups it under the
# "Stoer / Impact" label.
STYLE_DEFS: dict[str, dict] = {
    "punchy": {
        "label": "Punchy",
        "description": "Stevig, uppercase, cascade-opbouw — voor een scherpe aanbieding",
        "font": "stoer/BebasNeue-Regular.ttf",
        "preset": CapturePreset,
    },
    "bold": {
        "label": "Bold",
        "description": "Zwaar en sportief — segmenten schuiven snel in beeld",
        "font": "ArchivoBlack-Regular.ttf",
        "preset": BoldPreset,
    },
    "clean": {
        "label": "Clean",
        "description": "Rustig en strak — voor een zakelijke, nette uitstraling",
        "font": "strak/Oswald-Regular.ttf",
        "preset": CleanPreset,
    },
    "premium": {
        "label": "Premium",
        "description": "Subtiel en verfijnd — voor een premium uitstraling",
        "font": "elegant/PlayfairDisplay-Regular.ttf",
        "preset": PremiumPreset,
    },
}

DEFAULT_STYLE = "punchy"

# Ordered the same way webapp/app.py's picker has always shown them
# (Punchy first, as the existing default) with Bold inserted right after —
# the one new option the brief asked for.
STYLE_ORDER = ["punchy", "bold", "clean", "premium"]


def resolve_style(style: str | None) -> str:
    """Never raises — an unknown/missing style key (an EDL built before this
    field existed, or a bad form submission) falls back to DEFAULT_STYLE,
    the same "never break a render over a bad enum value" posture
    `_resolve_punch_font` already uses for a missing font file."""
    return style if style in STYLE_DEFS else DEFAULT_STYLE


def style_font(style: str | None) -> str:
    return STYLE_DEFS[resolve_style(style)]["font"]


def style_preset_class(style: str | None):
    return STYLE_DEFS[resolve_style(style)]["preset"]


def style_choices() -> list[tuple[str, str, str, str]]:
    """(key, label, description, font) in display order — webapp/app.py's
    style picker reads this directly instead of keeping its own, separate
    font mapping that could drift from this one."""
    return [
        (key, STYLE_DEFS[key]["label"], STYLE_DEFS[key]["description"], STYLE_DEFS[key]["font"])
        for key in STYLE_ORDER
    ]
