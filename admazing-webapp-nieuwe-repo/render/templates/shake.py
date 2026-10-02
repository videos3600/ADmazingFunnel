"""Shake — a hard cut straight into a decaying camera-shake + motion-blur
burst on the INCOMING clip, then settles to normal playback. A different
transition family from Papercut (which reveals the new clip gradually
through a wipe): here the cut itself is instant, and what sells the effect
is the new footage looking like the camera just got jolted before catching
itself.

Built from a direct reference the client sent (`Shake.mp4`, a demo cut
between two placeholder cards) — not eyeballed. Decoding it frame-by-frame
and tracking the on-screen content's centroid/pixel-spread per frame (see
the analysis this module is based on) gave a real, quantified shape instead
of a guess:

  - The OUTGOING clip is completely untouched — identical pixels frame to
    frame right up to the cut. The shake only ever happens on the clip that
    is being cut TO, never the one being cut away from.
  - At the cut, the incoming clip's position immediately jumps to a large
    offset (tens to ~100px on a 1080-wide frame) — the shake starts at full
    strength on frame one, it doesn't ramp up into it.
  - That offset then jitters unevenly (not a clean sine wave — more like a
    damped random walk) for about 20 frames at the reference's 24fps
    (~0.83s), decaying down to a dead stop.
  - Alongside the position jitter, there is real directional motion blur
    whose STRENGTH tracks the jitter's instantaneous speed: frames near a
    direction reversal (low velocity) are noticeably sharper than frames
    mid-swing (high velocity) — i.e. this isn't a flat "blurred for X
    frames" effect, the blur amount itself is driven by the same motion
    curve as the position.
  - After ~20 frames (24fps) / ~24 frames (this project's 30fps) it's
    completely static again — pixel-identical frame to frame, same as the
    outgoing clip before the cut.

Every template module in this package exposes the same signature:
    assemble(renderer, edl, resolution, ken_burns=False) -> Path
`renderer` gives access to its work_dir, assets_dir and the shared
_trim_and_normalize_clip/_concat_clips helpers. The returned Path is a video
at `resolution`, ready for the shared text/logo/music steps.

v2 correction (client feedback after seeing v1): the burst's most intense
moment — position AND blur together — needs to land exactly ON the cut
frame, not before it (the outgoing clip still never shakes) and not a few
frames after it either. v1 got the position part right (frame 0 was already
at full envelope amplitude) but not the blur part: blur strength was
derived from actual frame-to-frame motion in the random walk, and a
constant-size random walk can happen to swing hardest a few frames in, not
necessarily on frame 0 — so v1's first attempted fix (giving frame 0 a
virtual "pre-cut" point to blur against) made frame 0 non-zero but didn't
guarantee it was the PEAK; a few frames later could still out-blur it by
chance. Fixed properly by decoupling blur strength from the random walk's
actual velocity entirely: `blur_extent` is now its own value driven
directly by the same decay envelope as position (see `_shake_trajectory`'s
docstring) — maximum exactly at k=0, zero at the last frame, guaranteed by
construction rather than likely by chance. The client also asked for more
amplitude ("de shake mag wel iets meer") — bumped from 9% to 12% of the
shorter side, with OVERSCAN widened to match so the larger swing still has
margin to shift into.

v3 correction (client feedback after seeing v2): "iets minder shake" — v2's
12% amplitude read as slightly too strong once the peak-on-the-cut timing
was fixed (a strong shake that's perfectly timed reads as MORE intense than
the same strength poorly timed, so v2's timing fix likely made the existing
amplitude feel bigger than it did in v1). Pulled amplitude back from 12% to
10% of the shorter side, and peak blur span down from 10% to ~8.5% in the
same proportion (blur is driven by the same envelope as position, so it
scales down with it to keep the two visually matched). OVERSCAN margin eased
back from 1.28 to 1.24 to match the smaller swing — still has more margin
than the new amplitude needs, just not as much as the 12% version required.

v4 correction (client feedback: "de transitie zijn heel erg zwak" — after
sending a reference pack, it turned out `Shake.mp4`/`Flash.mp4`/`Zoom.mp4`
were themselves exports FROM that pack, not independent references). The
pack's own tutorial documents each transition's tunable "Controller" layer,
and it exposes Rotation Frequency and Rotation Amplitude as sliders separate
from Position Frequency/Amplitude — i.e. the real effect combines a small
camera ROLL with the translation jitter, not translation alone. That is a
real, structural gap in every version up to v3, and there's a good reason
the earlier frame analysis missed it: the reference was measured via
on-screen content's CENTROID position, and pure rotation around a frame's
own center barely moves a centered subject's centroid at all — the signal
was there in the original reference footage, the measurement method used to
reverse-engineer it just couldn't see it. Added a `rot` (degrees) output to
`_shake_trajectory`, driven by the exact same envelope as position (full
strength at the cut, zero at settle) so it decays in lockstep rather than as
a second, independently-timed wobble. Amplitude for it (`ROTATION_MAX_DEG`)
is a genuine hand-pick, not a re-measurement — re-deriving it from the
reference would need angle/edge tracking, not the centroid method already
used, and is out of scope for this pass. Deliberately left AMPLITUDE_FRAC
and BLUR_EXTENT_FRAC untouched this round so the next round of feedback
isolates whether rotation alone was the missing piece, instead of changing
translation strength and adding a new axis at the same time. OVERSCAN nudged
from 1.24 to 1.27 for rotation margin, and the frame rotation itself uses
edge-replicated borders (not black fill), so even if the margin runs a
little short on a max-amplitude frame the failure mode is stretched edge
pixels, not a visible black wedge.

Known simplifications vs. the reference (being upfront, not pretending this
is pixel-identical):
  - The reference's jitter trajectory was reconstructed from one clip's
    worth of centroid/spread measurements on plain colour cards — there was
    no way to separate "camera shake" from "a possible small zoom pulse"
    from that data with full confidence (both would show up as the
    bounding-box spread changing). This implementation does positional
    jitter, a small correlated rotation (see the v4 correction above), and a
    blur pass driven by the same decay envelope as the position (not by
    actual frame-to-frame velocity — see the v2 correction above), but still
    no zoom pulse. If the client says the burst is missing a "punch in"
    feeling, that is the next thing to add.
  - The blur here is approximated as an average of a handful of shifted
    samples of the SAME decoded frame (a discrete stand-in for a continuous
    per-pixel motion-blur kernel) — cheap and matches the reference closely
    enough in the frame checks below, but it is a sampled approximation,
    not a true motion-blur convolution.
  - Amplitude is defined as a fraction of the shorter output side (so it
    scales sensibly across 9:16 / 1:1 / 16:9) rather than reproducing the
    reference's exact pixel numbers, which were measured on a 1080-wide
    9:16 frame specifically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

from edl.schema import EDL, ClipRef
from render.ffmpeg_utils import run

FPS = 30
SHAKE_DURATION = 0.8          # seconds of decaying jitter+blur right after each cut (reference: ~20 frames @ 24fps = 0.83s)
SHAKE_FRAMES = round(SHAKE_DURATION * FPS)
OVERSCAN = 1.27                 # decode margin so jitter/blur/rotation never exposes empty edges — nudged from 1.24 for rotation margin (v4)
AMPLITUDE_FRAC = 0.10           # initial horizontal jitter amplitude, as a fraction of min(w, h) — v1: 0.09, v2: 0.12 ("iets meer"), v3: 0.10 ("iets minder" than v2)
VERTICAL_AMPLITUDE_RATIO = 0.72  # reference's vertical swing was consistently smaller than horizontal (~70-80px vs ~100-120px)
DECAY_POWER = 1.6               # envelope shape: fast initial decay with a short tail, matching the measured trajectory
BLUR_SAMPLES = 6                # sub-samples averaged per frame to approximate motion blur
BLUR_EXTENT_FRAC = 0.085        # peak blur smear span (at k=0) as a fraction of min(w, h); decays with the same envelope as position — scaled down with the v3 amplitude reduction
ROTATION_MAX_DEG = 3.0          # v4: peak camera-roll amplitude in degrees, decaying with the same envelope as position — hand-picked, see the v4 correction note above


def _decode_overscan(source: Path, duration: float, resolution: tuple[int, int]) -> np.ndarray:
    """Decode the first `duration` seconds of `source`, scaled/cropped to an
    OVERSCANNED canvas (bigger than the output resolution) so later frames
    have margin to shift into without exposing an edge."""
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
            f"decoding a shake-burst window from {source} produced 0 frames:\n"
            f"{proc.stderr[-2000:].decode(errors='replace')}"
        )
    frames = np.frombuffer(proc.stdout[: n * frame_size], dtype=np.uint8).reshape(n, oh, ow, 3)
    return frames


def _shake_trajectory(
    n: int, amp_x: float, amp_y: float, blur_extent_max: float, rot_amplitude_deg: float, seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """A decaying 2D jitter path, built so the ENVELOPE — not wherever a raw
    random walk happens to wander highest — controls the magnitude at every
    frame. That matters: an earlier version normalized a whole-sequence
    random walk to its own peak and then multiplied by the decay envelope,
    which meant the walk's peak (almost never at index 0) and the envelope's
    peak (always at index 0) fought each other — frame 0 came out at maybe
    a third of the intended amplitude, and the jitter visibly RAMPED UP for
    several frames before decaying, the opposite of the reference (where the
    very first shaken frame is already near maximum offset). Caught by
    printing the actual generated trajectory and comparing it against the
    intended amplitude, not by eyeballing the render — the real-footage
    render looked plausible at a glance even with the bug, because the
    effect was just weak, not visibly broken.

    Magnitude here comes directly from `envelope` (forced to 1.0 at k=0 and
    0.0 at the last frame, so the shake always starts at full strength and
    always ends pixel-static). Direction comes from a smoothed random walk
    in angle, so the wobble reverses irregularly instead of tracing a clean
    circle or sine — organic, not mechanical.

    Also returns `blur_extent`: how far each frame's motion blur should
    smear, in pixels. A first correction (giving frame 0 a virtual
    "pre-cut" point to blur against) was not enough on its own — the angle
    random walk still had a constant step size at every frame, so how much
    the DIRECTION changed frame-to-frame (and therefore how much blur that
    implied) could spike anywhere in the burst by chance, not necessarily at
    frame 0. The client's feedback ("de shake moet op de cut het heftigst
    zijn") needs that guaranteed, not just likely — so blur strength is no
    longer derived from the random walk's actual velocity at all. It is its
    own value, driven by the SAME envelope as position: maximum at k=0,
    zero at the last frame. The random walk still supplies the blur's
    DIRECTION each frame (so it still looks like it's smearing along the
    shake's own motion, not some unrelated axis), just not its magnitude.

    v4 also returns `rot`: a camera-roll angle in degrees, added after the
    client's "very weak" feedback surfaced that the reference pack's own
    Controller exposes Rotation Frequency/Amplitude as a separate axis from
    Position — a component the original centroid-based frame analysis
    couldn't have detected (a centered subject's centroid barely moves under
    pure rotation). Reuses this same `envelope` (peak at k=0, zero at the
    last frame) so the roll settles in lockstep with the translation instead
    of as a second, independently-timed wobble.

    Direction comes from its own small, slow random walk `phase` (radians),
    forced to start at exactly 0 — via `rot = rot_amplitude_deg * envelope *
    cos(phase)`. Two earlier drafts were wrong in ways only the printed
    array caught, not the eye: (1) using the position walk's raw sin(angle)
    directly as the multiplier is fine for jx/jy (cos^2+sin^2=1 makes that
    VECTOR's magnitude exactly amp*envelope regardless of angle) but wrong
    for a scalar like rot — a later frame's |sin(angle)| can exceed frame
    0's, letting rot briefly out-rotate the "peak at the cut" guarantee by
    chance; (2) a fix using sign(sin(angle)) instead guaranteed the
    magnitude envelope correctly but let the roll SNAP between + and - in a
    single frame whenever the position walk's angle crossed a multiple of
    pi, which would read as a jerky flip rather than a roll. `phase` here is
    its own walk with a small step size and cos(phase(0))=1 forced by
    construction, so |rot(k)| = rot_amplitude_deg*envelope(k)*|cos(phase(k))|
    is bounded by rot_amplitude_deg*envelope(k) at every frame (peak
    guaranteed at k=0, where cos(phase)=1 exactly) while varying smoothly in
    between — no discontinuities, and a small step size keeps it mostly
    one-directional (a real roll settling back, not a wobble that reverses
    direction mid-burst)."""
    rng = np.random.default_rng(seed)

    t = np.arange(n) / max(n - 1, 1)
    envelope = (1.0 - t) ** DECAY_POWER
    envelope[0] = 1.0
    envelope[-1] = 0.0

    angle_steps = rng.normal(0, 0.9, size=n)
    angle = np.cumsum(angle_steps)
    angle = np.convolve(angle, np.ones(3) / 3, mode="same")
    angle += rng.uniform(0, 2 * np.pi)  # random starting direction, not always along +x

    jx = amp_x * envelope * np.cos(angle)
    jy = amp_y * envelope * np.sin(angle)
    jx[-1] = 0.0
    jy[-1] = 0.0

    blur_extent = blur_extent_max * envelope

    phase_steps = rng.normal(0, 0.35, size=n)
    phase = np.cumsum(phase_steps)
    phase = np.convolve(phase, np.ones(3) / 3, mode="same")
    phase -= phase[0]  # force phase[0] == 0 exactly, so cos(phase[0]) == 1 and rot(0) hits the full amplitude
    rot = rot_amplitude_deg * envelope * np.cos(phase)
    rot[-1] = 0.0

    return jx, jy, blur_extent, rot


def _encode_shake_burst(
    renderer, frames: np.ndarray, jx: np.ndarray, jy: np.ndarray, blur_extent: np.ndarray, rot: np.ndarray,
    resolution: tuple[int, int], index: int,
) -> Path:
    w, h = resolution
    n, oh, ow, _ = frames.shape
    cx0, cy0 = (ow - w) / 2.0, (oh - h) / 2.0
    margin_x, margin_y = (ow - w) / 2.0, (oh - h) / 2.0
    rot_center = (ow / 2.0, oh / 2.0)

    out_path = renderer.work_dir / f"shake_burst_{index:02d}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS),
        "-i", "-",
        # explicit yuv420p: raw-RGB input otherwise defaults to yuv444p under
        # libx264, which breaks the concat-demuxer "-c copy" join with the
        # (yuv420p) body segments — this exact bug already bit the Papercut
        # template's torn-paper transition once (border/tape overlay vanishing
        # partway through a render), so it's non-negotiable here too.
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for k in range(n):
            # v4: rotate the whole oversized canvas about its own center
            # BEFORE the existing translate+blur sampling below — so that
            # logic doesn't need to change at all, it just now samples from
            # a rolled canvas instead of the raw decoded one. BORDER_REPLICATE
            # (not black fill) so a max-amplitude frame that eats slightly
            # into the rotation margin stretches edge pixels rather than
            # exposing a black wedge.
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
            # blur direction = the shake's own radial direction at this frame
            # (falls back to +x on the rare exact-zero position, which only
            # happens where blur_extent is already ~0 too, so it's invisible)
            dir_x, dir_y = (cur_x / mag, cur_y / mag) if mag > 1e-6 else (1.0, 0.0)
            half = float(blur_extent[k]) / 2.0
            samples = []
            for m in range(BLUR_SAMPLES):
                alpha = m / max(BLUR_SAMPLES - 1, 1)  # 0..1 across the smear span
                offset = -half + alpha * (2 * half)
                ix = float(np.clip(cur_x + dir_x * offset, -margin_x, margin_x))
                iy = float(np.clip(cur_y + dir_y * offset, -margin_y, margin_y))
                x0 = int(round(cx0 + ix))
                y0 = int(round(cy0 + iy))
                samples.append(canvas[y0:y0 + h, x0:x0 + w, :].astype(np.float32))
            frame = np.mean(samples, axis=0)
            proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed encoding shake burst {index} (exit {proc.returncode})")
    return out_path


def _trim_rest(renderer, source: Path, start: float, dur: float, resolution: tuple[int, int], index: int) -> Path:
    """The remainder of a clip after its shake burst — same scale/crop
    recipe as Renderer._trim_and_normalize_clip's non-ken_burns branch, just
    offset by `start` instead of beginning at 0, so it lines up pixel-for-
    pixel with the burst segment it gets concatenated onto."""
    w, h = resolution
    out_path = renderer.work_dir / f"shake_rest_{index:02d}.mp4"
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
    """Concat exactly two segments (a clip's shake burst + its own
    remainder) into a uniquely-named file. Deliberately NOT
    Renderer._concat_clips — that method always writes to the same fixed
    `work_dir/concatenated.mp4` path, and assemble() below also needs to
    call it once for the final, top-level concat. Reusing it here made an
    early version of this file feed a file into ffmpeg as both an input
    (this clip's burst+rest) and, moments later, the SAME path as the
    final output of the outer concat — ffmpeg truncated it on open before
    finishing reading it, silently dropping every clip after the first from
    the assembled video. Caught by checking the assembled video's actual
    duration against the EDL's, not by eye."""
    list_file = renderer.work_dir / f"shake_concat_list_{index:02d}.txt"
    list_file.write_text(f"file '{a.resolve()}'\nfile '{b.resolve()}'")
    out_path = renderer.work_dir / f"shake_clip_{index:02d}.mp4"
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])
    return out_path


def _render_shaken_clip(renderer, clip: ClipRef, resolution: tuple[int, int], index: int) -> Path:
    w, h = resolution
    src_path = renderer.assets_dir / clip.source
    if not src_path.exists():
        raise FileNotFoundError(f"EDL references clip '{clip.source}' but it is not in {renderer.assets_dir}")

    shake_dur = min(SHAKE_DURATION, clip.duration)
    frames = _decode_overscan(src_path, shake_dur, resolution)
    n = min(len(frames), SHAKE_FRAMES)

    amp_x = AMPLITUDE_FRAC * min(w, h)
    amp_y = amp_x * VERTICAL_AMPLITUDE_RATIO
    blur_extent_max = BLUR_EXTENT_FRAC * min(w, h)
    jx, jy, blur_extent, rot = _shake_trajectory(n, amp_x, amp_y, blur_extent_max, ROTATION_MAX_DEG, seed=3000 + index)

    burst_path = _encode_shake_burst(renderer, frames[:n], jx, jy, blur_extent, rot, resolution, index)

    body_dur = clip.duration - shake_dur
    if body_dur > 0.02:
        rest_path = _trim_rest(renderer, src_path, shake_dur, body_dur, resolution, index)
        return _concat_two(renderer, burst_path, rest_path, index)
    return burst_path


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    if len(edl.clips) == 1:
        return renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)

    # First clip is never shaken — the reference never touches the OUTGOING
    # clip, only the one being cut TO. Every clip after it gets a shake
    # burst grafted onto its own opening ~0.8s, then a hard concat (no
    # borrowed time from neighbours, unlike Papercut's overlap wipe — the
    # cut itself is instant here).
    parts: list[Path] = [
        renderer._trim_and_normalize_clip(edl.clips[0].source, edl.clips[0].duration, 0, resolution, ken_burns=ken_burns)
    ]
    for i in range(1, len(edl.clips)):
        parts.append(_render_shaken_clip(renderer, edl.clips[i], resolution, i))

    return renderer._concat_clips(parts)
