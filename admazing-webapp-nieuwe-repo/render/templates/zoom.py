"""Zoom — a hard cut into a brief white flash (reusing the same fade-in /
hold / fade-out pulse as the Flash template), which clears to reveal the
incoming clip pulled back (zoomed OUT, wider framing than normal) and then
punching IN smoothly to its normal framing over about a second. A seventh
template: structurally different from both Shake (position jitter + motion
blur, no scale change) and Flash (Shake's jitter/blur under a flash) — Zoom
has no positional jitter or blur at all, only a scale change.

Built from a direct reference the client sent (`Zoom.mp4`, the same demo-cut
format as `Shake.mp4`/`Flash.mp4` — two placeholder test cards, 1080x1920,
24fps, 8s) — measured frame-by-frame, not eyeballed:

  - The OUTGOING clip is untouched right up to the cut, same as every other
    hard-cut template in this set.
  - The mean-brightness curve at the cut is effectively identical to
    `Flash.mp4`'s: a brief hold near pure white, fading to the incoming
    clip's true brightness over the next several frames. Same cut
    treatment, reused here via `flash._flash_weights` rather than
    re-derived — see the "why no separate flash tuning" note below.
  - Once the flash clears, the incoming clip is NOT at its normal framing:
    tracking the on-screen text's bounding-box WIDTH (a brightness-threshold
    mask, not a full OCR — plenty precise for "is it bigger or smaller than
    its own settled size") shows it start at roughly 80% of its final,
    settled width immediately after the flash clears, then grow smoothly
    and monotonically back to 100% over about 23 frames at the reference's
    24fps (~0.96s). Growing on-screen size means the CAMERA is doing the
    opposite — starting more zoomed OUT (wider FOV, showing more of the
    frame, hence the smaller-looking text) and zooming IN to settle at the
    normal framing. No overshoot was measured (the width never exceeds its
    final settled value before reaching it), so this is a pull-in-and-
    settle, not a punch-past-100%-and-spring-back.
  - Unlike Shake/Flash, there is no meaningful positional wobble here — the
    small (~35px) centroid drift measured is consistent with bounding-box
    measurement noise as the text's own size is changing, not a distinct
    camera-shake signal. Zoom is kept as a clean, single-parameter effect
    (scale only) rather than importing Shake's jitter on top for a "just in
    case" — if the client says it needs shake too, that is the next thing
    to add, not assumed up front.

Every template module in this package exposes the same signature:
    assemble(renderer, edl, resolution, ken_burns=False) -> Path

Why no separate flash tuning for Zoom: the measured brightness curve here
matches `Flash.mp4` closely enough that re-deriving a second set of
hold/fade numbers would just be guesswork dressed up as precision — and the
client's most recent Flash feedback ("maak de witte vlak meer fade in fade
out type") is a preference about how a white cut-flash should FEEL in this
project generally, not a one-off fix scoped to that single template. So
Zoom imports `_flash_weights` from `flash.py` directly and gets that
correction for free; if the client ever wants Zoom's flash to differ from
Flash's, split them then.

Known simplifications vs. the reference (being upfront):
  - `ZOOM_START` (1.25x) is set slightly above the ~1.23x implied by the
    cleanest measured frame (the true k=0 value is hidden under the flash,
    same limitation Flash.mp4's own analysis ran into) — a small margin on
    the side of "slightly more zoom" rather than guessing short.
  - The zoom-in curve uses the same decay-envelope shape (`DECAY_POWER`,
    imported from shake.py) as Shake's jitter envelope, not a separately
    fitted curve — both are a fast-initial / short-tail decay reaching
    exactly 1.0 (fully settled) at the last frame, and re-deriving a
    separate shape from a noisier, indirect (bounding-box-width) signal
    wasn't worth a second decay constant to maintain.
  - Bounding-box width from a brightness threshold is a proxy for "on-screen
    scale", not a calibrated measurement — good enough to establish
    direction (shrinks then grows) and rough magnitude, not sub-percent
    precision.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

from edl.schema import EDL, ClipRef
from render.ffmpeg_utils import run
from render.templates.flash import _flash_weights
from render.templates.shake import DECAY_POWER, FPS

ZOOM_DURATION = 0.9              # seconds of zoom-in settle — measured ~23 frames @ reference's 24fps (~0.96s)
ZOOM_FRAMES = round(ZOOM_DURATION * FPS)
ZOOM_START = 1.25                # crop-window scale at k=0 (most zoomed OUT) — measured ~1.23x from the cleanest post-flash frame, rounded up slightly since the true k=0 value is hidden under the flash
OVERSCAN = 1.27                  # decode margin — must exceed ZOOM_START with room to spare, or the zoomed-out crop would run off the decoded frame


def _decode_overscan(source: Path, duration: float, resolution: tuple[int, int]) -> np.ndarray:
    """Same recipe as shake.py's _decode_overscan, duplicated with Zoom's
    OWN `OVERSCAN` rather than imported — shake.py's version reads shake's
    module-level OVERSCAN (currently 1.24), which is too small a margin for
    Zoom's 1.25x starting crop. Importing the function but not the constant
    it silently depends on would have been a real bug, not a style choice."""
    w, h = resolution
    ow, oh = round(w * OVERSCAN), round(h * OVERSCAN)
    cmd = [
        "ffmpeg", "-y", "-i", str(source), "-t", f"{duration:.3f}",
        "-vf", f"scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},setsar=1,fps={FPS}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    frame_size = ow * oh * 3
    n = len(proc.stdout) // frame_size
    if n == 0:
        raise RuntimeError(
            f"decoding a zoom-burst window from {source} produced 0 frames:\n"
            f"{proc.stderr[-2000:].decode(errors='replace')}"
        )
    frames = np.frombuffer(proc.stdout[: n * frame_size], dtype=np.uint8).reshape(n, oh, ow, 3)
    return frames


def _zoom_envelope(n: int) -> np.ndarray:
    """Per-frame crop-window scale: `ZOOM_START` (most zoomed out) at k=0,
    decaying to exactly 1.0 (normal framing) at the last frame — same
    envelope shape as Shake's jitter (`DECAY_POWER`, forced to the extremes
    at both ends by construction, not by chance), just driving a scale
    factor instead of a 2D offset."""
    t = np.arange(n) / max(n - 1, 1)
    envelope = (1.0 - t) ** DECAY_POWER
    envelope[0] = 1.0
    envelope[-1] = 0.0
    return 1.0 + (ZOOM_START - 1.0) * envelope


def _encode_zoom_burst(
    renderer, frames: np.ndarray, zoom_scale: np.ndarray, flash_w: np.ndarray,
    resolution: tuple[int, int], index: int,
) -> Path:
    w, h = resolution
    n, oh, ow, _ = frames.shape
    ccx, ccy = ow / 2.0, oh / 2.0

    out_path = renderer.work_dir / f"zoom_burst_{index:02d}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS),
        "-i", "-",
        "-pix_fmt", "yuv420p",  # see shake.py's _encode_shake_burst comment — non-negotiable for the concat step below
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for k in range(n):
            s = float(zoom_scale[k])
            cw, ch = w * s, h * s
            x0 = max(0, int(round(ccx - cw / 2.0)))
            y0 = max(0, int(round(ccy - ch / 2.0)))
            x1 = min(ow, x0 + max(1, int(round(cw))))
            y1 = min(oh, y0 + max(1, int(round(ch))))
            crop = frames[k, y0:y1, x0:x1, :]
            if crop.shape[1] != w or crop.shape[0] != h:
                interp = cv2.INTER_AREA if s > 1.0 + 1e-9 else cv2.INTER_LINEAR
                resized = cv2.resize(crop, (w, h), interpolation=interp)
            else:
                resized = crop
            frame = resized.astype(np.float32)
            fw = float(flash_w[k])
            if fw > 0.0:
                frame = frame * (1.0 - fw) + 255.0 * fw
            proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed encoding zoom burst {index} (exit {proc.returncode})")
    return out_path


def _trim_rest(renderer, source: Path, start: float, dur: float, resolution: tuple[int, int], index: int) -> Path:
    """Same recipe as shake.py's _trim_rest, duplicated with zoom-prefixed
    filenames — see shake.py's _concat_two docstring for why templates
    never share a fixed output filename."""
    w, h = resolution
    out_path = renderer.work_dir / f"zoom_rest_{index:02d}.mp4"
    vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={FPS}"
    run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{dur:.3f}",
        "-vf", vf, "-an",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path),
    ])
    return out_path


def _concat_two(renderer, a: Path, b: Path, index: int) -> Path:
    """Own uniquely-named concat, never the shared Renderer._concat_clips
    path — see shake.py's _concat_two docstring for the exact bug this
    avoids."""
    list_file = renderer.work_dir / f"zoom_concat_list_{index:02d}.txt"
    list_file.write_text(f"file '{a.resolve()}'\nfile '{b.resolve()}'")
    out_path = renderer.work_dir / f"zoom_clip_{index:02d}.mp4"
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])
    return out_path


def _render_zoomed_clip(renderer, clip: ClipRef, resolution: tuple[int, int], index: int) -> Path:
    w, h = resolution
    src_path = renderer.assets_dir / clip.source
    if not src_path.exists():
        raise FileNotFoundError(f"EDL references clip '{clip.source}' but it is not in {renderer.assets_dir}")

    burst_dur = min(ZOOM_DURATION, clip.duration)
    frames = _decode_overscan(src_path, burst_dur, resolution)
    n = min(len(frames), ZOOM_FRAMES)

    zoom_scale = _zoom_envelope(n)
    flash_w = _flash_weights(n)

    burst_path = _encode_zoom_burst(renderer, frames[:n], zoom_scale, flash_w, resolution, index)

    body_dur = clip.duration - burst_dur
    if body_dur > 0.02:
        rest_path = _trim_rest(renderer, src_path, burst_dur, body_dur, resolution, index)
        return _concat_two(renderer, burst_path, rest_path, index)
    return burst_path


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    if len(edl.clips) == 1:
        return renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)

    # First clip is never zoomed — same rule as every other hard-cut
    # template here: the effect only ever happens on the clip being cut TO.
    parts: list[Path] = [
        renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)
    ]
    for i in range(1, len(edl.clips)):
        parts.append(_render_zoomed_clip(renderer, edl.clips[i], resolution, i))

    return renderer._concat_clips(parts)
