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


@dataclass
class EDL:
    template: Literal["Papercut", "Rotator", "Splitter", "Shake", "Flash", "Zoom"]
    format: Format
    clips: list[ClipRef]
    text_cues: list[TextCue] = field(default_factory=list)
    music: MusicCue | None = None
    logo: str = ""        # asset filename, optional
    business_name: str = ""
    text_style: Literal["boxed", "minimal", "punch"] = "boxed"  # "Make it cleaner" toggles "minimal"; "punch" = bold in-your-face type

    @property
    def duration(self) -> float:
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
            music=MusicCue(**d["music"]) if d.get("music") else None,
            logo=d.get("logo", ""),
            business_name=d.get("business_name", ""),
            text_style=d.get("text_style", "boxed"),
        )
