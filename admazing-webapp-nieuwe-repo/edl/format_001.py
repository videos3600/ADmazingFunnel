"""
Format 001: a base 6-block structure —
HOOK -> HERO (+ merk-badge overlay) -> EXPERIENCE -> USP -> DESIRE -> CTA —
that GROWS up to MAX_BLOCKS (8) when more footage is uploaded.

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
  - every block is a flat, equal 4.0s — no more per-role timing tuned to
    the reference edit's pacing (that was the 7-block table's whole
    premise; the client has now explicitly moved off it).
  - "1 shot per blok" was already true of this implementation (one uploaded
    clip per footage block) — not a code change, just confirms the
    Reference Match Pass's multi-shot-per-chapter idea was NOT adopted.

Second revision (same day, client: "Maak de blocken max 8."): block count
now SCALES with how much footage is uploaded, instead of always being
exactly 6. BASE_ROLE_ORDER (HOOK/HERO/EXPERIENCE/USP/DESIRE/CTA) is the
floor — with 3 or fewer uploaded clips it behaves exactly as before
(cycling through what's there, same as any earlier round). Every uploaded
clip BEYOND the base 3 footage slots (HERO/EXPERIENCE/DESIRE) gets its own
extra "MOMENT" footage block, inserted right before CTA, up to
MAX_EXTRA_FOOTAGE_BLOCKS (2) — so the ceiling is 8 blocks / 32.0s on 5+
uploaded clips. A 6th+ uploaded clip is NOT silently cycled into an
existing slot and not shown anywhere — see build_format_001_edl's
`unused_clip_count` docs below. There's no subscription/paywall messaging
for this cap (nothing in this fase-1 test tool has a pricing tier to gate
behind) — webapp/app.py just tells the uploader plainly how many clips
won't make the cut, the same self-serve-diagnostic pattern already used for
font_requested/font_used and logo_cleanup.

Third revision (same day, client: "Format001 moet weg want elke edit moet
standaard de Format001 hebben. Format ia de volgorde van blocks en niet de
edit stijl. Flash, zoom etc is de edit stijl en daar alleen kan de
ondernemer uit kiezen."): this format's block structure is no longer one
option among several — it's now the ONLY render path, always on, for
every ad regardless of which edit style the entrepreneur picks. What used
to be the "Template" dropdown's one Format001-vs-other-templates choice is
gone; that dropdown now ONLY offers the six edit styles (Papercut/Rotator/
Splitter/Shake/Flash/Zoom), which this function takes as `edit_style` and
simply stores as the returned EDL's `template` field — see
render/block_pipeline.py for how each style then gets applied to this
format's footage blocks. Nothing about the block/role/timing logic below
changed for this revision; only which caller builds the EDL (webapp/app.py
calls this unconditionally now) and what `template` means downstream.

Footage-role blocks (HERO, EXPERIENCE, DESIRE, and any extra MOMENT blocks)
are filled from the uploaded clips in UPLOAD ORDER — there is deliberately
no vision/classification step in v1 (explicitly out of scope, same as
automatic shot selection and product cut-outs). Manual per-clip role
assignment is a real v1 feature too (the client asked for "upload volgorde,
of handmatig een rol per clip"), but has no UI yet — this round is schema +
generator + render only, not a UI rebuild (explicit client instruction).
`role_overrides` below is the seam a future per-clip role picker plugs into
without changing this function's signature (it only covers the base 3
footage roles — not the dynamic MOMENT extras, since those don't have a
single stable key to override).

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

# Flat per-block duration (seconds) — every block, base or extra, is the
# same length. See module docstring: the client moved off per-role timing
# tuned to the reference edit in favour of one uniform number.
BLOCK_DURATION = 4.0

BASE_ROLE_ORDER: list[BlockRole] = ["HOOK", "HERO", "EXPERIENCE", "USP", "DESIRE", "CTA"]

# Which roles are footage vs. engine-generated. Fixed for v1 — a role's TYPE
# is part of the format definition, not something per-client config changes.
ROLE_TYPES: dict[str, str] = {
    "HOOK": "tekstkaart",
    "HERO": "footage",
    "EXPERIENCE": "footage",
    "USP": "tekstkaart",
    "DESIRE": "footage",
    "CTA": "overlay",
    "MOMENT": "footage",  # extra footage beat beyond the base 3 — see module docstring
}
BASE_FOOTAGE_ROLES = [r for r in BASE_ROLE_ORDER if ROLE_TYPES[r] == "footage"]  # HERO, EXPERIENCE, DESIRE

# HERO carries the early brand moment as an overlay badge on its own footage
# (see Block.logo_overlay / render/block_pipeline.py), not a separate block.
HERO_LOGO_OVERLAY_ROLE: BlockRole = "HERO"

EXTRA_FOOTAGE_ROLE: BlockRole = "MOMENT"
MAX_BLOCKS = 8
MAX_EXTRA_FOOTAGE_BLOCKS = MAX_BLOCKS - len(BASE_ROLE_ORDER)  # 2
# The most uploaded clips Format 001 will ever actually use — 3 base footage
# slots + up to 2 extra MOMENT blocks. webapp/app.py uses this same number
# to tell the uploader how many of their files won't make the cut.
MAX_USED_CLIPS = len(BASE_FOOTAGE_ROLES) + MAX_EXTRA_FOOTAGE_BLOCKS  # 5


def build_format_001_edl(
    *,
    business_name: str,
    headline: str,            # HOOK card text
    cta: str,                 # CTA card text
    clip_filenames: list[str],
    edit_style: str,          # "Papercut" | "Rotator" | "Splitter" | "Shake" | "Flash" | "Zoom" — see render/block_pipeline.py
    usp_text: str = "",       # USP card text; "" falls back to the hook line (see fallback rules below)
    output_format: str = "9:16",
    music_file: str = "",
    logo: str = "",
    font_file: str = "",
    text_animation: str = "capture",
    role_overrides: dict[str, str] | None = None,
) -> EDL:
    """Builds one Format 001 EDL. Block count scales with how much footage
    was uploaded — see module docstring. `edit_style` is stored as-is on
    the returned EDL's `template` field — this function doesn't validate it
    against render/templates.TEMPLATES itself, webapp/app.py already does
    that before calling in. `role_overrides` is an optional
    {role: clip_filename} map for a future manual per-clip assignment UI,
    covering only the base 3 footage roles (HERO/EXPERIENCE/DESIRE) — any
    role not present in it falls back to the upload-order cycling below (so
    partial manual overrides, e.g. only HERO pinned, still work). The
    EDL's own `len(edl.blocks)` / `edl.duration` after this returns are the
    source of truth for how many blocks and how many seconds actually got
    built; webapp/app.py reads those rather than guessing.
    """
    if not clip_filenames:
        raise ValueError("Format 001 needs at least one uploaded clip for the footage blocks")

    role_overrides = role_overrides or {}
    n_clips = len(clip_filenames)
    n_base_footage = len(BASE_FOOTAGE_ROLES)  # 3

    # How many EXTRA footage blocks (beyond the base 3) this upload earns —
    # one per clip past the base, capped at MAX_EXTRA_FOOTAGE_BLOCKS so the
    # format never exceeds MAX_BLOCKS total. A thin upload (<=3 clips) gets
    # zero extras and behaves exactly as the base 6-block format always has.
    extra_blocks_n = max(0, min(n_clips - n_base_footage, MAX_EXTRA_FOOTAGE_BLOCKS))

    # Inserted right before CTA — "a couple more product beats, then the
    # call to action" reads naturally; HOOK/HERO/EXPERIENCE/USP/DESIRE never
    # move.
    role_order: list[BlockRole] = BASE_ROLE_ORDER[:-1] + [EXTRA_FOOTAGE_ROLE] * extra_blocks_n + [BASE_ROLE_ORDER[-1]]

    # Fallback rules for missing/blank text inputs (explicitly requested):
    # a block never loses its screen time in v1 — it shows a safer generic
    # line instead, so a thin form submission still produces a complete,
    # correctly-timed ad rather than an error or a blank card.
    hook_text = (headline or "").strip() or (business_name or "NU").strip().upper()
    usp_text = (usp_text or "").strip() or hook_text
    cta_text = (cta or "").strip() or "BESTEL NU"

    role_content: dict[str, str] = {
        "HOOK": hook_text,
        "USP": usp_text,
        "CTA": cta_text,
    }

    # Base footage roles (HERO/EXPERIENCE/DESIRE) cycle through the upload
    # exactly as before — unchanged behaviour for <=3 clips. Extra MOMENT
    # blocks take clips sequentially starting right after the base 3 (never
    # cycled — they only exist because those clips are actually there).
    blocks: list[Block] = []
    base_footage_seen = 0
    extra_footage_seen = 0
    for role in role_order:
        btype = ROLE_TYPES[role]
        if btype == "footage":
            if role == EXTRA_FOOTAGE_ROLE:
                content = clip_filenames[n_base_footage + extra_footage_seen]
                extra_footage_seen += 1
            else:
                content = role_overrides.get(role) or clip_filenames[base_footage_seen % n_clips]
                base_footage_seen += 1
        else:
            content = role_content[role]
        logo_overlay = role == HERO_LOGO_OVERLAY_ROLE
        blocks.append(Block(role=role, type=btype, duration=BLOCK_DURATION, content=content, logo_overlay=logo_overlay))  # type: ignore[arg-type]

    return EDL(
        template=edit_style,  # type: ignore[arg-type]
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
