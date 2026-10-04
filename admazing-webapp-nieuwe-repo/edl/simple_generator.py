"""
Generic EDL builder for the standalone upload-funnel test tool (webapp/).

Unlike edl/generator.py's build_edl (which assumes a fixed sector shot-order
of pre-tagged clips, e.g. restaurant: exterior -> owner -> food-closeup ->
customer), this builder makes no assumption about what the uploaded clips
ARE — it exists to let a real prospect drop in whatever footage they have,
in whatever order they uploaded it, and get a real EDL out. Fase 2's real
classification step is what's meant to replace the "clips play in upload
order" assumption here with real shot-selection — this function's job is
only to prove upload -> EDL -> MP4 works for arbitrary client footage, not
to be smart about shot order.
"""

from __future__ import annotations

from .schema import EDL, ClipRef, TextCue, MusicCue

DEFAULT_TOTAL_DURATION = 9.0  # seconds — a short social-ad length regardless of clip count
MIN_CLIP_DURATION = 1.0
MAX_CLIP_DURATION = 3.0


def build_simple_edl(
    *,
    business_name: str,
    headline: str,
    cta: str,
    clip_filenames: list[str],
    template: str,
    output_format: str = "9:16",
    text_style: str = "punch",
    music_style: str = "upbeat tropical / urban",
    music_bpm: int = 105,
    music_file: str = "",
    logo: str = "",
    font_file: str = "",
    target_total_duration: float = DEFAULT_TOTAL_DURATION,
) -> EDL:
    if not clip_filenames:
        raise ValueError("Need at least one uploaded clip to build an ad")

    # Spread the target total duration evenly across however many clips came
    # in, clamped to a sane single-shot range — a 3-clip upload gets ~3s
    # shots, an 8-clip upload gets shorter ~1.1s shots, rather than every ad
    # coming out at a fixed length regardless of how much footage was given.
    per_clip = target_total_duration / len(clip_filenames)
    per_clip = max(MIN_CLIP_DURATION, min(MAX_CLIP_DURATION, per_clip))

    clips: list[ClipRef] = []
    t = 0.0
    for i, filename in enumerate(clip_filenames):
        clip_end = round(t + per_clip, 3)
        clips.append(ClipRef(start=t, end=clip_end, source=filename, tag=f"clip_{i}"))
        t = clip_end

    total_duration = t

    text_cues = [
        TextCue(content=headline, start=0.4, end=max(total_duration - 1.4, 0.4), kind="headline"),
        TextCue(content=cta, start=max(total_duration - 1.6, 0.0), end=total_duration, kind="cta"),
    ]

    return EDL(
        template=template,  # type: ignore[arg-type]
        format=output_format,  # type: ignore[arg-type]
        clips=clips,
        text_cues=text_cues,
        music=MusicCue(style=music_style, bpm=music_bpm, file=music_file) if music_file else None,
        logo=logo,
        business_name=business_name,
        text_style=text_style,  # type: ignore[arg-type]
        font=font_file,
    )
