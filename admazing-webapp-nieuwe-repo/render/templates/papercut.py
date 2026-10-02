"""Papercut — a torn-paper SCRAPBOOK look, not just a torn-paper transition.

Fourth pass at this file, and the biggest correction so far. Passes 1-3
were all guesses about what "Papercut" means, refined each time the client
pushed back — a hard cut, then a flash/zoom-punch, then a torn-edge wipe
between cuts (which was closer, but still only a brief moment at each cut).
The client then sent still crops from the reference, zoomed in — and those
make clear "Papercut" isn't primarily a transition at all. It's a FRAME
TREATMENT applied throughout: the shot itself is a torn-paper cutout (a
jagged white edge running around the whole frame, with a hint of other
photos peeking through at the margins), held down with a strip of masking
tape, with a couple of thin "stress fracture" crack lines running across
the image, film grain over everything, and a muted, slightly faded color
grade. That look is what's on screen for the whole ad, not just at cuts.

So this file now does two things:
  1. The torn-edge WIPE between clips (kept from the previous version —
     the client hasn't said that part is wrong, and the reference video
     does cut between shots, it just also has the frame treatment below
     running the whole time).
  2. A persistent scrapbook LOOK applied to the entire assembled video: a
     jagged torn-paper border around the frame, a tape-strip graphic, a
     couple of crack-line accents, grain, and a desaturated/contrast-boosted
     color grade — built once per render as a static RGBA overlay (PIL) and
     composited on top of the whole clip via one ffmpeg filter pass, rather
     than per-frame in Python (the border/tape/cracks don't move, so there's
     no reason to pay per-frame Python cost for them — only the wipe itself
     needs that).

Every template module in this package exposes the same signature:
    assemble(renderer, edl, resolution, ken_burns=False) -> Path
`renderer` is the Renderer instance (gives access to its work_dir,
assets_dir and the shared _trim_and_normalize_clip/_concat_clips helpers).
The returned Path is a video at `resolution`, ready for text/logo/music —
templates never touch those, that stays shared across all of them.

Known simplifications vs. the reference (being upfront about scope, not
pretending this is pixel-identical):
  - The reference's "background peeking through the margins" is other
    PHOTOS from the same shoot (a real collage). There's no second layer of
    stock photography here, so the margin is a plain torn-paper color
    instead of another image — same mechanic, simpler content.
  - The border/tape/cracks are one fixed composition for the whole video
    (one static overlay), where the reference clearly varies tape/crack
    placement shot to shot. Varying it per shot is a straightforward
    extension (seed it per clip index instead of once) but not done here.
  - Tearing open onto TEXT CARDS (copy on its own torn card) still needs
    template-aware cue timing — unchanged from before, still fase-2 work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from edl.schema import EDL
from render.ffmpeg_utils import run

FPS = 30
OVERLAP = 0.5  # seconds the tear takes to sweep fully across frame
MAX_OVERLAP_FRACTION = 0.4  # never eat more than 40% of either neighbouring clip

# torn edge shape — organic base curve + a few sharper localized rips, not a
# uniform zigzag (a uniform zigzag reads as a digital pattern, not a tear)
JAG_BASE_AMPLITUDE = 32.0
JAG_NOTCH_MIN = 40.0
JAG_NOTCH_MAX = 95.0
JAG_MAX_REACH = 170.0  # generous upper bound on how far any row's edge can stray, for sweep math

# the paper itself
PAPER_WIDTH = 46.0                          # px — this is a strip, not a seam
PAPER_COLOR = np.array([236.0, 228.0, 210.0])  # warm off-white, not pixel-white
GRAIN_STRENGTH = 10.0                        # per-pixel fibre noise inside the band
FIBER_EDGE_WIDTH = 7.0                       # brighter ridge right where the band meets the footage
FIBER_BRIGHTEN = 55.0
SHADOW_WIDTH = 34.0                          # px of soft shadow the lifted paper casts onto the revealed clip
SHADOW_STRENGTH = 0.4


def _decode_window(path: Path, resolution: tuple[int, int], seek_args: list[str], t: float) -> np.ndarray:
    """Decode `t` seconds of raw RGB frames starting at `seek_args`'s
    position (e.g. -sseof for "last t seconds", or nothing for "from the
    start"). Segments are already normalized to `resolution` @ FPS by
    Renderer._trim_and_normalize_clip, so no scaling is needed here."""
    w, h = resolution
    cmd = [
        "ffmpeg", "-y", *seek_args, "-i", str(path),
        "-t", f"{t:.3f}", "-vf", f"fps={FPS}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    frame_size = w * h * 3
    data = proc.stdout
    n = len(data) // frame_size
    if n == 0:
        raise RuntimeError(
            f"decoding a torn-paper transition window from {path} produced 0 frames:\n"
            f"{proc.stderr[-2000:].decode(errors='replace')}"
        )
    return np.frombuffer(data[: n * frame_size], dtype=np.uint8).reshape(n, h, w, 3)


def _jagged_edge(height: int, seed: int) -> np.ndarray:
    """A fixed per-row x-offset shape for the tear. Built the way an actual
    torn edge looks, not a mathematically clean wave: a smoothed random-walk
    base (organic, gently wandering — paper doesn't tear in a sine curve),
    plus a handful of sharper localized rips (real tears have a few deep
    notches, not uniform teeth), plus fine per-row jitter so the micro-edge
    is ragged rather than a smooth plastic curve."""
    rng = np.random.default_rng(seed)

    steps = rng.normal(0, 3.0, size=height)
    walk = np.cumsum(steps)
    walk -= walk.mean()
    kernel = np.ones(41) / 41
    base = np.convolve(walk, kernel, mode="same")
    peak = np.abs(base).max()
    if peak > 1e-6:
        base *= JAG_BASE_AMPLITUDE / peak

    edge = base.copy()
    for _ in range(rng.integers(3, 6)):
        center = rng.integers(0, height)
        width = int(rng.integers(18, 55))
        amp = rng.choice([-1.0, 1.0]) * rng.uniform(JAG_NOTCH_MIN, JAG_NOTCH_MAX)
        y0, y1 = max(0, center - width), min(height, center + width)
        local = np.arange(y0, y1) - center
        tri = np.clip(1 - np.abs(local) / max(width, 1), 0, 1)  # triangular falloff = a "rip" shape
        edge[y0:y1] += amp * tri

    fine = rng.uniform(-4.0, 4.0, size=height)
    fine = np.convolve(fine, np.ones(3) / 3, mode="same")
    return edge + fine


def _paper_grain(resolution: tuple[int, int], seed: int) -> np.ndarray:
    """Fixed fibre-noise texture for the paper band, generated once per
    transition. Smoothed noise, not per-pixel static, so it reads as paper
    fibre rather than video noise."""
    w, h = resolution
    rng = np.random.default_rng(seed + 500)
    noise = rng.uniform(-1.0, 1.0, size=(h, w)).astype(np.float32)
    # cheap separable box blur for a soft fibrous look instead of sharp grain
    k = 3
    kernel = np.ones(k) / k
    noise = np.apply_along_axis(lambda m: np.convolve(m, kernel, mode="same"), axis=1, arr=noise)
    noise = np.apply_along_axis(lambda m: np.convolve(m, kernel, mode="same"), axis=0, arr=noise)
    return noise * GRAIN_STRENGTH


def _ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _edge_lookup(u: np.ndarray, samples: np.ndarray) -> np.ndarray:
    """Look up jaggedness at continuous positions `u` (any shape) against a
    1D table of samples spanning u's own [min, max] range — lets the tear's
    edge vary smoothly along an arbitrary axis, not just along image rows."""
    u_min, u_max = float(u.min()), float(u.max())
    n = len(samples)
    idx = (u - u_min) / max(u_max - u_min, 1e-6) * (n - 1)
    idx = np.clip(idx, 0, n - 1)
    return np.interp(idx.ravel(), np.arange(n), samples).reshape(u.shape)


def _reveal_frame(
    frame_a: np.ndarray, frame_b: np.ndarray, angle_deg: float, edge_samples: np.ndarray,
    grain: np.ndarray, progress: float,
) -> np.ndarray:
    """Composite frame_a/frame_b against a jagged torn edge sweeping at
    `angle_deg` (0=left-to-right, 90=top-to-bottom, 45=corner-to-corner —
    the client's actual reference is mostly top-down/diagonal, not
    left-right, which is why this is a parameter and not fixed to one axis
    like the previous version of this function was)."""
    h, w, _ = frame_a.shape
    theta = np.radians(angle_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    s = xs * cos_t + ys * sin_t          # sweep axis — boundary moves along this
    u = -xs * sin_t + ys * cos_t         # perpendicular axis — jaggedness varies along this

    edge_val = _edge_lookup(u, edge_samples)
    s_min, s_max = float(s.min()), float(s.max())
    boundary_pos = s_min - JAG_MAX_REACH + _ease_in_out(progress) * ((s_max - s_min) + 2 * JAG_MAX_REACH)
    boundary = boundary_pos + edge_val

    # positive = the sweep has passed this pixel = newly revealed (frame_b).
    # (An earlier version of this had `s - boundary`, which is backwards —
    # it made progress=0 show frame_b everywhere and progress=1 show frame_a
    # everywhere, i.e. every Papercut transition played new-to-old and then
    # hard-cut BACK to the correct clip. Caught by a synthetic solid-colour
    # test, not by eye — the visual "is there a jagged edge sweeping"
    # checks that caught earlier bugs didn't check *which side* was which.)
    dist = boundary - s

    half = PAPER_WIDTH / 2.0
    out = np.where(dist[..., None] > 0, frame_b, frame_a).astype(np.float32)

    # soft shadow the lifted paper casts onto the newly revealed footage
    shadow_t = np.clip(1.0 - (dist - half) / SHADOW_WIDTH, 0.0, 1.0)
    shadow_t = np.where(dist > half, shadow_t, 0.0) * SHADOW_STRENGTH
    out = out * (1.0 - shadow_t[..., None])

    # the paper band itself: opaque strip, grained, feathered ~2px edges for AA
    band_mask = np.clip(1.0 - (np.abs(dist) - half) / 2.0, 0.0, 1.0)
    paper_rgb = PAPER_COLOR[None, None, :] + grain[..., None]
    fiber = np.clip(1.0 - np.abs(np.abs(dist) - half) / FIBER_EDGE_WIDTH, 0.0, 1.0) ** 1.4
    paper_rgb = paper_rgb + fiber[..., None] * FIBER_BRIGHTEN
    out = out * (1.0 - band_mask[..., None]) + paper_rgb * band_mask[..., None]

    return np.clip(out, 0, 255).astype(np.uint8)


FLASH_SUBFRAMES = 4
FLASH_CURVE = [0.35, 0.85, 0.65, 0.25]  # a quick pulse, not a flat block — reads as a flash, not a blackout
FLASH_COLOR = np.array([250.0, 246.0, 232.0])  # warm-white, not pixel-white — paper catching light, not a lighting error


def _flash_frame(frame: np.ndarray, amt: float) -> np.ndarray:
    out = frame.astype(np.float32) * (1.0 - amt) + FLASH_COLOR[None, None, :] * amt
    return np.clip(out, 0, 255).astype(np.uint8)


def _render_transition(renderer, seg_a: Path, seg_b: Path, resolution: tuple[int, int], overlap: float, index: int) -> Path:
    w, h = resolution
    frames_a = _decode_window(seg_a, resolution, ["-sseof", f"-{overlap:.3f}"], overlap)
    frames_b = _decode_window(seg_b, resolution, [], overlap)
    n = min(len(frames_a), len(frames_b), round(overlap * FPS))
    n_flash = min(FLASH_SUBFRAMES, max(n - 4, 0))
    n_reveal = n - n_flash

    edge_samples = _jagged_edge(max(w, h), seed=1000 + index)
    grain = _paper_grain(resolution, seed=1000 + index)
    # mostly top-down to diagonal-from-a-corner — what the reference actually
    # shows (see module docstring); a bit of per-cut variety within that band
    # rather than the same angle every single time.
    angle_deg = float(np.random.default_rng(2000 + index).uniform(40.0, 100.0))

    out_path = renderer.work_dir / f"tear_{index:02d}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS),
        "-i", "-",
        # explicit yuv420p: without it libx264 defaults raw-RGB input to
        # yuv444p, which silently breaks the concat-demuxer "-c copy" join
        # with the (yuv420p) body segments below — same container, mismatched
        # pixel format per segment. That's exactly what caused the frame
        # overlay to desync and vanish partway through the very first render
        # of this version (border/tape/grain fine for clip 0, gone from clip
        # 1 onward) — caught by checking frames every second instead of just
        # at the cuts.
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for k in range(n_flash):
            amt = FLASH_CURVE[k] if k < len(FLASH_CURVE) else FLASH_CURVE[-1]
            proc.stdin.write(_flash_frame(frames_a[k], amt).tobytes())
        for k in range(n_reveal):
            progress = (k + 0.5) / max(n_reveal, 1)
            ia = min(n_flash + k, len(frames_a) - 1)
            ib = min(n_flash + k, len(frames_b) - 1)
            frame = _reveal_frame(frames_a[ia], frames_b[ib], angle_deg, edge_samples, grain, progress)
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed encoding torn-paper transition {index} (exit {proc.returncode})")
    return out_path


# -- persistent scrapbook frame treatment (border/tape/cracks/grain/grade) --

BORDER_MARGIN = 26.0    # px — average distance the torn edge sits in from the frame edge
BORDER_BAND = 15.0       # px — width of the torn-paper strip itself
BORDER_JAG_SCALE = 0.4   # the border wobbles far less than a mid-video tear — it's a trim, not a rip
LOOK_SEED = 7            # fixed so the frame treatment is a consistent signature across renders


def _band_alpha(dist: np.ndarray, band: float, feather: float = 2.5) -> np.ndarray:
    inner = np.clip(dist / feather, 0.0, 1.0)
    outer = np.clip((band - dist) / feather, 0.0, 1.0)
    a = np.minimum(inner, outer)
    return np.where((dist >= 0.0) & (dist <= band), np.clip(a, 0.0, 1.0), 0.0)


def _border_layer(resolution: tuple[int, int], seed: int) -> np.ndarray:
    """RGBA (h, w, 4) float array: a jagged torn-paper strip framing the
    whole shot, same paper look (color/grain/fiber-highlight) as the wipe,
    just wrapped around the perimeter instead of sweeping across it."""
    w, h = resolution
    top_edge = BORDER_MARGIN + _jagged_edge(w, seed + 11) * BORDER_JAG_SCALE
    bottom_edge = BORDER_MARGIN + _jagged_edge(w, seed + 12) * BORDER_JAG_SCALE
    left_edge = BORDER_MARGIN + _jagged_edge(h, seed + 13) * BORDER_JAG_SCALE
    right_edge = BORDER_MARGIN + _jagged_edge(h, seed + 14) * BORDER_JAG_SCALE

    ys = np.arange(h)[:, None].astype(np.float32)
    xs = np.arange(w)[None, :].astype(np.float32)

    dist_top = ys - top_edge[None, :]
    dist_bottom = (h - 1 - ys) - bottom_edge[None, :]
    dist_left = xs - left_edge[:, None]
    dist_right = (w - 1 - xs) - right_edge[:, None]

    alpha = np.zeros((h, w), dtype=np.float32)
    fiber_amt = np.zeros((h, w), dtype=np.float32)
    for dist in (dist_top, dist_bottom, dist_left, dist_right):
        a = _band_alpha(dist, BORDER_BAND)
        alpha = np.maximum(alpha, a)
        fiber = np.clip(1.0 - np.abs(dist - BORDER_BAND) / FIBER_EDGE_WIDTH, 0.0, 1.0) ** 1.4
        fiber_amt = np.maximum(fiber_amt, fiber * (a > 0))

    grain = _paper_grain(resolution, seed + 20)
    color = PAPER_COLOR[None, None, :] + grain[..., None] + fiber_amt[..., None] * FIBER_BRIGHTEN
    color = np.clip(color, 0, 255)
    return np.dstack([color, alpha * 255.0])


def _draw_tape(draw: ImageDraw.ImageDraw, resolution: tuple[int, int], rng: np.random.Generator) -> None:
    """A masking-tape strip graphic — semi-transparent grey, ragged short
    ends (never a clean rectangle), a couple of faint diagonal sheen lines."""
    w, h = resolution
    tape_w = int(w * rng.uniform(0.5, 0.66))
    tape_h = int(h * rng.uniform(0.032, 0.044))
    cx = w // 2 + int(rng.uniform(-0.05, 0.05) * w)
    y0 = int(h * rng.uniform(0.025, 0.05))
    y1 = y0 + tape_h
    x0, x1 = cx - tape_w // 2, cx + tape_w // 2
    notch = max(int(tape_h * 0.55), 4)

    pts = [
        (x0 + int(rng.integers(0, notch)), y0),
        (x1 - int(rng.integers(0, notch)), y0),
        (x1, y0 + int(rng.integers(0, notch))),
        (x1 - int(rng.integers(0, notch)), y1),
        (x0 + int(rng.integers(0, notch)), y1),
        (x0, y1 - int(rng.integers(0, notch))),
    ]
    draw.polygon(pts, fill=(160, 157, 152, 120))
    for i in range(5):
        lx = x0 + (x1 - x0) * i / 5 + int(rng.integers(-8, 8))
        draw.line([(lx, y0), (lx + 14, y1)], fill=(255, 255, 255, 28), width=1)


def _draw_cracks(draw: ImageDraw.ImageDraw, resolution: tuple[int, int], seed: int) -> None:
    """A couple of thin "stress fracture" hairlines across the shot — the
    fine crack-lines visible running through the reference's photos, not a
    second torn edge, just a faint highlight + offset shadow stroke."""
    w, h = resolution
    rng = np.random.default_rng(seed + 900)
    for i in range(int(rng.integers(2, 4))):
        vertical = rng.random() < 0.6
        if vertical:
            x0 = int(rng.integers(int(w * 0.2), int(w * 0.8)))
            wobble = _jagged_edge(h, seed + 950 + i) * 0.12
            pts = [(int(x0 + wobble[y]), y) for y in range(0, h, 8)]
        else:
            y0 = int(rng.integers(int(h * 0.25), int(h * 0.75)))
            wobble = _jagged_edge(w, seed + 970 + i) * 0.12
            pts = [(x, int(y0 + wobble[x])) for x in range(0, w, 8)]
        draw.line([(x + 1, y + 1) for x, y in pts], fill=(0, 0, 0, 30), width=1, joint="curve")
        draw.line(pts, fill=(255, 255, 255, 60), width=1, joint="curve")


def _build_frame_overlay(resolution: tuple[int, int], seed: int) -> Image.Image:
    rgba = np.clip(_border_layer(resolution, seed), 0, 255).astype(np.uint8)
    img = Image.fromarray(rgba, mode="RGBA")
    draw = ImageDraw.Draw(img)
    rng = np.random.default_rng(seed + 77)
    _draw_tape(draw, resolution, rng)
    _draw_cracks(draw, resolution, seed)
    return img


def _apply_look(renderer, video_path: Path, resolution: tuple[int, int], seed: int = LOOK_SEED) -> Path:
    """Composite the persistent scrapbook treatment over the whole
    assembled video in one ffmpeg pass: a muted/contrasty color grade, film
    grain, and the static border+tape+cracks overlay on top."""
    overlay_path = renderer.work_dir / "papercut_overlay.png"
    _build_frame_overlay(resolution, seed).save(overlay_path)

    out_path = renderer.work_dir / "papercut_look.mp4"
    filter_complex = (
        "[0:v]eq=saturation=0.85:contrast=1.05:brightness=0.015,"
        "noise=alls=9:allf=t+u[graded];"
        "[graded][1:v]overlay=0:0:format=auto[out]"
    )
    run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(overlay_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        str(out_path),
    ])
    return out_path


def _trim(renderer, src: Path, start: float, dur: float, index: int) -> Path:
    out_path = renderer.work_dir / f"paper_body_{index:02d}.mp4"
    run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
        "-pix_fmt", "yuv420p",  # must match _render_transition's segments — see the comment there
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path),
    ])
    return out_path


def assemble(renderer, edl: EDL, resolution: tuple[int, int], ken_burns: bool = False) -> Path:
    segments = [
        renderer._trim_and_normalize_clip(clip.source, clip.duration, i, resolution, ken_burns=ken_burns)
        for i, clip in enumerate(edl.clips)
    ]
    if len(segments) == 1:
        assembled = segments[0]
    else:
        durations = [clip.duration for clip in edl.clips]
        overlaps = [
            min(OVERLAP, min(durations[i], durations[i + 1]) * MAX_OVERLAP_FRACTION)
            for i in range(len(segments) - 1)
        ]

        parts: list[Path] = []
        for i, seg in enumerate(segments):
            lead = overlaps[i - 1] if i > 0 else 0.0
            trail = overlaps[i] if i < len(segments) - 1 else 0.0
            body_dur = durations[i] - lead - trail
            if body_dur > 0.02:
                parts.append(_trim(renderer, seg, lead, body_dur, i))
            if i < len(segments) - 1:
                parts.append(_render_transition(renderer, seg, segments[i + 1], resolution, overlaps[i], i))
        assembled = renderer._concat_clips(parts)

    # the scrapbook frame treatment (border/tape/cracks/grain/grade) runs for
    # the whole video, not just at cuts — see module docstring.
    return _apply_look(renderer, assembled, resolution)
