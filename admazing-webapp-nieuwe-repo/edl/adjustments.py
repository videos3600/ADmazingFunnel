"""
The four quick-adjust actions from the Result screen ("More energetic",
"Make it cleaner", "Change music", "Rewrite text" — NL: "Meer energie",
"Rustiger/cleaner", "Andere muziek", "Tekst zakelijker").

Each function takes the current EDL and returns a NEW EDL — never mutates
in place, so the caller can always fall back to the version before the
adjustment (needed once there's a real UI with an undo). This is the exact
mapping the Result screen's buttons will call once the funnel/website
exists (technisch bouwplan, fase 1 "nog niet gebouwd"): one button = one of
these functions, followed by a re-render through render/pipeline.py. No UI
work changes what these functions do — only what calls them.
"""

from __future__ import annotations

import copy

from .schema import EDL, ClipRef, TextCue, MusicCue

# Alternate music beds available to cycle through with "Change music".
# Fase 1: a couple of generated placeholders. Fase-1-productie: a real
# licensed library, selected by (style, bpm) instead of a fixed list.
MUSIC_LIBRARY = [
    MusicCue(style="upbeat tropical / urban", bpm=105, file="music_placeholder.m4a"),
    MusicCue(style="warmer / lo-fi", bpm=92, file="music_placeholder_alt.m4a"),
]

# Business-tone rewrites for known headline/CTA pairs. Fase 2 replaces this
# lookup with a real LLM call (technisch bouwplan, fase 2: AI-copywriter) —
# the function signature below does not change, only its body.
BUSINESS_TONE_REWRITES: dict[str, dict[str, str]] = {
    "Surinaamse smaken in hartje Almere": {
        "headline": "Surinaamse gerechten, vers bereid in Almere.",
        "cta": "Bestel nu online",
    },
}


def more_energetic(edl: EDL, factor: float = 0.72) -> EDL:
    """Faster cuts: shorten every clip by `factor`, rescale the text cues
    to the new (shorter) total duration so they stay proportionally timed."""
    new = copy.deepcopy(edl)
    old_duration = edl.duration or 1.0

    new_clips: list[ClipRef] = []
    t = 0.0
    for clip in edl.clips:
        new_dur = round(clip.duration * factor, 3)
        new_clips.append(ClipRef(start=round(t, 3), end=round(t + new_dur, 3), source=clip.source, tag=clip.tag))
        t += new_dur
    new.clips = new_clips
    new_duration = t

    scale = new_duration / old_duration
    new.text_cues = [
        TextCue(
            content=cue.content,
            start=round(cue.start * scale, 3),
            end=round(min(cue.end * scale, new_duration), 3),
            kind=cue.kind,
        )
        for cue in edl.text_cues
    ]
    return new


def make_cleaner(edl: EDL) -> EDL:
    """Strip the text-background band — type only, drop shadow instead of a box."""
    new = copy.deepcopy(edl)
    new.text_style = "minimal"
    return new


def change_music(edl: EDL) -> EDL:
    """Cycle to the next bed in MUSIC_LIBRARY (wraps around)."""
    new = copy.deepcopy(edl)
    current_file = edl.music.file if edl.music else None
    files = [m.file for m in MUSIC_LIBRARY]
    next_index = (files.index(current_file) + 1) % len(MUSIC_LIBRARY) if current_file in files else 0
    new.music = copy.deepcopy(MUSIC_LIBRARY[next_index])
    return new


def rewrite_text(edl: EDL, tone: str = "business") -> EDL:
    """Swap the headline/CTA for a more `tone` version.

    Fase 1: looked up from BUSINESS_TONE_REWRITES (only knows the worked
    example's copy). Fase 2: this becomes a real LLM call with the same
    (edl, tone) -> EDL contract, so nothing calling it has to change.
    """
    new = copy.deepcopy(edl)
    headline_cue = next((c for c in edl.text_cues if c.kind == "headline"), None)
    rewrite = BUSINESS_TONE_REWRITES.get(headline_cue.content) if headline_cue else None
    if not rewrite:
        return new  # no known rewrite for this copy yet — no-op rather than guessing

    new.text_cues = [
        TextCue(
            content=rewrite["headline"] if cue.kind == "headline" else rewrite["cta"],
            start=cue.start,
            end=cue.end,
            kind=cue.kind,
        )
        for cue in edl.text_cues
    ]
    return new


ADJUSTMENTS = {
    "more_energetic": more_energetic,
    "make_cleaner": make_cleaner,
    "change_music": change_music,
    "rewrite_text": rewrite_text,
}
