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


BlockRole = Literal["HOOK", "HERO", "BRAND", "EXPERIENCE", "USP", "DESIRE", "CTA"]
BlockType = Literal["footage", "tekstkaart", "logo", "overlay"]


@dataclass
class Block:
    """One slot in a format's fixed timeline (Format 001:
    HOOK -> HERO -> EXPERIENCE -> USP -> DESIRE -> CTA -> BRAND — see
    edl/format_001.py). BRAND appears TWICE in the finished video — once as
    a badge overlaid on the back portion of HERO's footage (`logo_overlay`
    below), once as its own closing card — but only the closing one is a
    block of its own; measured from John's reference edit (Jonny's Burgers,
    Oct 2026), where the early brand moment is a logo stamped onto the
    product shot, not a separate full-screen card.

    Unlike ClipRef (a position on an already-assembled timeline), a Block IS
    the timeline: render/block_pipeline.py renders each one as its own
    normalized segment and concatenates them in order, so `duration` here is
    that block's own length, not a start/end pair on a shared clock.

    `content` means different things per `type`, same resolve-by-filename
    pattern as edl.font/edl.logo elsewhere in this schema:
      - "footage": the clip filename (must exist in assets_dir)
      - "tekstkaart": the text to render (e.g. the HOOK line or USP line)
      - "logo": text shown alongside edl.logo (e.g. the business name) —
        content may be blank if edl.logo alone is enough
      - "overlay": the CTA line, stamped with the same badge style the
        punch-text CTA already uses elsewhere in this app
    """
    role: BlockRole
    type: BlockType
    duration: float
    content: str = ""
    logo_overlay: bool = False  # "footage" blocks only — stamps edl.logo (or business_name) as a badge over this block's back portion


@dataclass
class EDL:
    # "Format001" is a block-based EDL (see `blocks` below), rendered by
    # render/block_pipeline.py — not render/pipeline.py's TEMPLATES registry.
    # `clips`/`text_cues` stay empty for it; `blocks` stays empty for every
    # other template.
    template: Literal["Papercut", "Rotator", "Splitter", "Shake", "Flash", "Zoom", "Format001"]
    format: Format
    clips: list[ClipRef]
    text_cues: list[TextCue] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)  # Format 001+ only
    music: MusicCue | None = None
    logo: str = ""        # asset filename, optional
    business_name: str = ""
    text_style: Literal["boxed", "minimal", "punch"] = "boxed"  # "Make it cleaner" toggles "minimal"; "punch" = bold in-your-face type
    font: str = ""         # filename in assets/fonts/ for text_style="punch"; "" = render engine's own default (Anton)
    text_animation: Literal["capture", "pop"] = "capture"  # text_style="punch" only — "capture" = per-letter cascade/glitch (render/text_fx.py's CapturePreset), "pop" = calmer per-word scale-bounce

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
        )
