"""
Format 001 renderer: turns a block-based EDL (edl.blocks — see
edl/format_001.py) into one MP4.

Each block becomes its own normalized segment at the EDL's resolution/fps,
then every segment is concatenated via the same ffmpeg concat-demuxer
approach render/templates/*.py already use for footage cuts
(render.pipeline.Renderer._concat_clips) — which is why every segment here
is encoded with identical codec/pix_fmt/resolution/fps, footage and
generated cards alike; concat -c copy silently breaks the moment one
segment's encode settings drift from the others (render/templates/papercut.py
has a comment about exactly this).

"footage" blocks reuse Renderer._trim_and_normalize_clip — no ken_burns:
Format 001 is a fixed-rhythm CUT format (that's the whole point of its
block timing), not a continuous-push-in template like the punch overlay's
Ken Burns effect.

The HERO block additionally carries `logo_overlay=True` (see
edl/format_001.py) — the "early brand moment" measured from John's
reference edit (Jonny's Burgers): the logo appearing as a badge stamped
onto the back ~40% of the product footage, not a separate full-screen
card. `_apply_logo_overlay` below composites that badge onto HERO's
trimmed/normalized segment using the exact two-step pattern
render/pipeline.py's `_apply_punch_text_and_logo` already uses for the
punch-text overlay: render a transparent pop-in badge (reusing _place(),
same as everywhere else in this app) then ffmpeg filter_complex
overlay=0:0 it onto the footage — so this doesn't introduce a third,
inconsistent compositing approach.

"tekstkaart"/"logo"/"overlay" blocks are generated from scratch here: a
solid brand-colour background with text/logo/CTA-badge composited on top
via the SAME pop-in motion and CTA badge render/text_fx.py's punch-text
overlay already uses (_render_pop_word_layer / _render_cta_badge / _place),
so Format 001 doesn't introduce a third, inconsistent typography treatment
alongside CAPTURE/pop.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from edl.schema import EDL
from render.ffmpeg_utils import probe_duration
from render.ffmpeg_utils import run as _run
from render.pipeline import Renderer, FORMAT_RESOLUTIONS, _resolve_punch_font
from render.text_fx import (
    ACCENT,
    INK,
    WHITE,
    _encode_frames,
    _place,
    _render_cta_badge,
    _render_pop_word_layer,
)

FPS = 30
BG_DARK = (10, 10, 13, 255)      # --bg (style.css)
BG_ACCENT = ACCENT               # --accent (style.css) — reused as-is, not redefined, so the two can't drift apart


def _logo_layer(logo_path: Path, target_w: int) -> Image.Image | None:
    if not logo_path.exists():
        return None
    img = Image.open(logo_path).convert("RGBA")
    ratio = target_w / img.width
    return img.resize((target_w, max(int(img.height * ratio), 1)), Image.LANCZOS)


def _text_card(
    text: str, font_path: str, resolution: tuple[int, int], duration: float,
    work_dir: Path, index: int, *, accent_bg: bool = False, logo_path: Path | None = None,
) -> Path:
    """HOOK/USP (plain tekstkaart) blocks: every word gets its OWN line,
    stacked top to bottom and popped in with a short stagger. Also still
    used for a "logo" block type if one is ever constructed again (it
    accepts `logo_path`) — Format 001 as of Oct 17 2026 no longer builds a
    standalone BRAND/"logo" block (brand only shows as HERO's overlay
    badge, see `_apply_logo_overlay` below), so this branch is currently
    unreachable from render_format_001_edl, kept for forward-compat.

    Previously all words were joined onto ONE line, sized to fit only the
    LONGEST WORD's width — fine for a single word, but a multi-word phrase
    (e.g. "BINNEN 30 MINUTEN") then ran past the edges of the frame, since
    nothing accounted for the full joined string's width. Client feedback:
    "Zorg er wel voor dat de tekst goed te zien is! Mijn voorkeur is groot
    en onder elkaar" (make sure the text is clearly visible, large and
    stacked). Sizing each word for its OWN line fixes the overflow and lets
    every word be considerably larger than before, since it no longer
    shares width with the rest of the phrase.

    Still the same _place() scale-bounce pop used everywhere else in this
    app, staggered slightly per line — not CapturePreset's per-letter
    cascade (the client also named "Capture" as an example animation).
    CapturePreset draws two extra "echo" rows ±20% of the FULL frame
    height above/below its own centre — built for one centered headline
    with room to spare, not compact multi-line stacking; stacking two or
    three of those close together overlaps their echo bands into a blur.
    The staggered pop below is the animated version of this stack that
    actually holds up; swap in CapturePreset per line later if the client
    wants the literal cascade and is fine with looser line spacing to give
    the echo bands room."""
    w, h = resolution
    bg_color = BG_ACCENT if accent_bg else BG_DARK
    fill = INK if accent_bg else WHITE

    words = (text or "").upper().split() or ["ADMAZING"]
    safe_width = w * 0.86
    layers = [
        _render_pop_word_layer(
            word, font_path,
            max(min(int(h * 0.15), int(safe_width / (max(len(word), 1) * 0.58))), 48),
            fill,
        )
        for word in words
    ]

    logo_layer = _logo_layer(logo_path, int(w * 0.28)) if logo_path else None

    gap = h * 0.025
    logo_h = logo_layer.height if logo_layer is not None else 0
    logo_gap = h * 0.05 if logo_layer is not None else 0
    stack_h = sum(l.height for l in layers) + gap * (len(layers) - 1)
    total_h = logo_h + logo_gap + stack_h

    # Scale the whole stack down uniformly if it would run past the top or
    # bottom of the frame (a long headline with several words) — the same
    # "never let it overflow" principle that drove this rewrite, just
    # applied vertically instead of horizontally.
    vertical_budget = h * 0.86
    if total_h > vertical_budget > 0:
        scale = vertical_budget / total_h
        layers = [l.resize((max(1, int(l.width * scale)), max(1, int(l.height * scale))), Image.LANCZOS) for l in layers]
        if logo_layer is not None:
            logo_layer = logo_layer.resize(
                (max(1, int(logo_layer.width * scale)), max(1, int(logo_layer.height * scale))), Image.LANCZOS
            )
        logo_h = logo_layer.height if logo_layer is not None else 0
        stack_h = sum(l.height for l in layers) + gap * (len(layers) - 1)
        total_h = logo_h + logo_gap + stack_h

    top = h / 2 - total_h / 2
    logo_cy = top + logo_h / 2 if logo_layer is not None else None
    positions = []
    cy = top + logo_h + logo_gap + (layers[0].height / 2 if layers else 0)
    for layer in layers:
        positions.append(cy)
        cy += layer.height + gap

    def frames():
        for idx in range(math.ceil(duration * FPS) + 2):
            t = idx / FPS
            canvas = Image.new("RGBA", resolution, bg_color)
            if logo_layer is not None and logo_cy is not None:
                _place(canvas, logo_layer, t, 0.0, w // 2, int(logo_cy))
            for i, (layer, pos) in enumerate(zip(layers, positions)):
                _place(canvas, layer, t, 0.05 * (i + 1), w // 2, int(pos))
            yield canvas

    out_path = Path(work_dir) / f"block_{index:02d}_card.mp4"
    return _encode_frames(out_path, resolution, FPS, frames(), transparent=False)


def _cta_card(
    text: str, font_path: str, resolution: tuple[int, int], duration: float,
    work_dir: Path, index: int, *, logo_path: Path | None = None,
) -> Path:
    """CTA block ("overlay" type): the same stamped CTA badge the punch-text
    overlay uses elsewhere, on its own branded closing card (a "logo-outro")
    rather than composited over footage — simpler for v1, and avoids a
    freeze-frame of whichever clip happened to be last."""
    w, h = resolution
    safe_width = w * 0.85
    fontsize = max(1, min(round(h * 0.06), round(safe_width / (max(len(text), 1) * 0.6))))
    badge = _render_cta_badge((text or "").upper(), font_path, fontsize)

    logo_layer = _logo_layer(logo_path, int(w * 0.24)) if logo_path else None

    def frames():
        for idx in range(math.ceil(duration * FPS) + 2):
            t = idx / FPS
            canvas = Image.new("RGBA", resolution, BG_DARK)
            if logo_layer is not None:
                _place(canvas, logo_layer, t, 0.0, w // 2, int(h * 0.40))
                _place(canvas, badge, t, 0.08, w // 2, int(h * 0.62))
            else:
                _place(canvas, badge, t, 0.0, w // 2, h // 2)
            yield canvas

    out_path = Path(work_dir) / f"block_{index:02d}_cta.mp4"
    return _encode_frames(out_path, resolution, FPS, frames(), transparent=False)


def _apply_logo_overlay(
    segment_path: Path, logo_path: Path | None, business_name: str, font_path: str,
    resolution: tuple[int, int], duration: float, work_dir: Path, index: int,
) -> Path:
    """Stamps the brand logo (+ business name) as a badge over the back
    ~40% of a HERO footage segment's own duration — see module docstring.
    Returns `segment_path` unchanged if there's neither a logo nor a
    business name to show (a blank badge would just be a flash of nothing).

    Uses the segment's own PROBED duration, not the block's nominal
    `duration` — a source clip shorter than its assigned block duration
    (_trim_and_normalize_clip can't pad a short real video the way it pads
    a still image, see that function's own docstring) still gets trimmed to
    whatever length is actually available. Building the badge for the
    nominal (longer) duration instead would outlast the footage, and
    ffmpeg's overlay filter then repeats the footage's last frame for the
    difference (its default eof_action) — a silent freeze-frame tail with
    no footage-shortfall warning anywhere. Probing avoids that outright."""
    actual_duration = probe_duration(segment_path)
    if abs(actual_duration - duration) > 0.05:
        print(
            f"[format001] block {index} (logo overlay): source clip shorter than its "
            f"assigned {duration:.2f}s — segment is only {actual_duration:.2f}s; "
            f"badge timed to the real length, not the shortfall",
            flush=True,
        )
    duration = actual_duration

    w, h = resolution
    logo_layer = _logo_layer(logo_path, int(w * 0.30)) if logo_path and logo_path.exists() else None

    text_layer = None
    label = (business_name or "").strip().upper()
    if label:
        fontsize = max(min(int(h * 0.04), int(w * 0.55 / (max(len(label), 1) * 0.6))), 22)
        text_layer = _render_pop_word_layer(label, font_path, fontsize, WHITE)

    if logo_layer is None and text_layer is None:
        return segment_path

    pop_start = duration * 0.6  # badge appears for the back ~40% of the block

    def frames():
        for idx in range(math.ceil(duration * FPS) + 2):
            t = idx / FPS
            canvas = Image.new("RGBA", resolution)
            if t >= pop_start:
                if logo_layer is not None:
                    _place(canvas, logo_layer, t, pop_start, w // 2, int(h * 0.82))
                if text_layer is not None:
                    _place(canvas, text_layer, t, pop_start + 0.05, w // 2, int(h * 0.91))
            yield canvas

    badge_path = _encode_frames(Path(work_dir) / f"block_{index:02d}_badge.mov", resolution, FPS, frames(), True)

    out_path = Path(work_dir) / f"block_{index:02d}_hero_overlay.mp4"
    _run([
        "ffmpeg", "-y",
        "-i", str(segment_path), "-i", str(badge_path),
        "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
        "-map", "[out]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path),
    ])
    return out_path


def render_format_001_edl(edl: EDL, assets_dir: Path, output_path: Path, work_dir: Path | None = None) -> Path:
    if not edl.blocks:
        raise ValueError("render_format_001_edl called on an EDL with no blocks — build it with edl.format_001.build_format_001_edl")

    resolution = FORMAT_RESOLUTIONS[edl.format]
    renderer = Renderer(assets_dir=assets_dir, work_dir=work_dir)
    font_path = _resolve_punch_font(edl)
    logo_path = (renderer.assets_dir / edl.logo) if edl.logo else None

    segments: list[Path] = []
    for i, block in enumerate(edl.blocks):
        if block.type == "footage":
            seg = renderer._trim_and_normalize_clip(block.content, block.duration, i, resolution, ken_burns=False)
            seg_duration = probe_duration(seg)
            if seg_duration < block.duration - 0.05:
                # _trim_and_normalize_clip can only CAP a real video clip's
                # length ("-t"), never pad one that's shorter than the block
                # calls for (only a still image gets looped — see that
                # function's own docstring). A thin upload silently shrinks
                # Format 001's fixed total duration unless this is visible
                # somewhere; printing it here means it shows up in Render's
                # log stream the same way the font diagnostics do.
                print(
                    f"[format001] block {i} '{block.role}' ({block.content}): wanted "
                    f"{block.duration:.2f}s but the source clip is only {seg_duration:.2f}s long "
                    f"— segment trimmed short, total render duration will come in under the "
                    f"intended {edl.duration:.2f}s",
                    flush=True,
                )
            if block.logo_overlay:
                seg = _apply_logo_overlay(
                    seg, logo_path, edl.business_name, font_path, resolution, block.duration, renderer.work_dir, i,
                )
        elif block.type == "logo":
            seg = _text_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i, logo_path=logo_path)
        elif block.type == "overlay":
            seg = _cta_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i, logo_path=logo_path)
        else:  # "tekstkaart"
            seg = _text_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i)
        segments.append(seg)

    assembled = renderer._concat_clips(segments)
    actual_duration = probe_duration(assembled)  # per-segment "-t" trims round to frame boundaries; don't trust edl.duration blindly for the audio fade math
    finished = renderer._add_music(assembled, edl, duration=actual_duration)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        finished.replace(output_path)
    except OSError:
        import shutil
        shutil.copy2(finished, output_path)
        finished.unlink()
    return output_path
