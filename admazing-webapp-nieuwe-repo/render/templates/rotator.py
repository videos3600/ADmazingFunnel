"""Rotator — clips crossfade into each other with a radial wipe instead of
a hard cut (ffmpeg's xfade filter, transition=radial). Genuinely different
motion from Papercut, not a reskin: consecutive clips overlap in time.

That overlap means the assembled video is SHORTER than the naive sum of
clip durations (each transition "spends" its duration from both
neighbouring clips at once) — exactly how a crossfade works in any real
NLE. render() accounts for this by probing the actual assembled duration
and driving music/text-overlay timing off that instead of edl.duration.
"""

from __future__ import annotations

from pathlib import Path

from edl.schema import EDL
from render.ffmpeg_utils import run

TRANSITION = "radial"
MAX_OVERLAP = 0.4  # seconds


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    segments = [
        renderer._trim_and_normalize_clip(clip.source, clip.duration, i, resolution, ken_burns=ken_burns)
        for i, clip in enumerate(edl.clips)
    ]
    if len(segments) == 1:
        return segments[0]

    durations = [clip.duration for clip in edl.clips]
    # Keep the overlap sane for short clips — never eat more than 40% of the
    # shortest neighbour, so a 1s clip doesn't vanish into its own transition.
    overlap = min(MAX_OVERLAP, min(durations) * 0.4)

    inputs: list[str] = []
    for seg in segments:
        inputs += ["-i", str(seg)]

    filter_parts: list[str] = []
    running_duration = durations[0]
    prev_label = "0:v"
    for i in range(1, len(segments)):
        offset = running_duration - overlap
        out_label = f"v{i}" if i < len(segments) - 1 else "vout"
        filter_parts.append(
            f"[{prev_label}][{i}:v]xfade=transition={TRANSITION}:duration={overlap:.3f}:offset={offset:.3f}[{out_label}]"
        )
        running_duration = running_duration + durations[i] - overlap
        prev_label = out_label

    out_path = renderer.work_dir / "rotator_assembled.mp4"
    run([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", f"[{prev_label}]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path),
    ])
    return out_path
