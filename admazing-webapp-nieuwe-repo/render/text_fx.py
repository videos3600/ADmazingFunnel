"""CAPTURE motion preset for ADmazing (Pillow + FFmpeg).

Reference: supplied 1920x1080 / 30 fps clip, CAPTURE at frames 21–67.
Authored approximation of its geometry and timing, not a pixel-identical copy.
The original font, project and exact motion curves are unavailable.

Drop-in entry point: render_punch_overlay(edl, resolution, fps, work_dir,
                                        font_path, duration=None).
Standalone: python text_fx.py --preview capture_preview.mp4 --text CAPTURE
Requires Pillow and ffmpeg. Preview font defaults to DejaVu Sans Bold;
use --font to supply your own licensed heavy, extended sans-serif font.
"""
from __future__ import annotations

import argparse
import math
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFilter, ImageFont

if TYPE_CHECKING:
    from edl.schema import EDL

ACCENT = (255, 0, 153, 255)
INK = (10, 10, 13, 255)
WHITE = (245, 242, 233, 255)
RENDER_HEADROOM = 1.75
POP_DURATION = 0.28
FADE_DURATION = 0.12
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _ease_out_back(t: float) -> float:
    t = max(0.0, min(t, 1.0))
    return 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2


def _ease_out_quad(t: float) -> float:
    t = max(0.0, min(t, 1.0))
    return 1 - (1 - t) ** 2


def _curve(frame: float, keys: tuple[tuple[float, float], ...]) -> float:
    if frame <= keys[0][0]:
        return keys[0][1]
    for (a, av), (b, bv) in zip(keys, keys[1:]):
        if frame <= b:
            return av + (bv - av) * (frame - a) / (b - a)
    return keys[-1][1]


class CapturePreset:
    """Independent glyph motion, three rows, sliced echoes and stepped erase.

    All timings use a 30 fps design clock; output fps never changes the speed.
    There is no frame RNG and no reference-image content in rendered output.
    The glyph cache stays bounded even for long headlines.
    """

    BUILD_FRAMES = 27.0
    ERASE_FRAMES = 13.0

    def __init__(self, text: str, font_path: str, fill: tuple = WHITE):
        self.text = text.upper().strip()
        if not self.text:
            raise ValueError("CapturePreset requires non-empty text")
        self.fill = fill
        font = ImageFont.truetype(str(font_path), 240)
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        cap = probe.textbbox((0, 0), "H", font=font)
        cap_height = cap[3] - cap[1]
        self.glyphs = []
        for char in self.text:
            if char == " ":
                self.glyphs.append(Image.new("L", (int(cap_height * 0.42), cap_height)))
                continue
            box = probe.textbbox((0, 0), char, font=font, stroke_width=9)
            mask = Image.new("L", (box[2] - box[0] + 4, box[3] - box[1] + 4))
            ImageDraw.Draw(mask).text((2 - box[0], 2 - box[1]), char,
                                     font=font, fill=255, stroke_width=9)
            self.glyphs.append(mask)
        self.widths = [g.width / g.height for g in self.glyphs]
        self.total_ratio = sum(self.widths) + 0.025 * (len(self.text) - 1)

    @lru_cache(maxsize=256)
    def _glyph(self, index: int, width: int, height: int) -> Image.Image:
        mask = self.glyphs[index].resize((width, height), Image.Resampling.LANCZOS)
        layer = Image.new("RGBA", (width, height), self.fill)
        if self.fill[3] != 255:
            mask = mask.point(lambda a: round(a * self.fill[3] / 255))
        layer.putalpha(mask)
        return layer

    def _row(self, w: int, h: int, glyph_h: float, x: float, cy: float,
             count: int, stretch: float, echo: float, frame: int) -> Image.Image:
        row = Image.new("RGBA", (w, h))
        cursor = x
        for index in range(count):
            gw = max(1, round(glyph_h * self.widths[index] * stretch))
            gh = max(1, round(glyph_h))
            if cursor + gw >= 0 and cursor < w:
                layer = self._glyph(index, gw, gh)
                row.alpha_composite(layer, (round(cursor), round(cy - gh / 2)))
            cursor += gw + glyph_h * 0.025 * stretch
        if echo > 0:
            # A flattened second impression underneath each row, not a soft fade.
            box = row.getbbox()
            if box:
                impression = row.crop(box)
                impression = impression.resize((impression.width,
                                                max(1, round(impression.height * echo))),
                                               Image.Resampling.LANCZOS)
                impression.putalpha(impression.getchannel("A").point(lambda a: round(a * 0.8)))
                row.alpha_composite(impression, (box[0], box[3] + max(1, round(h * 0.005))))
        # Brief horizontal strips jump independently at authored transition frames.
        if frame in {2, 3, 7, 8, 13, 16, 19, 22, 24, 25, 28, 29, 35}:
            for strip, (dy, thickness, dx) in enumerate(((0.16, 0.07, -0.045),
                                                       (0.63, 0.055, 0.03))):
                y = round(cy - glyph_h / 2 + glyph_h * dy)
                bottom = min(h, y + max(1, round(glyph_h * thickness)))
                top = max(0, y)
                if bottom > top:
                    band = row.crop((0, top, w, bottom))
                    ImageDraw.Draw(row).rectangle((0, top, w, bottom - 1), fill=(0, 0, 0, 0))
                    row.alpha_composite(band, (round(dx * glyph_h), top))
        return row

    def draw(self, canvas: Image.Image, elapsed: float, span: float,
             cx: float | None = None, cy: float | None = None) -> None:
        if elapsed < 0 or elapsed >= span or span <= 0:
            return
        w, h = canvas.size
        cx = w / 2 if cx is None else cx
        cy = h / 2 if cy is None else cy
        # Preserve a legible hold even when the EDL gives a very short cue.
        build = min(self.BUILD_FRAMES / 30, span * 0.62)
        erase = min(self.ERASE_FRAMES / 30, span * 0.27)
        exit_start = span - erase
        n = len(self.text)
        target_h = min(h * 0.12, w * 0.74 / (self.total_ratio * 1.40))
        target_w = w * 0.74

        if elapsed < build:
            f = round(elapsed / build * self.BUILD_FRAMES, 6)
            fi = int(f + 1e-6)
            # CAPTURE: C, A, P, T, U, R, E arrive at 0, 6, 12, 15, 18, 21, 23.
            checkpoints = (0, 6, 12, 15, 18, 21, 23)
            reveal = sum(f >= k for k in checkpoints) / 7
            count = max(1, min(n, math.ceil(reveal * n)))
            glyph_h = _curve(f, ((0, h * 0.68), (5, h * 0.68),
                                  (6, h * 0.60), (11, h * 0.60),
                                  (12, h * 0.48), (14, h * 0.48),
                                  (15, h * 0.33), (17, h * 0.33),
                                  (18, h * 0.21), (20, h * 0.21),
                                  (21, h * 0.15), (23, target_h),
                                  (26, target_h * 0.78), (27, target_h * 0.78)))
            stretch = target_w / (glyph_h * self.total_ratio) if f >= 23 else 1.40
            widths = [glyph_h * r * stretch + glyph_h * 0.025 * stretch
                      for r in self.widths[:count]]
            last_checkpoint = checkpoints[max(0, min(6, math.ceil(count / n * 7) - 1))]
            age = max(0, f - last_checkpoint)
            # Incoming letter travels from the right; the existing letters leave left.
            latest_x = w * ((0.60 - min(age, 5) * 0.26) if f < 15
                            else (0.68 - min(age, 3) * 0.12))
            x = latest_x - sum(widths[:-1])
            center_x = cx - sum(widths) / 2
            if f >= 21:
                settle = min(1, (f - 21) / 5)
                x = x * (1 - settle) + center_x * settle
            rows = [0] if f < 2 else [-1, 0, 1]
            echo = _curve(f, ((0, 0), (20, 0), (22, 0.07),
                             (24, 0.28), (27, 0.34)))
        elif elapsed < exit_start:
            f = 27 + (elapsed - build) * 30
            fi = int(f + 1e-6)
            count = n
            # The compressed rows rebound before holding at full cap height.
            glyph_h = target_h * _curve(f, ((27, 0.78), (29, 0.78),
                                            (32, 0.99), (33, 1.0)))
            stretch = target_w / (glyph_h * self.total_ratio)
            x = cx - glyph_h * self.total_ratio * stretch / 2
            rows = [-1, 0, 1]
            echo = _curve(f, ((27, 0.34), (29, 0.34), (32, 0.06), (33, 0.035)))
        else:
            f = (elapsed - exit_start) / erase * self.ERASE_FRAMES
            fi = 40 + int(f + 1e-6)
            count = max(1, math.ceil(n * (1 - f / self.ERASE_FRAMES)))
            glyph_h = target_h
            stretch = target_w / (glyph_h * self.total_ratio)
            x = cx - target_w / 2
            rows = [-1, 0, 1] if f < 3 else [0]
            echo = 0.035 if f < 3 else 0

        result = Image.new("RGBA", canvas.size)
        for ri in rows:
            row_y = cy + ri * h * 0.20
            # A different arrival offset per row produces the repeated-letter cascade.
            jitter = (ri * w * 0.035 * (1 - min(1, f / 23))
                      if elapsed < build else 0)
            row = self._row(w, h, glyph_h, x + jitter, row_y,
                            count, stretch, echo, fi)
            if elapsed >= exit_start and f < 3 and ri != 0:
                # Outer rows collapse through a hard mask before the prefix erase.
                visible = glyph_h * max(0, 1 - f / 3)
                top, bottom = int(row_y - visible / 2), int(row_y + visible / 2)
                masked = Image.new("RGBA", canvas.size)
                top, bottom = max(0, top), min(h, bottom)
                if bottom > top:
                    masked.alpha_composite(row.crop((0, top, w, bottom)), (0, top))
                row = masked
            result.alpha_composite(row)
        # Low-level optical bloom on bright type, without a black cartoon outline.
        glow = result.filter(ImageFilter.GaussianBlur(max(0.6, h / 360)))
        glow.putalpha(glow.getchannel("A").point(lambda a: round(a * 0.17)))
        canvas.alpha_composite(glow)
        canvas.alpha_composite(result)


def _encode_frames(out_path: Path, size: tuple[int, int], fps: int,
                   frames, transparent: bool) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    codec = (["-c:v", "qtrle"] if transparent else
             ["-c:v", "libx264", "-crf", "18", "-preset", "fast",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart"])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size[0]}x{size[1]}",
           "-r", str(fps), "-i", "-", "-an", *codec, str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        assert proc.stdin is not None
        for frame in frames:
            proc.stdin.write(frame.tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError(f"FFmpeg failed encoding {out_path.name}: {proc.returncode}")
    return out_path


def render_punch_overlay(edl: EDL, resolution: tuple[int, int], fps: int,
                         work_dir: Path, font_path: str,
                         duration: float | None = None) -> Path:
    """Existing pipeline interface; alpha MOV, headline cues and existing CTA.

    edl.text_animation picks which of the two interchangeable animation
    templates renders the headline: "capture" (default) is the per-letter
    cascade/glitch below; "pop" is the calmer per-word scale-bounce in
    _render_pop_overlay(). Both share the same CTA badge and write the same
    punch_overlay.mov, so render/pipeline.py never has to know which one ran."""
    if fps <= 0 or min(resolution) <= 0:
        raise ValueError("Resolution and fps must be positive")
    duration = float(edl.duration if duration is None else duration)
    if duration <= 0:
        raise ValueError("Duration must be positive")
    if getattr(edl, "text_animation", "capture") == "pop":
        return _render_pop_overlay(edl, resolution, fps, work_dir, font_path, duration)
    w, h = resolution
    headline = next((c for c in edl.text_cues if c.kind == "headline"), None)
    cta = next((c for c in edl.text_cues if c.kind == "cta"), None)
    timeline = []
    if headline and headline.content.strip() and headline.end > headline.start:
        words = headline.content.upper().split()
        step = (headline.end - headline.start) / len(words)
        overlap = min(2 / 30, step * 0.08)
        for i, word in enumerate(words):
            start = headline.start + i * step
            end = min(headline.end, start + step + overlap)
            timeline.append((start, end, CapturePreset(word, font_path)))
    cta_layer = None
    if cta and cta.content.strip():
        fontsize = max(1, min(round(h * 0.055),
                              round(w * 0.9 / (len(cta.content) * 0.62))))
        cta_layer = _render_cta_badge(cta.content.upper(), font_path, fontsize)

    def frames():
        for idx in range(math.ceil(duration * fps) + 4):
            t = idx / fps
            canvas = Image.new("RGBA", resolution)
            for start, end, preset in timeline:
                preset.draw(canvas, t - start, end - start, w / 2, h * 0.40)
            if cta_layer is not None and cta.start <= t < cta.end:
                _place(canvas, cta_layer, t, cta.start, w // 2, round(h * 0.82))
            yield canvas
    return _encode_frames(Path(work_dir) / "punch_overlay.mov", resolution, fps, frames(), True)


def _render_pop_word_layer(text: str, font_path: str, fontsize: int, fill: tuple) -> Image.Image:
    """The "pop" template's headline word: one solid layer, soft blurred
    drop shadow for legibility, no stroke outline — unlike the earlier
    baseline this style replaces, which drew a hard black border around
    every word (read as off-brand once the pink/white palette landed)."""
    render_size = int(fontsize * RENDER_HEADROOM)
    font = ImageFont.truetype(str(font_path), render_size)
    shadow_blur, shadow_offset, shadow_alpha = 10, (0, 8), 130

    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = probe.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = shadow_blur + max(abs(shadow_offset[0]), abs(shadow_offset[1])) + 12
    W, H = tw + pad * 2, th + pad * 2
    origin = (pad - bbox[0], pad - bbox[1])

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (origin[0] + shadow_offset[0], origin[1] + shadow_offset[1]), text, font=font,
        fill=(0, 0, 0, shadow_alpha),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))
    layer = Image.alpha_composite(layer, shadow)
    ImageDraw.Draw(layer).text(origin, text, font=font, fill=fill)
    return layer


def _render_pop_overlay(edl: EDL, resolution: tuple[int, int], fps: int,
                        work_dir: Path, font_path: str, duration: float) -> Path:
    """"pop" animation template: one clean scale-bounce pop-in per headline
    word (reusing the same _place()/_render_cta_badge() as the CTA badge
    and the "capture" template above), held, then replaced by the next
    word — no per-letter build, no glitch strips. The last word pops in
    ACCENT pink so there's still one beat of emphasis."""
    w, h = resolution
    headline = next((c for c in edl.text_cues if c.kind == "headline"), None)
    cta = next((c for c in edl.text_cues if c.kind == "cta"), None)
    safe_width = w * 0.9

    words_timeline: list[tuple[float, float, Image.Image]] = []
    if headline and headline.content.strip() and headline.end > headline.start:
        words = headline.content.upper().split()
        window = max(headline.end - headline.start, 0.1)
        per_word = window / len(words)
        hold = per_word * 1.15
        longest = max(len(word) for word in words)
        fontsize = max(min(int(h * 0.11), int(safe_width / (max(longest, 1) * 0.72))), 40)
        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            layer = _render_pop_word_layer(word, font_path, fontsize, ACCENT if is_last else WHITE)
            w_start = headline.start + i * per_word
            w_end = min(w_start + hold, headline.end)
            words_timeline.append((w_start, w_end, layer))

    cta_layer = None
    if cta and cta.content.strip():
        fontsize = max(1, min(round(h * 0.055), round(safe_width / (len(cta.content) * 0.62))))
        cta_layer = _render_cta_badge(cta.content.upper(), font_path, fontsize)

    def frames():
        for idx in range(math.ceil(duration * fps) + 4):
            t = idx / fps
            canvas = Image.new("RGBA", resolution)
            for w_start, w_end, layer in words_timeline:
                if w_start <= t < w_end:
                    _place(canvas, layer, t, w_start, w // 2, int(h * 0.40))
                    break
            if cta_layer is not None and cta.start <= t < cta.end:
                _place(canvas, cta_layer, t, cta.start, w // 2, round(h * 0.82))
            yield canvas
    return _encode_frames(Path(work_dir) / "punch_overlay.mov", resolution, fps, frames(), True)


def render_preview(out_path: Path, text: str = "CAPTURE", font_path: str = DEFAULT_FONT,
                   resolution: tuple[int, int] = (1920, 1080), fps: int = 30) -> Path:
    preset = CapturePreset(text, font_path)

    def frames():
        for index in range(round(2.3 * fps)):
            canvas = Image.new("RGBA", resolution, (14, 14, 14, 255))
            preset.draw(canvas, index / fps - 0.7, 47 / 30)
            yield canvas
    return _encode_frames(out_path, resolution, fps, frames(), False)


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
    """Scale-bounce pop-in — still used for the CTA badge (a single stamped
    element reads better with one clean pop than a letter-by-letter build)."""
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



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, default=Path("capture_preview.mp4"))
    parser.add_argument("--text", default="CAPTURE")
    parser.add_argument("--font", default=DEFAULT_FONT)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()
    # CTA helpers are defined before this block in the completed module.
    render_preview(args.preview, args.text, args.font, (args.width, args.height), args.fps)
