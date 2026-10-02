"""Splitter — clips play in simultaneous top/bottom panels instead of one
after another (two angles of the same moment, before/after, etc.), stacked
with ffmpeg's vstack. Structurally the most different template from
Papercut: two source clips occupy the SAME span of output time instead of
each getting their own, so (like Rotator's crossfades) the assembled
duration isn't the naive sum of clip durations — see render/pipeline.py's
use of ffmpeg_utils.probe_duration for how the rest of the pipeline copes.

Clips are paired in order (0+1, 2+3, ...); an odd clip left over at the end
plays full-frame, solo, for its own duration.
"""

from __future__ import annotations

from pathlib import Path

from edl.schema import EDL
from render.ffmpeg_utils import run


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    w, h = resolution
    half_h = h // 2
    clips = edl.clips

    parts: list[Path] = []
    idx = 0
    i = 0
    while i < len(clips):
        if i + 1 < len(clips):
            top_clip, bottom_clip = clips[i], clips[i + 1]
            pair_duration = top_clip.duration  # the pair is on screen only as long as the first half has footage for
            top = renderer._trim_and_normalize_clip(top_clip.source, pair_duration, idx, (w, half_h))
            idx += 1
            bottom = renderer._trim_and_normalize_clip(bottom_clip.source, pair_duration, idx, (w, half_h))
            idx += 1

            out_path = renderer.work_dir / f"split_pair_{i:02d}.mp4"
            run([
                "ffmpeg", "-y",
                "-i", str(top), "-i", str(bottom),
                "-filter_complex", "[0:v][1:v]vstack=inputs=2[out]",
                "-map", "[out]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(out_path),
            ])
            parts.append(out_path)
            i += 2
        else:
            solo_clip = clips[i]
            solo = renderer._trim_and_normalize_clip(solo_clip.source, solo_clip.duration, idx, (w, h), ken_burns=ken_burns)
            idx += 1
            parts.append(solo)
            i += 1

    return renderer._concat_clips(parts)
