"""
Real motion-design text overlay for text_style="punch" — replaces the
static drawtext version. This is FFmpeg-only end-to-end, but the text
itself is no longer a filter-graph caption: it's pre-rendered per-element
(PIL, with real stroke + blurred drop shadow) and animated frame-by-frame
with a proper easing curve (scale-bounce pop-in, not a linear fade), piped
into ffmpeg as a transparent RGBA video and composited on top of the ad.

Why this exists: drawtext can size/position/enable text, but it cannot
scale or ease a value over time — every drawtext "animation" is really
just an appear/disappear cut. A bounce-pop is what separates "template
caption" from "someone designed this," so it has to be built as actual
per-frame image compositing instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from edl.schema import EDL

ACCENT = (255, 0, 153, 255)   # 0xFF0099 — ADmazing brand pink
INK = (10, 10, 13, 255)       # 0x0A0A0D — near-black
WHITE = (255, 255, 255, 255)

# Layers are pre-rendered larger than their steady-state size so the
# animation's overshoot (ease-out-back peaks just above 1.0) only ever
# downsamples, never upsamples — keeps the bounce crisp instead of soft.
RENDER_HEADROOM = 1.25

POP_DURATION = 0.28   # seconds for the scale-bounce to settle
FADE_DURATION = 0.12  # seconds for opacity to reach 1 (faster than the bounce, so it "pops")


def _ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    t = max(0.0, min(t, 1.0))
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _ease_out_quad(t: float) -> float:
    t = max(0.0, min(t, 1.0))
    return 1 - (1 - t) ** 2


def _render_word_layer(text: str, font_path: str, fontsize: int, fill: tuple) -> Image.Image:
    render_size = int(fontsize * RENDER_HEADROOM)
    font = ImageFont.truetype(font_path, render_size)
    stroke_width = max(int(render_size * 0.09), 2)
    shadow_blur, shadow_offset, shadow_alpha = 10, (0, 8), 130

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = probe.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = stroke_width + shadow_blur + max(abs(shadow_offset[0]), abs(shadow_offset[1])) + 12
    W, H = tw + pad * 2, th + pad * 2
    origin = (pad - bbox[0], pad - bbox[1])

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (origin[0] + shadow_offset[0], origin[1] + shadow_offset[1]), text, font=font,
        fill=(0, 0, 0, shadow_alpha), stroke_width=stroke_width, stroke_fill=(0, 0, 0, shadow_alpha),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
    layer = Image.alpha_composite(layer, shadow)
    ImageDraw.Draw(layer).text(origin, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=(0, 0, 0, 255))
    return layer


def _render_cta_badge(text: str, font_path: str, fontsize: int) -> Image.Image:
    render_size = int(fontsize * RENDER_HEADROOM)
    font = ImageFont.truetype(font_path, render_size)
    pad_x, pad_y = int(render_size * 0.55), int(render_size * 0.32)
    shadow_pad = 16

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    W = tw + pad_x * 2 + shadow_pad * 2
    H = th + pad_y * 2 + shadow_pad * 2

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [shadow_pad, shadow_pad + 6, W - shadow_pad, H - shadow_pad + 6], radius=14, fill=(0, 0, 0, 150)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    layer = Image.alpha_composite(layer, shadow)

    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle([shadow_pad, shadow_pad, W - shadow_pad, H - shadow_pad], radius=14, fill=ACCENT)
    text_origin = (shadow_pad + pad_x - bbox[0], shadow_pad + pad_y - bbox[1])
    draw.text(text_origin, text, font=font, fill=INK)
    return layer


def _place(canvas: Image.Image, layer: Image.Image, t: float, start: float, cx: int, cy: int) -> None:
    progress = t - start
    scale = _ease_out_back(progress / POP_DURATION) if progress < POP_DURATION else 1.0
    opacity = _ease_out_quad(progress / FADE_DURATION) if progress < FADE_DURATION else 1.0
    scale = max(scale, 0.01) / RENDER_HEADROOM

    lw, lh = layer.size
    new_size = (max(int(lw * scale), 1), max(int(lh * scale), 1))
    scaled = layer.resize(new_size, Image.LANCZOS)
    if opacity < 1.0:
        scaled = scaled.copy()
        alpha = scaled.split()[3].point(lambda p, o=opacity: int(p * o))
        scaled.putalpha(alpha)

    pos = (cx - new_size[0] // 2, cy - new_size[1] // 2)
    canvas.alpha_composite(scaled, pos)


def render_punch_overlay(
    edl: EDL, resolution: tuple[int, int], fps: int, work_dir: Path, font_path: str, duration: float | None = None
) -> Path:
    """`duration` overrides edl.duration when the template that assembled the
    video produced a different real runtime (e.g. Rotator's crossfades, or
    Splitter's paired clips) — see render/pipeline.py's render()."""
    w, h = resolution
    duration = duration if duration is not None else edl.duration
    total_frames = int(duration * fps) + 4  # small buffer so it never runs out before the main video ends

    headline_cue = next((c for c in edl.text_cues if c.kind == "headline"), None)
    cta_cue = next((c for c in edl.text_cues if c.kind == "cta"), None)

    safe_width = w * 0.9
    words_timeline: list[tuple[float, float, Image.Image]] = []
    if headline_cue:
        words = headline_cue.content.upper().split()
        window = max(headline_cue.end - headline_cue.start, 0.1)
        per_word = window / len(words)
        hold = per_word * 1.15
        longest = max(len(word) for word in words)
        fontsize = max(min(int(h * 0.11), int(safe_width / (max(longest, 1) * 0.72))), 40)
        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            layer = _render_word_layer(word, font_path, fontsize, ACCENT if is_last else WHITE)
            w_start = headline_cue.start + i * per_word
            w_end = min(w_start + hold, headline_cue.end)
            words_timeline.append((w_start, w_end, layer))

    cta_layer = None
    if cta_cue:
        char_count = max(len(cta_cue.content), 1)
        cta_fontsize = max(min(int(h * 0.055), int(safe_width / (char_count * 0.62))), 28)
        cta_layer = _render_cta_badge(cta_cue.content.upper(), font_path, cta_fontsize)

    out_path = work_dir / "punch_overlay.mov"
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-r", str(fps),
        "-i", "-",
        "-c:v", "qtrle",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None

    try:
        for frame_idx in range(total_frames):
            t = frame_idx / fps
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

            for w_start, w_end, layer in words_timeline:
                if w_start <= t < w_end:
                    _place(canvas, layer, t, w_start, w // 2, int(h * 0.40))
                    break

            if cta_cue and cta_layer is not None and cta_cue.start <= t < cta_cue.end:
                _place(canvas, cta_layer, t, cta_cue.start, w // 2, int(h * 0.82))

            proc.stdin.write(canvas.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed while encoding the punch text overlay (exit {proc.returncode})")

    return out_path
