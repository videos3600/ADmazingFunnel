"""
Format 001: a base 5-block structure —
HOOK (hook text + merk-badge, both overlaid on the first footage clip) ->
EXPERIENCE -> USP -> DESIRE -> CTA — that GROWS up to MAX_BLOCKS (8) when
more footage is uploaded.

Product decision (Oct 2026, confirmed with the client): v1 does NOT scale
these numbers to a target total length. These are Format 001's one and only
timings for now — no dynamic timing system until one format has actually
been judged end to end.

Revision (Oct 17 2026, client: "Brand mag in overlay. Dus het worden er dat
wat minder blocks. [...] Elke block duur 4 seconden. 1 Shot per blok
graag."), superseding the earlier 7-block/32.5s table:
  - BRAND is no longer a standalone closing block — it is ONLY the overlay
    badge stamped on the footage block that carries `logo_overlay` (see
    below). The client was explicit that this overlay still needs its own
    "vette animatie" (a punchier, more deliberate motion than today's
    simple pop-in badge) but hasn't specified it yet ("Ik laat je nog weten
    hoe ik dat wil") — so render/block_pipeline.py's badge compositing is
    UNCHANGED in that respect and stays a placeholder until that spec
    arrives. Do not polish that animation preemptively.
  - every block is a flat, equal 4.0s — no more per-role timing tuned to
    the reference edit's pacing (that was the 7-block table's whole
    premise; the client has now explicitly moved off it).
  - "1 shot per blok" was already true of this implementation (one uploaded
    clip per footage block) — not a code change, just confirms the
    Reference Match Pass's multi-shot-per-chapter idea was NOT adopted.

Second revision (same day, client: "Maak de blocken max 8."): block count
now SCALES with how much footage is uploaded, instead of always being
fixed. BASE_ROLE_ORDER is the floor — with `len(BASE_FOOTAGE_ROLES)` or
fewer uploaded clips it behaves exactly as before (cycling through what's
there, same as any earlier round). Every uploaded clip BEYOND the base
footage slots gets its own extra "MOMENT" footage block, inserted right
before CTA, up to MAX_EXTRA_FOOTAGE_BLOCKS — so the ceiling is MAX_BLOCKS
(8) blocks on a big enough upload. An upload beyond MAX_USED_CLIPS is NOT
silently cycled into an existing slot and not shown anywhere — see
build_format_001_edl's `unused_clip_count` docs below. There's no
subscription/paywall messaging for this cap (nothing in this fase-1 test
tool has a pricing tier to gate behind) — webapp/app.py just tells the
uploader plainly how many clips won't make the cut, the same self-serve-
diagnostic pattern already used for font_requested/font_used and
logo_cleanup.

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
format's footage blocks.

Fourth revision (same day, client: "Hooktekst moet op de eerste block samen
de eerste (hookfootage) [...] USP mag wel in een aparte block."): the old
standalone HOOK text card is GONE — the hook headline is now composited
straight onto the FIRST footage block's own clip (that block's role stays
named "HOOK", its `type` is now "footage" like any other footage block, and
it also still carries the brand-badge overlay that used to live on the old
"HERO" role — "HERO" is retired as a role entirely, folded into HOOK). USP
is unaffected — client was explicit it stays its own tekstkaart block. This
drops BASE_ROLE_ORDER from 6 roles to 5, which — through the exact same
MAX_BLOCKS-driven formulas as the second revision above, unchanged — raises
MAX_EXTRA_FOOTAGE_BLOCKS from 2 to 3 and MAX_USED_CLIPS from 5 to 6 as a
direct, mechanical consequence (not a separate decision): one fewer base
block leaves one more extra-block slot under the same MAX_BLOCKS=8 ceiling.
The client also asked for this to be explained to the entrepreneur — see
webapp/templates/index.html's new hook-explainer note above the dropzone,
and webapp/static/app.js's formatDurationNote, whose block-count constants
were updated to match this file's (still no shared config, see that
function's own comment).

Footage-role blocks (HOOK, EXPERIENCE, DESIRE, and any extra MOMENT blocks)
are filled from the uploaded clips in UPLOAD ORDER — there is deliberately
no vision/classification step in v1 (explicitly out of scope, same as
automatic shot selection and product cut-outs). Manual per-clip role
assignment is a real v1 feature too (the client asked for "upload volgorde,
of handmatig een rol per clip"), but has no UI yet — this round is schema +
generator + render only, not a UI rebuild (explicit client instruction).
`role_overrides` below is the seam a future per-clip role picker plugs into
without changing this function's signature (it only covers the base
footage roles — not the dynamic MOMENT extras, since those don't have a
single stable key to override).

Non-footage blocks (USP, CTA) are engine-generated text cards — see
render/block_pipeline.py for how they're actually drawn (reuses the exact
same pop-in/CTA-badge look the punch-text overlay already uses elsewhere,
so Format 001 doesn't introduce a third, inconsistent typography
treatment). The brand moment and the hook headline are NOT blocks of their
own — they're both composited straight onto the HOOK block's own footage
(see render/block_pipeline.py's overlay logic), the headline early (to
actually grab the viewer in the first instant) and the badge late (on the
back ~40%, unchanged from before), timed so the two never show at once.
"""

from __future__ import annotations

from .schema import EDL, Block, BlockRole, MusicCue

# Flat per-block duration (seconds) — every block, base or extra, is the
# same length. See module docstring: the client moved off per-role timing
# tuned to the reference edit in favour of one uniform number.
BLOCK_DURATION = 4.0

BASE_ROLE_ORDER: list[BlockRole] = ["HOOK", "EXPERIENCE", "USP", "DESIRE", "CTA"]

# Which roles are footage vs. engine-generated. Fixed for v1 — a role's TYPE
# is part of the format definition, not something per-client config changes.
ROLE_TYPES: dict[str, str] = {
    "HOOK": "footage",  # hook text + brand badge, both overlaid on this block's own clip — see module docstring's "Fourth revision"
    "EXPERIENCE": "footage",
    "USP": "tekstkaart",
    "DESIRE": "footage",
    "CTA": "overlay",
    "MOMENT": "footage",  # extra footage beat beyond the base footage slots — see module docstring
}
BASE_FOOTAGE_ROLES = [r for r in BASE_ROLE_ORDER if ROLE_TYPES[r] == "footage"]  # HOOK, EXPERIENCE, DESIRE

# HOOK carries BOTH the hook headline (early) and the brand badge (late, back
# ~40%) as overlays on its own footage — see Block.overlay_text /
# Block.logo_overlay and render/block_pipeline.py's combined compositor.
# "HERO" (the old separate footage role the badge used to live on) is
# retired — folded into HOOK, see module docstring.
HOOK_OVERLAY_ROLE: BlockRole = "HOOK"

EXTRA_FOOTAGE_ROLE: BlockRole = "MOMENT"
MAX_BLOCKS = 8
MAX_EXTRA_FOOTAGE_BLOCKS = MAX_BLOCKS - len(BASE_ROLE_ORDER)  # 8 - 5 = 3
# The most uploaded clips Format 001 will ever actually use — the base
# footage slots + up to MAX_EXTRA_FOOTAGE_BLOCKS extra MOMENT blocks.
# webapp/app.py uses this same number to tell the uploader how many of
# their files won't make the cut.
MAX_USED_CLIPS = len(BASE_FOOTAGE_ROLES) + MAX_EXTRA_FOOTAGE_BLOCKS  # 3 + 3 = 6


def build_format_001_edl(
    *,
    business_name: str,
    headline: str,            # HOOK text — now composited onto the first footage block's own clip, not a text card (see module docstring)
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
    covering only the base footage roles (HOOK/EXPERIENCE/DESIRE) — any
    role not present in it falls back to the upload-order cycling below (so
    partial manual overrides, e.g. only HOOK pinned, still work). The
    EDL's own `len(edl.blocks)` / `edl.duration` after this returns are the
    source of truth for how many blocks and how many seconds actually got
    built; webapp/app.py reads those rather than guessing.
    """
    if not clip_filenames:
        raise ValueError("Format 001 needs at least one uploaded clip for the footage blocks")

    role_overrides = role_overrides or {}
    n_clips = len(clip_filenames)
    n_base_footage = len(BASE_FOOTAGE_ROLES)

    # How many EXTRA footage blocks (beyond the base footage slots) this
    # upload earns — one per clip past the base, capped at
    # MAX_EXTRA_FOOTAGE_BLOCKS so the format never exceeds MAX_BLOCKS total.
    # A thin upload (<= the base footage slot count) gets zero extras and
    # behaves exactly as the base format always has.
    extra_blocks_n = max(0, min(n_clips - n_base_footage, MAX_EXTRA_FOOTAGE_BLOCKS))

    # Inserted right before CTA — "a couple more product beats, then the
    # call to action" reads naturally; HOOK/EXPERIENCE/USP/DESIRE never move.
    role_order: list[BlockRole] = BASE_ROLE_ORDER[:-1] + [EXTRA_FOOTAGE_ROLE] * extra_blocks_n + [BASE_ROLE_ORDER[-1]]

    # Fallback rules for missing/blank text inputs (explicitly requested):
    # a block never loses its screen time in v1 — it shows a safer generic
    # line instead, so a thin form submission still produces a complete,
    # correctly-timed ad rather than an error or a blank card.
    hook_text = (headline or "").strip() or (business_name or "NU").strip().upper()
    usp_text = (usp_text or "").strip() or hook_text
    cta_text = (cta or "").strip() or "BESTEL NU"

    # HOOK is footage now (see module docstring), so it's not in here — its
    # text goes on `overlay_text` below instead of `content`.
    role_content: dict[str, str] = {
        "USP": usp_text,
        "CTA": cta_text,
    }

    # Base footage roles (HOOK/EXPERIENCE/DESIRE) cycle through the upload
    # exactly as before — unchanged behaviour for a thin upload. Extra
    # MOMENT blocks take clips sequentially starting right after the base
    # footage slots (never cycled — they only exist because those clips are
    # actually there).
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
        is_hook = role == HOOK_OVERLAY_ROLE
        blocks.append(Block(
            role=role, type=btype, duration=BLOCK_DURATION, content=content,
            logo_overlay=is_hook,
            overlay_text=hook_text if is_hook else "",
        ))  # type: ignore[arg-type]

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
