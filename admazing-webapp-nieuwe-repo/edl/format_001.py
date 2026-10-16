"""
Format 001: a fixed 7-block structure —
HOOK -> HERO (+ merk-badge overlay, laatste ~40%) -> EXPERIENCE -> USP ->
DESIRE -> CTA -> BRAND (sluiting).

Product decision (Oct 2026, confirmed with the client): v1 does NOT scale
these numbers to a target total length. These are Format 001's one and only
timings for now — no dynamic timing system until one format has actually
been judged end to end.

This timing table replaces an earlier 12.2s placeholder (the client's own
rough guess before any reference existed). It is instead measured from the
client's actual uploaded reference edit (Jonny's Burgers, 31.44s, 14 scene
cuts analyzed via ffmpeg scene-cut detection + frame review) and confirmed
with the client message-for-message:
  - the reference showed BRAND appearing twice — early, as a logo badge
    OVERLAID on the HERO footage (not a standalone card), and again as a
    genuine standalone closing card — which the client confirmed was his
    original intent all along ("De brand overlay was de oorspronkelijke
    bedoeling. vroeg en op het einde.");
  - the reference had no explicit CTA (it relied solely on its hook/offer
    text), but the client explicitly chose to keep a dedicated CTA block in
    Format 001 regardless ("CTA laten we erin. Men moet tot actie overgaan
    hoe dan ook."), since not every future client will have a hard discount
    to lean on;
  - total duration grew from 12.2s to 32.5s to match the reference's actual
    pacing (12.2s left almost no room for the pop-in animation itself,
    which eats ~0.4s regardless of block length).
Final confirmed table (client: "Graag"):
  HOOK 3.0s / HERO 9.0s (+overlay) / EXPERIENCE 6.0s / USP 3.5s /
  DESIRE 5.5s / CTA 2.5s / BRAND 3.0s = 32.5s.

Footage-role blocks (HERO, EXPERIENCE, DESIRE) are filled from the uploaded
clips in UPLOAD ORDER — there is deliberately no vision/classification step
in v1 (explicitly out of scope, same as automatic shot selection and product
cut-outs). Manual per-clip role assignment is a real v1 feature too (the
client asked for "upload volgorde, of handmatig een rol per clip"), but has
no UI yet — this round is schema + generator + render only, not a UI
rebuild (explicit client instruction). `role_overrides` below is the seam
a future per-clip role picker plugs into without changing this function's
signature.

Non-footage blocks (HOOK, USP, CTA, BRAND) are engine-generated text/logo
cards — see render/block_pipeline.py for how they're actually drawn (reuses
the exact same pop-in/CTA-badge look the punch-text overlay already uses
elsewhere, so Format 001 doesn't introduce a third, inconsistent typography
treatment). HERO's early brand moment is NOT a block of its own — it's the
`logo_overlay` flag on the HERO block, composited onto that footage's own
back portion (see render/block_pipeline.py's overlay logic).
"""

from __future__ import annotations

from .schema import EDL, Block, BlockRole, MusicCue

# Fixed v1 timing (seconds) — sums to exactly 32.5s. See module docstring:
# measured from the client's actual uploaded reference edit and confirmed
# with him block by block.
FORMAT_001_TIMING: dict[str, float] = {
    "HOOK": 3.0,
    "HERO": 9.0,
    "EXPERIENCE": 6.0,
    "USP": 3.5,
    "DESIRE": 5.5,
    "CTA": 2.5,
    "BRAND": 3.0,
}
FORMAT_001_ROLE_ORDER: list[BlockRole] = ["HOOK", "HERO", "EXPERIENCE", "USP", "DESIRE", "CTA", "BRAND"]
FORMAT_001_TOTAL_DURATION = round(sum(FORMAT_001_TIMING.values()), 3)  # 32.5

# Which roles are footage vs. engine-generated. Fixed for v1 — a role's TYPE
# is part of the format definition, not something per-client config changes.
ROLE_TYPES: dict[str, str] = {
    "HOOK": "tekstkaart",
    "HERO": "footage",
    "EXPERIENCE": "footage",
    "USP": "tekstkaart",
    "DESIRE": "footage",
    "CTA": "overlay",
    "BRAND": "logo",
}
FOOTAGE_ROLES = [r for r in FORMAT_001_ROLE_ORDER if ROLE_TYPES[r] == "footage"]  # HERO, EXPERIENCE, DESIRE

# HERO carries the early brand moment as an overlay badge on its own footage
# (see Block.logo_overlay / render/block_pipeline.py), not a separate block.
HERO_LOGO_OVERLAY_ROLE: BlockRole = "HERO"


def build_format_001_edl(
    *,
    business_name: str,
    headline: str,            # HOOK card text
    cta: str,                 # CTA card text
    clip_filenames: list[str],
    usp_text: str = "",       # USP card text; "" falls back to the hook line (see fallback rules below)
    output_format: str = "9:16",
    music_file: str = "",
    logo: str = "",
    font_file: str = "",
    text_animation: str = "capture",
    role_overrides: dict[str, str] | None = None,
) -> EDL:
    """Builds one Format 001 EDL. `role_overrides` is an optional
    {role: clip_filename} map for a future manual per-clip assignment UI —
    any role not present in it falls back to the upload-order cycling below
    (so partial manual overrides, e.g. only HERO pinned, still work).
    """
    if not clip_filenames:
        raise ValueError("Format 001 needs at least one uploaded clip for the footage blocks")

    role_overrides = role_overrides or {}

    # Upload-order assignment: fewer uploaded clips than footage roles (e.g.
    # one client photo) cycles back to the start rather than failing — the
    # format's rhythm (3 footage beats) never depends on how much footage
    # came in. "Which clip is the HERO shot" is just "whichever one the
    # client uploaded first" until fase 2's real classification exists.
    assigned: dict[str, str] = {}
    for i, role in enumerate(FOOTAGE_ROLES):
        assigned[role] = role_overrides.get(role) or clip_filenames[i % len(clip_filenames)]

    # Fallback rules for missing/blank text inputs (explicitly requested):
    # a block never loses its screen time in v1 — it shows a safer generic
    # line instead, so a thin form submission still produces a complete,
    # correctly-timed 32.5s ad rather than an error or a blank card.
    hook_text = (headline or "").strip() or (business_name or "NU").strip().upper()
    usp_text = (usp_text or "").strip() or hook_text
    brand_text = (business_name or "").strip() or "ADMAZING"
    cta_text = (cta or "").strip() or "BESTEL NU"

    role_content: dict[str, str] = {
        "HOOK": hook_text,
        "BRAND": brand_text,
        "USP": usp_text,
        "CTA": cta_text,
    }

    blocks: list[Block] = []
    for role in FORMAT_001_ROLE_ORDER:
        btype = ROLE_TYPES[role]
        duration = FORMAT_001_TIMING[role]
        content = assigned[role] if btype == "footage" else role_content[role]
        logo_overlay = role == HERO_LOGO_OVERLAY_ROLE
        blocks.append(Block(role=role, type=btype, duration=duration, content=content, logo_overlay=logo_overlay))  # type: ignore[arg-type]

    return EDL(
        template="Format001",
        format=output_format,  # type: ignore[arg-type]
        clips=[],
        blocks=blocks,
        music=MusicCue(style="", bpm=0, file=music_file) if music_file else None,
        logo=logo,
        business_name=business_name,
        text_style="punch",
        font=font_file,
        text_animation=text_animation,  # type: ignore[arg-type]
    )
