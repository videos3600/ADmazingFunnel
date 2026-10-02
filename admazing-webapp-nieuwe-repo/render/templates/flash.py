"""Flash — a hard cut into a brief full-white flash pulse, which then clears
to reveal the same decaying camera-shake + motion-blur burst used by the
Shake template. A sixth template, not a Shake variant: the EDL author picks
"Flash" when the punch of a white flash-cut fits better than a plain shake.

Built from a direct reference the client sent (`Flash.mp4`, same demo-cut
format as `Shake.mp4` — two placeholder test cards, 1080x1920, 24fps, 8s) —
measured frame-by-frame, not eyeballed:

  - The OUTGOING clip is untouched right up to the cut, exactly like Shake.
  - At the cut, the frame goes to PURE white (255,255,255, every channel,
    not just a bright tint) and HOLDS there for a few frames — measured 4
    frames at the reference's 24fps (~0.167s). This is a real flash-frame,
    not a quick fade.
  - The white then clears back to the incoming clip's true content over
    roughly 2 more frames (~0.083s) — fast, not a slow dissolve.
  - Once the white has cleared, the revealed content is NOT simply static:
    tracking the incoming clip's on-screen content (centroid position via a
    brightness threshold, and sharpness via Laplacian variance) through the
    frames right after the flash clears shows the exact same signature as
    the standalone Shake reference — position swinging within a shrinking
    envelope, sharpness rising from blurred to fully sharp — settling to
    completely static by about 20 frames after the cut (24fps), the same
    order of magnitude as Shake's own ~20-frame settle. In other words: the
    reference for "Flash" is not flash-only, it is a flash pulse laid over
    the FRONT of the same shake burst — the shake is simply invisible for
    the few frames it's hidden under solid white, then revealed mid-decay
    once the flash clears.

Because of that, this module does not reimplement the jitter/blur physics —
it imports `_decode_overscan` and `_shake_trajectory` from `shake.py`
(pure, file-free functions; safe to share) and reuses them unchanged, then
adds one more compositing step on top: blending each burst frame toward
white according to a flash envelope for the first few frames. The encode,
trim-rest and concat helpers ARE duplicated here rather than imported,
deliberately — see the `_concat_two` docstring in shake.py for the exact
bug (a shared fixed output filename silently truncating a later concat)
that duplicating small, uniquely-named helpers per template avoids.

v3 correction (client feedback: "de transitie zijn heel erg zwak", after it
turned out `Flash.mp4` was itself an export from the reference transitions
pack, not an independent reference — see shake.py's v4 correction for the
full story). `_shake_trajectory` now also returns a small camera-roll `rot`
per frame, because the pack's own Controller exposes Rotation as a separate
axis from Position, which the original centroid-based analysis of
`Flash.mp4` couldn't have detected. Since Flash already imports and reuses
`_shake_trajectory` unchanged, it gets this rotation "for free" — the only
change needed here is threading the extra `rot` return value through to a
matching per-frame canvas rotation in `_encode_flash_burst`, mirroring
shake.py's `_encode_shake_burst` exactly.

Every template module in this package exposes the same signature:
    assemble(renderer, edl, resolution, ken_burns=False) -> Path

v2 correction (client feedback after seeing v1): "maak de witte vlak meer
fade in fade out type" — v1 followed the reference literally, which jumps
straight to full white on the very first frame of the burst (no ease-in)
and only fades on the way out. The client wants the white pulse itself to
read as a soft fade in both directions, not an instant pop followed by a
fade. `_flash_weights` was rewritten from a hold-then-linear-fade shape to
a smoothstep-eased fade-in -> hold -> fade-out trapezoid (`_ease`, zero
slope at both ends of each ramp so it reads as a fade, not a linear ramp
with a visible kink). This is a deliberate deviation from what was actually
measured in `Flash.mp4` (which has no fade-in) in favour of what the client
asked for once they saw it rendered — noted here so a future reader isn't
confused about why the code no longer matches the reference-analysis bullets
above on this one point.

Known simplifications vs. the reference (being upfront):
  - The jitter/blur amplitude constants are imported directly from
    `shake.py` (currently 10% amplitude / 8.5% peak blur span, after two
    rounds of client-driven correction there) rather than re-measured
    independently from `Flash.mp4` — the two references show swings of the
    same order of magnitude, and re-deriving a second, separately-tuned set
    of constants for what looks like the same underlying effect would just
    be two numbers to keep in sync instead of one. If the client says
    Flash's shake should be stronger or weaker than Shake's on its own,
    that is the point to split them.
  - The white pulse's fade-in/hold/fade-out durations (0.1s / 0.067s / 0.1s,
    after the v2 correction above) are a hand-picked shape, not measured —
    v1's durations were read off the reference's mean-brightness curve, but
    v2 intentionally departs from that curve's hard jump-to-white per client
    feedback, so there is no reference number left to anchor the new shape
    to; total pulse length was kept close to v1's for continuity.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

from edl.schema import EDL, ClipRef
from render.ffmpeg_utils import run
from render.templates.shake import (
    AMPLITUDE_FRAC,
    BLUR_EXTENT_FRAC,
    BLUR_SAMPLES,
    FPS,
    ROTATION_MAX_DEG,
    SHAKE_DURATION,
    SHAKE_FRAMES,
    VERTICAL_AMPLITUDE_RATIO,
    _decode_overscan,
    _shake_trajectory,
)

FLASH_FADE_IN = 0.100            # seconds easing UP from the outgoing clip's last frame to pure white
FLASH_HOLD = 0.067               # seconds held at pure white between the two eases
FLASH_FADE_OUT = 0.100           # seconds easing back DOWN from white to the incoming clip
FLASH_FADE_IN_FRAMES = round(FLASH_FADE_IN * FPS)
FLASH_HOLD_FRAMES = round(FLASH_HOLD * FPS)
FLASH_FADE_OUT_FRAMES = round(FLASH_FADE_OUT * FPS)


def _ease(u: np.ndarray) -> np.ndarray:
    """Smoothstep (3u^2 - 2u^3): eases in and out with zero slope at both
    ends, so the ramp up/down to white reads as a soft fade rather than a
    linear ramp with a visible kink where it meets the hold."""
    u = np.clip(u, 0.0, 1.0)
    return 3.0 * u ** 2 - 2.0 * u ** 3


def _flash_weights(n: int) -> np.ndarray:
    """Per-frame white-blend weight, now a proper fade-in / hold / fade-out
    pulse rather than an instant jump to white: eases 0.0 -> 1.0 over the
    fade-in window, holds at 1.0 briefly, then eases 1.0 -> 0.0 over the
    fade-out window before the (now-visible) shake burst continues
    underneath. Client feedback after v1 ("maak de witte vlak meer fade in
    fade out type") — v1 cut straight to full white on frame 0 (matching
    what was actually measured in the reference), which read as an abrupt
    pop rather than a flash; this trades a little reference-fidelity for a
    softer, more deliberate pulse. Built so it is correct by construction
    for any n (including a burst shorter than the full pulse, on a very
    short clip) rather than needing a separate short-clip branch."""
    k = np.arange(n, dtype=np.float64)
    rise = _ease(k / max(FLASH_FADE_IN_FRAMES, 1))
    fall_start = FLASH_FADE_IN_FRAMES + FLASH_HOLD_FRAMES
    fall = 1.0 - _ease((k - fall_start) / max(FLASH_FADE_OUT_FRAMES, 1))
    w = np.minimum(rise, fall)
    return np.clip(w, 0.0, 1.0)


def _encode_flash_burst(
    renderer, frames: np.ndarray, jx: np.ndarray, jy: np.ndarray, blur_extent: np.ndarray, rot: np.ndarray,
    flash_w: np.ndarray, resolution: tuple[int, int], index: int,
) -> Path:
    w, h = resolution
    n, oh, ow, _ = frames.shape
    cx0, cy0 = (ow - w) / 2.0, (oh - h) / 2.0
    margin_x, margin_y = (ow - w) / 2.0, (oh - h) / 2.0
    rot_center = (ow / 2.0, oh / 2.0)

    out_path = renderer.work_dir / f"flash_burst_{index:02d}.mp4"
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
            # v3: same whole-canvas rotation as shake.py's _encode_shake_burst
            # before the existing translate+blur sampling — see that
            # function's comment for why BORDER_REPLICATE, not black fill.
            deg = float(rot[k])
            if abs(deg) > 1e-6:
                rot_mat = cv2.getRotationMatrix2D(rot_center, deg, 1.0)
                canvas = cv2.warpAffine(
                    frames[k], rot_mat, (ow, oh),
                    flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
                )
            else:
                canvas = frames[k]

            cur_x, cur_y = float(jx[k]), float(jy[k])
            mag = (cur_x ** 2 + cur_y ** 2) ** 0.5
            dir_x, dir_y = (cur_x / mag, cur_y / mag) if mag > 1e-6 else (1.0, 0.0)
            half = float(blur_extent[k]) / 2.0
            samples = []
            for m in range(BLUR_SAMPLES):
                alpha = m / max(BLUR_SAMPLES - 1, 1)
                offset = -half + alpha * (2 * half)
                ix = float(np.clip(cur_x + dir_x * offset, -margin_x, margin_x))
                iy = float(np.clip(cur_y + dir_y * offset, -margin_y, margin_y))
                x0 = int(round(cx0 + ix))
                y0 = int(round(cy0 + iy))
                samples.append(canvas[y0:y0 + h, x0:x0 + w, :].astype(np.float32))
            frame = np.mean(samples, axis=0)
            fw = float(flash_w[k])
            if fw > 0.0:
                frame = frame * (1.0 - fw) + 255.0 * fw
            proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed encoding flash burst {index} (exit {proc.returncode})")
    return out_path


def _trim_rest(renderer, source: Path, start: float, dur: float, resolution: tuple[int, int], index: int) -> Path:
    """Same recipe as shake.py's _trim_rest, duplicated with flash-prefixed
    filenames rather than imported — see the module docstring above."""
    w, h = resolution
    out_path = renderer.work_dir / f"flash_rest_{index:02d}.mp4"
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
    """Same recipe as shake.py's _concat_two (own uniquely-named output,
    never the shared Renderer._concat_clips path) — duplicated, not
    imported, so a filename collision between templates isn't even
    possible to introduce by accident."""
    list_file = renderer.work_dir / f"flash_concat_list_{index:02d}.txt"
    list_file.write_text(f"file '{a.resolve()}'\nfile '{b.resolve()}'")
    out_path = renderer.work_dir / f"flash_clip_{index:02d}.mp4"
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])
    return out_path


def _render_flashed_clip(renderer, clip: ClipRef, resolution: tuple[int, int], index: int) -> Path:
    w, h = resolution
    src_path = renderer.assets_dir / clip.source
    if not src_path.exists():
        raise FileNotFoundError(f"EDL references clip '{clip.source}' but it is not in {renderer.assets_dir}")

    burst_dur = min(SHAKE_DURATION, clip.duration)
    frames = _decode_overscan(src_path, burst_dur, resolution)
    n = min(len(frames), SHAKE_FRAMES)

    amp_x = AMPLITUDE_FRAC * min(w, h)
    amp_y = amp_x * VERTICAL_AMPLITUDE_RATIO
    blur_extent_max = BLUR_EXTENT_FRAC * min(w, h)
    jx, jy, blur_extent, rot = _shake_trajectory(n, amp_x, amp_y, blur_extent_max, ROTATION_MAX_DEG, seed=4000 + index)
    flash_w = _flash_weights(n)

    burst_path = _encode_flash_burst(renderer, frames[:n], jx, jy, blur_extent, rot, flash_w, resolution, index)

    body_dur = clip.duration - burst_dur
    if body_dur > 0.02:
        rest_path = _trim_rest(renderer, src_path, burst_dur, body_dur, resolution, index)
        return _concat_two(renderer, burst_path, rest_path, index)
    return burst_path


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    if len(edl.clips) == 1:
        return renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)

    # First clip is never flashed — same rule as Shake: the effect only ever
    # happens on the clip being cut TO.
    parts: list[Path] = [
        renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)
    ]
    for i in range(1, len(edl.clips)):
        parts.append(_render_flashed_clip(renderer, edl.clips[i], resolution, i))

    return renderer._concat_clips(parts)
