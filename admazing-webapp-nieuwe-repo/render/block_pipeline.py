"""
Format 001 renderer: turns a block-based EDL (edl.blocks — see
edl/format_001.py) into one MP4. As of Oct 17 2026 this is the ONLY render
path in the app — see edl/format_001.py's module docstring for the client
instruction that triggered this ("Format001 moet weg want elke edit moet
standaard de Format001 hebben. [...] Flash, zoom etc is de edit stijl en
daar alleen kan de ondernemer uit kiezen."). `edl.template` no longer means
"which render pipeline" (there is only one now) — it means which of the six
EDIT STYLES (Papercut/Rotator/Splitter/Shake/Flash/Zoom, still defined in
render/templates/*.py exactly as before) is applied to the FOOTAGE blocks
inside this fixed timeline. render/pipeline.py's Renderer/render_edl/TEMPLATES
dispatch is no longer called from webapp/app.py at all; this module reuses
its Renderer class (trim/concat/text/music helpers) and reuses each edit
style's own `assemble()` (or, for the three "burst" styles, their per-clip
helper functions directly) rather than duplicating any of that logic.

Each non-footage block still becomes its own normalized segment exactly as
before (tekstkaart/overlay cards — see _text_card/_cta_card below). Footage
blocks are now handled per CONTIGUOUS RUN instead of one at a time, because
an edit style needs to know about a run's internal structure:

  - "Burst" styles (Shake/Flash/Zoom) only ever need the ONE clip they're
    cutting into — no pairing required. Every footage block in a run gets
    that style's own per-clip burst treatment (imported straight from
    render/templates/shake.py / flash.py / zoom.py — the exact same
    functions those modules' own assemble() already calls), except the
    FIRST block of each run, which stays a plain cut — mirroring those
    modules' own "first clip is never shaken/flashed/zoomed" rule exactly,
    just reinterpreted as "first block of this footage run" instead of
    "first element of edl.clips".
  - "Pair" styles (Papercut/Rotator/Splitter) need a real second clip to
    crossfade/wipe/split against, so they only make sense across a run of
    2+ consecutive footage blocks — handed off to that style's own
    `assemble()` wholesale (via a small throwaway synthetic EDL containing
    just that run's clips), which already knows how to chain transitions
    across an arbitrary number of clips. A length-1 run (nothing to pair
    with) falls back to a plain cut, same as a burst style's first block.

Every segment (footage run output, text card, CTA card) is still
concatenated via the same ffmpeg concat-demuxer approach
(render.pipeline.Renderer._concat_clips) — which is why every segment here
is encoded with identical codec/pix_fmt/resolution/fps; concat -c copy
silently breaks the moment one segment's encode settings drift from the
others (render/templates/papercut.py has a comment about exactly this).

"footage" blocks reuse Renderer._trim_and_normalize_clip for a plain cut —
no ken_burns: Format 001 is a fixed-rhythm CUT format (that's the whole
point of its block timing), not a continuous-push-in template like the
punch overlay's Ken Burns effect. None of the six edit styles touch
ken_burns either (always called with ken_burns=False) for the same reason.

The HOOK block additionally carries `logo_overlay=True` AND `overlay_text`
(see edl/format_001.py's "Fourth revision") — it now composites BOTH the
hook headline (early, grabs the viewer immediately) and the brand badge
(late, back ~40% — the "early brand moment" measured from John's reference
edit, Jonny's Burgers) onto its own footage, timed so the two never show
at once. "HERO" (the old separate role the badge used to live on) is
retired, folded into HOOK. HOOK and EXPERIENCE are ALWAYS adjacent in
Format 001's base role order, so HOOK is always the first block of a 2+
run — meaning for a "pair" edit style, both overlays have to be composited
onto the WHOLE assembled run (HOOK's clip already merged with
EXPERIENCE's via that style's own transition), not onto a standalone HOOK
segment. See _apply_hook_block_overlays' docstring for how that's handled,
and its known timing-precision caveat for Papercut/Rotator specifically.

"tekstkaart"/"logo"/"overlay" blocks (USP/CTA — HOOK is footage now, see
above) are generated from scratch here: a solid brand-colour background
with text/logo/CTA-badge composited on top via the SAME pop-in motion and
CTA badge render/text_fx.py's punch-text overlay already uses
(_render_pop_word_layer / _render_cta_badge / _place), so Format 001
doesn't introduce a third, inconsistent typography treatment alongside
CAPTURE/pop.

Known simplification (documented, not silently dropped): Papercut's
persistent scrapbook frame treatment (border/tape/cracks/grain/grade — see
render/templates/papercut.py's own module docstring, "that look is what's
on screen for the whole ad") only decorates the footage run(s) it gets
reused on here, via that style's own assemble(). It does NOT currently
extend to the USP/CTA text cards, since those are generated independently
of any edit style. If the client wants the scrapbook look on every block
(not just footage), that's the next thing to add — it would mean applying
papercut._apply_look to the FINAL assembled video instead of per-run.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

from edl.schema import EDL, ClipRef
from render.ffmpeg_utils import probe_duration
from render.ffmpeg_utils import run as _run
from render.pipeline import Renderer, FORMAT_RESOLUTIONS, _resolve_punch_font
from render.templates import flash as _flash
from render.templates import papercut as _papercut
from render.templates import rotator as _rotator
from render.templates import shake as _shake
from render.templates import splitter as _splitter
from render.templates import zoom as _zoom
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

# "Burst" edit styles: operate on ONE clip at a time (the clip being cut
# INTO), regardless of what precedes it — reused straight from each
# style's own per-clip helper function.
_BURST_ASSEMBLERS = {
    "Shake": _shake._render_shaken_clip,
    "Flash": _flash._render_flashed_clip,
    "Zoom": _zoom._render_zoomed_clip,
}
# "Pair" edit styles: need a real second clip to transition against, so
# they only apply across a run of 2+ footage blocks — reused via that
# style's own whole-list assemble().
_PAIR_ASSEMBLERS = {
    "Papercut": _papercut.assemble,
    "Rotator": _rotator.assemble,
    "Splitter": _splitter.assemble,
}


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
    """USP (plain tekstkaart) blocks: every word gets its OWN line,
    stacked top to bottom and popped in with a short stagger. Also still
    used for a "logo" block type if one is ever constructed again (it
    accepts `logo_path`) — Format 001 as of Oct 17 2026 no longer builds a
    standalone BRAND/"logo" block, and HOOK is footage now too (see
    edl/format_001.py's "Fourth revision" — brand badge and hook text both
    show as overlays on the HOOK footage block instead, see
    `_apply_hook_block_overlays` below), so this branch is currently
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


def _apply_hook_block_overlays(
    segment_path: Path, *, headline_text: str, logo_path: Path | None, business_name: str,
    font_path: str, resolution: tuple[int, int], duration: float, work_dir: Path, index: int,
    probe_segment: bool,
) -> Path:
    """Composites BOTH overlays the HOOK block can carry onto its own
    footage, in a single pass (see edl/format_001.py's "Fourth revision",
    client: "Hooktekst moet op de eerste block samen de eerste
    (hookfootage)"):
      - the hook headline itself — pops in almost immediately
        (HOOK_TEXT_POP_IN) so it actually grabs the viewer in the first
        instant, and clears again well before the badge's own window
        starts (HOOK_TEXT_POP_OUT), so the two never show at once.
      - the brand badge (logo + business name) — unchanged from the
        earlier "early brand moment" design: pops in at ~60% through the
        block and holds for the back ~40%.

    `probe_segment=True`: `segment_path` is the HOOK block's own standalone
    clip (a burst edit style, or a run too short to need pairing) — its
    REAL probed duration is used, not the block's nominal `duration` — a
    source clip shorter than its assigned slot (_trim_and_normalize_clip
    can't pad a short real video the way it pads a still image) still gets
    trimmed to whatever length is actually available, and building either
    overlay for the nominal (longer) duration would outlast the footage
    (ffmpeg's overlay filter repeats the shorter input's last frame past
    its end, a silent freeze-frame tail with no shortfall warning anywhere
    otherwise).

    `probe_segment=False`: `segment_path` is a multi-block "pair" edit
    style run (HOOK merged with EXPERIENCE by Papercut/Rotator/Splitter —
    HOOK and EXPERIENCE are ALWAYS adjacent in Format 001's base role
    order, so this is the path every pair style actually takes, not an
    edge case). This can't probe the segment's own length to know "how
    long is HOOK on screen" — `duration` (HOOK's own nominal
    block.duration) is used instead, and both layers are windowed to only
    [0, duration] of the run via an ffmpeg `enable=` expression, so neither
    overlay bleeds into EXPERIENCE's own footage once HOOK's own span ends.
    Known simplification: this windowing is approximate, not exact, for
    Papercut and Rotator — both borrow a small overlap (<=0.4s) off HOOK's
    tail for their transition into EXPERIENCE, so the badge in particular
    can read as popping in a few tenths of a second into EXPERIENCE's
    footage rather than right at HOOK's own end. Exact for Splitter, whose
    pair plays for exactly top_clip.duration (= HOOK's own duration) with
    no borrowed overlap. Fine for v1 — the badge's own "vette animatie" is
    still an open spec item from the client anyway (see edl/format_001.py).

    Returns `segment_path` unchanged if there's neither headline text, a
    logo, nor a business name to show."""
    if probe_segment:
        actual_duration = probe_duration(segment_path)
        if abs(actual_duration - duration) > 0.05:
            print(
                f"[format001] block {index} (hook overlays): source clip shorter than its "
                f"assigned {duration:.2f}s — segment is only {actual_duration:.2f}s; "
                f"overlays timed to the real length, not the shortfall",
                flush=True,
            )
        duration = actual_duration

    w, h = resolution
    logo_layer = _logo_layer(logo_path, int(w * 0.30)) if logo_path and logo_path.exists() else None

    biz_text_layer = None
    label = (business_name or "").strip().upper()
    if label:
        fontsize = max(min(int(h * 0.04), int(w * 0.55 / (max(len(label), 1) * 0.6))), 22)
        biz_text_layer = _render_pop_word_layer(label, font_path, fontsize, WHITE)

    hook_words = (headline_text or "").upper().split()
    hook_safe_width = w * 0.82
    hook_layers = [
        _render_pop_word_layer(
            word, font_path,
            max(min(int(h * 0.09), int(hook_safe_width / (max(len(word), 1) * 0.58))), 40),
            WHITE,
        )
        for word in hook_words
    ]

    if logo_layer is None and biz_text_layer is None and not hook_layers:
        return segment_path

    HOOK_POP_IN = 0.08              # seconds — near-immediate, this IS the attention-grab
    HOOK_POP_OUT = duration * 0.55  # clears comfortably before the badge's own window starts
    BADGE_POP_IN = duration * 0.6   # unchanged from the original "early brand moment" timing

    hook_gap = h * 0.02
    hook_stack_h = sum(l.height for l in hook_layers) + hook_gap * (len(hook_layers) - 1) if hook_layers else 0
    hook_top = h * 0.40 - hook_stack_h / 2

    def frames():
        for idx in range(math.ceil(duration * FPS) + 2):
            t = idx / FPS
            canvas = Image.new("RGBA", resolution)
            if hook_layers and HOOK_POP_IN <= t < HOOK_POP_OUT:
                cy = hook_top + (hook_layers[0].height / 2)
                for i, layer in enumerate(hook_layers):
                    _place(canvas, layer, t, HOOK_POP_IN + 0.05 * i, w // 2, int(cy))
                    cy += layer.height + hook_gap
            if t >= BADGE_POP_IN:
                if logo_layer is not None:
                    _place(canvas, logo_layer, t, BADGE_POP_IN, w // 2, int(h * 0.82))
                if biz_text_layer is not None:
                    _place(canvas, biz_text_layer, t, BADGE_POP_IN + 0.05, w // 2, int(h * 0.91))
            yield canvas

    overlay_path = _encode_frames(Path(work_dir) / f"block_{index:02d}_hookoverlay.mov", resolution, FPS, frames(), True)

    out_path = Path(work_dir) / f"block_{index:02d}_hook_composited.mp4"
    if probe_segment:
        filter_complex = "[0:v][1:v]overlay=0:0[out]"
    else:
        filter_complex = f"[0:v][1:v]overlay=0:0:enable='lt(t,{duration:.3f})'[out]"
    _run([
        "ffmpeg", "-y",
        "-i", str(segment_path), "-i", str(overlay_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        str(out_path),
    ])
    return out_path


def _assemble_pair_run(
    renderer: Renderer, style: str, run_blocks: list, resolution: tuple[int, int],
    base_index: int, edl_format: str,
) -> Path:
    """Runs a contiguous footage run of 2+ blocks through one of the three
    "pair" edit styles' own assemble(), via a small throwaway EDL holding
    just this run's clips — reusing render/templates/papercut.py /
    rotator.py / splitter.py wholesale instead of reimplementing any of
    their pairwise transition logic here.

    Gets its OWN isolated sub-work-dir (renderer.work_dir / "run_NN"),
    not the shared top-level one: every style module writes fixed or
    index-from-zero filenames into whatever work_dir it's given (e.g.
    papercut.py's "papercut_look.mp4", or "tear_00.mp4" for the first
    transition in ANY call) — Format 001 can have TWO separate pair-style
    runs in one render (HERO+EXPERIENCE, and DESIRE+any extra MOMENT
    blocks), and without separate sub-directories the second call would
    silently overwrite the first run's output files before the final
    concat ever reads them."""
    clips = [ClipRef(start=0.0, end=b.duration, source=b.content) for b in run_blocks]
    synthetic_edl = EDL(template=style, format=edl_format, clips=clips)  # type: ignore[arg-type]

    run_renderer = Renderer(assets_dir=renderer.assets_dir, work_dir=renderer.work_dir / f"run_{base_index:02d}")
    assemble_fn = _PAIR_ASSEMBLERS[style]
    return assemble_fn(run_renderer, synthetic_edl, resolution, ken_burns=False)


def _render_footage_run(
    renderer: Renderer, style: str, run_blocks: list, run_start_index: int,
    resolution: tuple[int, int], *, business_name: str, font_path: str,
    logo_path: Path | None, edl_format: str,
) -> list[Path]:
    """Builds the segment(s) for one contiguous run of `footage` blocks, in
    the chosen edit style — see module docstring for the burst-vs-pair
    split. Always returns a list the caller can .extend() straight into its
    overall segments list: length == len(run_blocks) for burst styles (and
    for any length-1 run), length == 1 for a pair-style run of 2+ blocks
    (that style's assemble() already concatenates the whole run into one
    Path internally)."""
    n = len(run_blocks)

    if style in _PAIR_ASSEMBLERS and n >= 2:
        combined = _assemble_pair_run(renderer, style, run_blocks, resolution, run_start_index, edl_format)
        if run_blocks[0].logo_overlay or run_blocks[0].overlay_text:
            combined = _apply_hook_block_overlays(
                combined, headline_text=run_blocks[0].overlay_text, logo_path=logo_path,
                business_name=business_name, font_path=font_path, resolution=resolution,
                duration=run_blocks[0].duration, work_dir=renderer.work_dir, index=run_start_index,
                probe_segment=False,
            )
        return [combined]

    # Burst styles, or a run too short (1 block) for any style's pairwise
    # logic to apply to — handled per block.
    segments: list[Path] = []
    for offset, block in enumerate(run_blocks):
        index = run_start_index + offset
        if offset == 0 or style not in _BURST_ASSEMBLERS:
            # The first block of a run never gets a burst (mirrors every
            # burst style's own "first clip is never shaken/flashed/zoomed"
            # rule — see shake.py/flash.py/zoom.py's assemble()). A pair
            # style with nothing to pair against (n == 1) falls back to the
            # exact same plain cut.
            seg = renderer._trim_and_normalize_clip(block.content, block.duration, index, resolution, ken_burns=False)
        else:
            clip = ClipRef(start=0.0, end=block.duration, source=block.content)
            seg = _BURST_ASSEMBLERS[style](renderer, clip, resolution, index)

        seg_duration = probe_duration(seg)
        if seg_duration < block.duration - 0.05:
            # _trim_and_normalize_clip (and the burst helpers, which also
            # end in a trimmed "rest" segment) can only CAP a real video
            # clip's length, never pad one shorter than the block calls
            # for. A thin upload silently shrinks Format 001's fixed total
            # duration unless this is visible somewhere; printing it here
            # means it shows up in Render's log stream the same way the
            # font diagnostics do.
            print(
                f"[format001] block {index} '{block.role}' ({block.content}): wanted "
                f"{block.duration:.2f}s but the source clip is only {seg_duration:.2f}s long "
                f"— segment trimmed short, total render duration will come in under the "
                f"intended total",
                flush=True,
            )
        if block.logo_overlay or block.overlay_text:
            seg = _apply_hook_block_overlays(
                seg, headline_text=block.overlay_text, logo_path=logo_path,
                business_name=business_name, font_path=font_path, resolution=resolution,
                duration=block.duration, work_dir=renderer.work_dir, index=index,
                probe_segment=True,
            )
        segments.append(seg)

    return segments


def render_format_001_edl(edl: EDL, assets_dir: Path, output_path: Path, work_dir: Path | None = None) -> Path:
    if not edl.blocks:
        raise ValueError("render_format_001_edl called on an EDL with no blocks — build it with edl.format_001.build_format_001_edl")

    resolution = FORMAT_RESOLUTIONS[edl.format]
    renderer = Renderer(assets_dir=assets_dir, work_dir=work_dir)
    font_path = _resolve_punch_font(edl)
    logo_path = (renderer.assets_dir / edl.logo) if edl.logo else None
    style = edl.template  # the chosen edit style — see module docstring

    segments: list[Path] = []
    blocks = edl.blocks
    n = len(blocks)
    i = 0
    while i < n:
        block = blocks[i]
        if block.type == "footage":
            # Collect the whole contiguous run of footage blocks starting
            # here (HOOK+EXPERIENCE, or DESIRE+any extra MOMENT blocks —
            # see edl/format_001.py's role ordering; never more than those
            # two runs in one EDL, but this loop doesn't assume that).
            run_start = i
            j = i
            while j < n and blocks[j].type == "footage":
                j += 1
            run_blocks = blocks[run_start:j]
            segments.extend(
                _render_footage_run(
                    renderer, style, run_blocks, run_start, resolution,
                    business_name=edl.business_name, font_path=font_path,
                    logo_path=logo_path, edl_format=edl.format,
                )
            )
            i = j
        else:
            if block.type == "logo":
                seg = _text_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i, logo_path=logo_path)
            elif block.type == "overlay":
                seg = _cta_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i, logo_path=logo_path)
            else:  # "tekstkaart"
                seg = _text_card(block.content, font_path, resolution, block.duration, renderer.work_dir, i)
            segments.append(seg)
            i += 1

    assembled = renderer._concat_clips(segments)
    actual_duration = probe_duration(assembled)  # per-segment trims round to frame boundaries, and Rotator/Splitter runs legitimately shorten the total — don't trust edl.duration blindly for the audio fade math
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
