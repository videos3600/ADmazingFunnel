"""
Format 001: a fixed 6-block structure —
HOOK -> HERO (+ merk-badge overlay) -> EXPERIENCE -> USP -> DESIRE -> CTA.

Product decision (Oct 2026, confirmed with the client): v1 does NOT scale
these numbers to a target total length. These are Format 001's one and only
timings for now — no dynamic timing system until one format has actually
been judged end to end.

Revision (Oct 17 2026, client: "Brand mag in overlay. Dus het worden er dat
wat minder blocks. [...] Elke block duur 4 seconden. 1 Shot per blok
graag."), superseding the earlier 7-block/32.5s table:
  - BRAND is no longer a standalone closing block — it is ONLY the overlay
    badge stamped on HERO (see `logo_overlay` below). The client was
    explicit that this overlay still needs its own "vette animatie" (a
    punchier, more deliberate motion than today's simple pop-in badge) but
    hasn't specified it yet ("Ik laat je nog weten hoe ik dat wil") — so
    `_apply_logo_overlay` in render/block_pipeline.py is UNCHANGED for now
    and stays a placeholder until that spec arrives. Do not polish that
    animation preemptively.
  - every remaining role gets a flat, equal 4.0s — no more per-role timing
    tuned to the reference edit's pacing (that was the 7-block table's
    whole premise; the client has now explicitly moved off it).
  - "1 shot per blok" was already true of this implementation (FOOTAGE_ROLES
    assigns exactly one uploaded clip per footage role — see
    build_format_001_edl below) — not a code change, just confirms the
    Reference Match Pass's multi-shot-per-chapter idea was NOT adopted.
Current table: HOOK 4.0s / HERO 4.0s (+overlay) / EXPERIENCE 4.0s /
USP 4.0s / DESIRE 4.0s / CTA 4.0s = 24.0s.

Footage-role blocks (HERO, EXPERIENCE, DESIRE) are filled from the uploaded
clips in UPLOAD ORDER — there is deliberately no vision/classification step
in v1 (explicitly out of scope, same as automatic shot selection and product
cut-outs). Manual per-clip role assignment is a real v1 feature too (the
client asked for "upload volgorde, of handmatig een rol per clip"), but has
no UI yet — this round is schema + generator + render only, not a UI
rebuild (explicit client instruction). `role_overrides` below is the seam
a future per-clip role picker plugs into without changing this function's
signature.

Non-footage blocks (HOOK, USP, CTA) are engine-generated text cards — see
render/block_pipeline.py for how they're actually drawn (reuses the exact
same pop-in/CTA-badge look the punch-text overlay already uses elsewhere,
so Format 001 doesn't introduce a third, inconsistent typography
treatment). The brand moment is NOT a block of its own — it's the
`logo_overlay` flag on the HERO block, composited onto that footage's own
back portion (see render/block_pipeline.py's overlay logic).
"""

from __future__ import annotations

from .schema import EDL, Block, BlockRole, MusicCue

# Fixed v1 timing (seconds) — flat 4.0s per block, sums to 24.0s. See module
# docstring: BRAND dropped as a standalone block (overlay-only now), and the
# client moved off per-role timing tuned to the reference edit in favour of
# a uniform duration.
FORMAT_001_TIMING: dict[str, float] = {
    "HOOK": 4.0,
    "HERO": 4.0,
    "EXPERIENCE": 4.0,
    "USP": 4.0,
    "DESIRE": 4.0,
    "CTA": 4.0,
}
FORMAT_001_ROLE_ORDER: list[BlockRole] = ["HOOK", "HERO", "EXPERIENCE", "USP", "DESIRE", "CTA"]
FORMAT_001_TOTAL_DURATION = round(sum(FORMAT_001_TIMING.values()), 3)  # 24.0

# Which roles are footage vs. engine-generated. Fixed for v1 — a role's TYPE
# is part of the format definition, not something per-client config changes.
ROLE_TYPES: dict[str, str] = {
    "HOOK": "tekstkaart",
    "HERO": "footage",
    "EXPERIENCE": "footage",
    "USP": "tekstkaart",
    "DESIRE": "footage",
    "CTA": "overlay",
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
    # correctly-timed 24.0s ad rather than an error or a blank card.
    hook_text = (headline or "").strip() or (business_name or "NU").strip().upper()
    usp_text = (usp_text or "").strip() or hook_text
    cta_text = (cta or "").strip() or "BESTEL NU"

    role_content: dict[str, str] = {
        "HOOK": hook_text,
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
