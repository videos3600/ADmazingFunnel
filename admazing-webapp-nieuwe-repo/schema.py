"""
ADmazing EDL (Edit Decision List) schema.

The EDL is the contract between the AI layer (the "director") and the
render engine (the "editor"). The AI layer never touches pixels — it only
produces one of these structures. The render engine only ever reads one of
these structures; it never has to guess what the ad should look like.

Kept deliberately small for the MVP (fase 1): enough to drive the Papercut
template end to end. Extend with more fields (transitions, per-clip pan/zoom,
subtitle tracks, etc.) once the render engine needs them — never add a field
"for later" that nothing reads yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

Format = Literal["9:16", "1:1", "16:9"]


@dataclass
class ClipRef:
    """One shot in the timeline.

    `source` must exist in the assets folder passed to the renderer.
    `start`/`end` are positions on the OUTPUT timeline in seconds — the
    renderer takes the first (end - start) seconds of `source`, so source
    clips should already be roughly trimmed to their usable portion by the
    classification step (fase 2). For the fase-1 MVP, `source` files are
    used from their own beginning.
    """
    start: float
    end: float
    source: str
    tag: str = ""  # e.g. "exterior", "owner", "food-closeup", "customer" — for logging/debugging only

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass
class TextCue:
    """A line of on-screen text, shown for a window of the output timeline."""
    content: str
    start: float
    end: float
    kind: Literal["headline", "cta"] = "headline"


@dataclass
class MusicCue:
    style: str          # free-text description, e.g. "upbeat tropical / urban"
    bpm: int
    file: str = ""       # resolved asset filename; empty until fase-1 music library exists


BlockRole = Literal["HOOK", "HERO", "BRAND", "EXPERIENCE", "USP", "DESIRE", "CTA", "MOMENT"]
# "MOMENT" (added Oct 17 2026) is a generic extra footage beat — see
# edl/format_001.py's dynamic block count. Unlike the other roles it can
# appear MORE THAN ONCE in one EDL's `blocks` list (one per extra uploaded
# clip beyond the base 3 footage slots); nothing in the schema or renderer
# requires role values to be unique within a Block list, role is purely a
# semantic/logging label.
BlockType = Literal["footage", "tekstkaart", "logo", "overlay"]


@dataclass
class Block:
    """One slot in a format's fixed timeline (Format 001:
    HOOK -> HERO -> EXPERIENCE -> USP -> DESIRE -> CTA — see
    edl/format_001.py). BRAND is not a block of its own: it's only a badge
    overlaid on the back portion of HERO's footage (`logo_overlay` below).
    "BRAND" stays in the BlockRole Literal below for forward-compatibility
    (a future format may give it a standalone block again), but Format 001
    as of Oct 17 2026 never constructs one (client: "Brand mag in overlay.
    Dus het worden er dat wat minder blocks.").

    Unlike ClipRef (a position on an already-assembled timeline), a Block IS
    the timeline: render/block_pipeline.py renders each one as its own
    normalized segment and concatenates them in order, so `duration` here is
    that block's own length, not a start/end pair on a shared clock.

    `content` means different things per `type`, same resolve-by-filename
    pattern as edl.font/edl.logo elsewhere in this schema:
      - "footage": the clip filename (must exist in assets_dir)
      - "tekstkaart": the text to render (e.g. the USP line)
      - "logo": text shown alongside edl.logo (e.g. the business name) —
        content may be blank if edl.logo alone is enough
      - "overlay": the CTA line, stamped with the same badge style the
        punch-text CTA already uses elsewhere in this app

    Revision (Oct 17 2026, client: "Hooktekst moet op de eerste block samen
    de eerste (hookfootage)"): the HOOK line is no longer its own tekstkaart
    block — it's now composited straight onto the first footage block's own
    clip (see edl/format_001.py's "Fourth revision"). `overlay_text` below
    carries that text; `content` on that same block still holds the clip
    filename as any other "footage" block would.
    """
    role: BlockRole
    type: BlockType
    duration: float
    content: str = ""
    logo_overlay: bool = False  # "footage" blocks only — stamps edl.logo (or business_name) as a badge over this block's back portion
    overlay_text: str = ""  # "footage" blocks only — the HOOK role's headline text, composited onto this block's own footage instead of its own text card (see the revision note above)


@dataclass
class EDL:
    # Revision (Oct 17 2026, client: "Format001 moet weg want elke edit moet
    # standaard de Format001 hebben. Format ia de volgorde van blocks en niet
    # de edit stijl. Flash, zoom etc is de edit stijl..."): every EDL built
    # by webapp/app.py is now block-based (`blocks` populated, `clips`
    # empty) and rendered by render/block_pipeline.py — "Format001" is no
    # longer a selectable template value, because it's no longer optional.
    # `template` now names the EDIT STYLE (Papercut/Rotator/Splitter/Shake/
    # Flash/Zoom) applied to the footage blocks WITHIN that fixed timeline —
    # see render/block_pipeline.py's module docstring for exactly how each
    # style gets reused there. render/pipeline.py's Renderer/TEMPLATES
    # dispatch (the OLD render path, driven by `clips`/`text_cues`) still
    # exists and still works, it's just no longer wired up from the webapp —
    # kept rather than deleted in case a future format needs it again.
    #
    # The one exception: render/block_pipeline.py internally builds small
    # THROWAWAY synthetic EDLs with `clips` populated, purely to hand a
    # footage run off to a "pair" edit style's own assemble() — never
    # user-facing, never constructed by edl/format_001.py's
    # build_format_001_edl, and never what `clips` being non-empty means
    # anywhere else in this app.
    template: Literal["Papercut", "Rotator", "Splitter", "Shake", "Flash", "Zoom"]
    format: Format
    clips: list[ClipRef]
    text_cues: list[TextCue] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)  # Format 001+ only
    music: MusicCue | None = None
    logo: str = ""        # asset filename, optional
    business_name: str = ""
    text_style: Literal["boxed", "minimal", "punch"] = "boxed"  # "Make it cleaner" toggles "minimal"; "punch" = bold in-your-face type
    font: str = ""         # filename in assets/fonts/ for text_style="punch"; "" = render engine's own default (Anton)
    text_animation: Literal["capture", "pop"] = "capture"  # LEGACY — only read by render/pipeline.py's old render_punch_overlay() path, which webapp/app.py no longer calls. render/block_pipeline.py (the only active path) reads `style` below instead; kept so an old EDL/caller still round-trips.
    # Revision (Oct 25 2026): the style picker now drives a full per-style
    # MOTION preset (not just a font) — see render/motion_registry.py, the
    # single source of truth for style -> {font, preset class}. This is the
    # field render/block_pipeline.py actually reads to pick HOOK/USP motion;
    # `text_animation` above is no longer consulted by that path at all.
    style: Literal["punchy", "bold", "clean", "premium"] = "punchy"

    @property
    def duration(self) -> float:
        if self.blocks:
            return round(sum(b.duration for b in self.blocks), 3)
        return max((c.end for c in self.clips), default=0.0)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "EDL":
        return EDL(
            template=d["template"],
            format=d["format"],
            clips=[ClipRef(**c) for c in d["clips"]],
            text_cues=[TextCue(**t) for t in d.get("text_cues", [])],
            blocks=[Block(**b) for b in d.get("blocks", [])],
            music=MusicCue(**d["music"]) if d.get("music") else None,
            logo=d.get("logo", ""),
            business_name=d.get("business_name", ""),
            text_style=d.get("text_style", "boxed"),
            font=d.get("font", ""),
            text_animation=d.get("text_animation", "capture"),
            style=d.get("style", "punchy"),
        )
