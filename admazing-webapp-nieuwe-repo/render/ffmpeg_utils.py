"""Shared FFmpeg helpers used by both the core renderer and the template
modules (render/templates/). Split out so templates don't have to import
from render.pipeline — pipeline.py imports the template registry, so a
template importing back from pipeline.py would be circular."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg command failed:\n"
            + " ".join(cmd)
            + "\n\nstderr:\n"
            + result.stderr[-4000:]
        )


def escape_drawtext(text: str) -> str:
    # ffmpeg drawtext needs these escaped inside the filter graph string
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")  # sidestep quoting entirely, keep it readable
    )


def probe_resolution(path: Path) -> tuple[int, int]:
    """(width, height) of a video's first stream — used by the webapp to
    decide whether an uploaded phone clip needs downscaling before it hits
    the render pipeline (see webapp/app.py's _downscale_video_if_needed)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    w_str, h_str = result.stdout.strip().split("x")
    return int(w_str), int(h_str)


def probe_duration(path: Path) -> float:
    """Real duration of a rendered file. Templates whose assembly changes the
    total runtime (Rotator's crossfades eat time; Splitter's paired clips
    run two clips in the time of one) mean edl.duration is only the EDL
    author's estimate, not the truth — the rest of the pipeline (music
    fade-out, the punch text overlay's frame count) needs the real number."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())
