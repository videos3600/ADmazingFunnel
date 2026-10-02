"""
Fase-1 EDL generator for the "Papercut" template.

This is the simplest possible stand-in for the AI layer described in the
concept doc: no vision model, no LLM call — a fixed shot-order heuristic per
sector plus a fixed timing pattern. It exists so the render engine has a real
EDL to consume and so we can prove the upload -> EDL -> MP4 path end to end.

Fase 2 replaces `classify_clips()` with real clip classification (vision
model or the client's own tags) and `build_headline()` with an LLM call —
neither of those changes what the render engine receives: still just an EDL.
"""

from __future__ import annotations

from .schema import EDL, ClipRef, TextCue, MusicCue

# Fixed shot order per sector, per the concept doc's examples.
# Fase 2: make this sector-specific and data-driven (technisch bouwplan, fase 2).
SECTOR_SHOT_ORDER = {
    "restaurant": ["exterior", "owner", "food-closeup", "customer"],
    "contractor": ["problem", "work", "result", "logo"],
}

# Fixed per-shot durations (seconds) for the Papercut template, restaurant sector —
# mirrors the worked example in the concept doc exactly.
DEFAULT_DURATIONS = {
    "exterior": 1.4,
    "owner": 1.6,
    "food-closeup": 1.2,
    "customer": 1.8,
}


def classify_clips(tagged_sources: dict[str, str]) -> dict[str, str]:
    """Fase-1 stand-in for vision classification.

    `tagged_sources` is {tag: filename} supplied directly by the caller
    (in the MVP funnel: by the client tagging their own upload, or by
    filename heuristics). Fase 2 replaces this with a real classifier and
    keeps the same return shape, so nothing downstream changes.
    """
    return dict(tagged_sources)


def build_edl(
    *,
    sector: str,
    business_name: str,
    headline: str,
    cta: str,
    tagged_sources: dict[str, str],
    music_style: str = "upbeat tropical / urban",
    music_bpm: int = 105,
    music_file: str = "",
    logo: str = "",
    output_format: str = "9:16",
) -> EDL:
    shot_order = SECTOR_SHOT_ORDER.get(sector, list(tagged_sources.keys()))
    clips_by_tag = classify_clips(tagged_sources)

    clips: list[ClipRef] = []
    t = 0.0
    for tag in shot_order:
        if tag not in clips_by_tag:
            continue
        duration = DEFAULT_DURATIONS.get(tag, 1.5)
        clip_start, clip_end = t, round(t + duration, 3)
        clips.append(ClipRef(start=clip_start, end=clip_end, source=clips_by_tag[tag], tag=tag))
        t = clip_end

    total_duration = t

    text_cues = [
        TextCue(content=headline, start=0.6, end=total_duration - 0.4, kind="headline"),
        TextCue(content=cta, start=max(total_duration - 1.6, 0.0), end=total_duration, kind="cta"),
    ]

    return EDL(
        template="Papercut",
        format=output_format,  # type: ignore[arg-type]
        clips=clips,
        text_cues=text_cues,
        music=MusicCue(style=music_style, bpm=music_bpm, file=music_file),
        logo=logo,
        business_name=business_name,
    )
