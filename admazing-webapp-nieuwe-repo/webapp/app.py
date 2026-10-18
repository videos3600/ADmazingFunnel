"""
Standalone upload-funnel test tool (bouwplan: "kleinste ding om écht te
testen" — zie de sectie daarover). Deliberately NOT a WordPress plugin: this
is a small, self-contained Flask app that wraps the existing EDL/render
pipeline in exactly the funnel steps from the concept doc's MVP-scope
("Homepage -> Upload -> bedrijfsinformatie -> template -> Generate ->
Preview -> Download" — checkout left out on purpose, see the bouwplan).

Render runs in a BACKGROUND THREAD, not inside the /generate request: a
real render (multiple phone clips + logo + punch text + music) regularly
takes well past the ~30s a web proxy/gateway (and Render.com's own default)
will hold a single HTTP request open, and a dropped mobile connection during
a long synchronous request kills the whole upload with it. /generate now
only validates + saves the upload (fast) and returns a job_id; the browser
polls /status/<job_id> instead of waiting on one open connection, so a
flaky mobile network just means a missed poll, not a lost render — the job
keeps running server-side either way. Job state lives in a status.json file
per job (not an in-memory dict) so it's read correctly even if Render ever
runs more than one gunicorn worker process.
"""

from __future__ import annotations

import json
import sys
import shutil
import threading
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# simple_generator's build_simple_edl is no longer wired up anywhere (see
# edl/format_001.py's "Third revision" docstring note) — MIN_CLIP_DURATION
# is still used below for upload validation, independent of which EDL
# builder ends up running.
from edl.simple_generator import MIN_CLIP_DURATION  # noqa: E402
from edl.format_001 import build_format_001_edl, MAX_USED_CLIPS  # noqa: E402
from edl.schema import EDL  # noqa: E402
from webapp.logo_cleanup import clean_logo_background  # noqa: E402
from render.ffmpeg_utils import probe_duration, probe_resolution, run as ffmpeg_run  # noqa: E402
from render.pipeline import FORMAT_RESOLUTIONS, _resolve_punch_font  # noqa: E402
from render.block_pipeline import render_format_001_edl  # noqa: E402
from render.templates import TEMPLATES  # noqa: E402

# Every ad is Format 001's block structure now, always (see
# edl/format_001.py) — TEMPLATES (render/templates/__init__.py) is the
# dropdown's ONLY source of valid values these days: the six EDIT STYLES
# (Papercut/Rotator/Splitter/Shake/Flash/Zoom), no longer a separate
# "Format001" choice alongside them. DEFAULT_EDIT_STYLE is a deliberate,
# explicit pick (NOT "whatever sorts first") — see index()'s own comment
# for the bug that bit this app the last time a dropdown default was left
# implicit.
DEFAULT_EDIT_STYLE = "Papercut"

# Longest side an uploaded clip/photo is allowed to keep — a modern phone
# easily uploads 4K video or a 12MP photo, both far bigger than any format
# this app outputs (max 1920 on the long side). Downscaling up front cuts
# memory/CPU for every later step (trim, crossfade, text overlay, encode)
# and shrinks how much the background job has to chew through.
MAX_UPLOAD_DIMENSION = 1080

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
RESULT_DIR = APP_DIR / "results"
UPLOAD_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

MUSIC_DIR = ROOT / "assets" / "music"
FONTS_DIR = ROOT / "assets" / "fonts"

# Display name per font file in assets/fonts/ (or assets/fonts/<categorie>/)
# — same self-service idea as MUSIC_DIR: drop a new .ttf in there and it
# shows up here automatically (falls back to the bare filename stem for one
# with no label yet, same as a music track with no entry in GENRE_LABELS).
FONT_LABELS = {
    "Anton-Regular": "Anton (condensed, huidige standaard)",
    "ArchivoBlack-Regular": "Archivo Black",
    "BebasNeue-Regular": "Bebas Neue",
    "Staatliches-Regular": "Staatliches",
    "AlfaSlabOne-Regular": "Alfa Slab One",
    "BlackOpsOne-Regular": "Black Ops One",
    "Bungee-Regular": "Bungee",
    "Oswald-Regular": "Oswald",
    "Montserrat-Regular": "Montserrat Black",
    "Righteous-Regular": "Righteous",
    "LuckiestGuy-Regular": "Luckiest Guy",
    "PermanentMarker-Regular": "Permanent Marker",
    "Pacifico-Regular": "Pacifico",
    "PlayfairDisplay-Regular": "Playfair Display",
    "AbrilFatface-Regular": "Abril Fatface",
}

# Display label per category-folder (assets/fonts/<categorie>/) — same
# taxonomy idea as GENRE_LABELS below, just for lettertypen. "stoer" is
# also where Anton/ArchivoBlack are grouped in the dropdown even though
# those two still live loose at the top of assets/fonts/ (see
# _LEGACY_FONT_CATEGORY) — nothing had to move for that.
FONT_CATEGORY_LABELS = {
    "stoer": "Stoer / Impact",
    "strak": "Strak / Zakelijk",
    "speels": "Speels",
    "handgeschreven": "Handgeschreven",
    "elegant": "Elegant",
    "overig": "Overig",
}
_FONT_CATEGORY_ORDER = ("stoer", "strak", "speels", "handgeschreven", "elegant", "overig")
_LEGACY_FONT_CATEGORY = {
    "Anton-Regular": "stoer",
    "ArchivoBlack-Regular": "stoer",
}

# text_style="punch" animation templates — render/text_fx.py's
# render_punch_overlay() dispatches on edl.text_animation between these two.
# (key, label, description) — the description is what tells the two apart
# in the picker, since "CAPTURE" / "Rustig" alone doesn't.
TEXT_ANIMATIONS = [
    ("capture", "CAPTURE", "Per letter opbouw, cascade en glitch — druk en opvallend"),
    ("pop", "Rustig (pop-in)", "Eén woord per keer, zachte bounce — strakker en leesbaarder"),
]
DEFAULT_TEXT_ANIMATION = TEXT_ANIMATIONS[0][0]

# "Stijl" replaces the separate font-picker + tekst-animatie-picker below
# with ONE choice. Product decision (confirmed with the client, Oct 2026):
# a 12-font picker grouped into 5 categories, PLUS a separate animation
# toggle, asks the ondernemer to play grafisch ontwerper ("Dan hebben we
# gewoon Canva opnieuw gebouwd"). The format/stijl should decide the
# typography, not the user: "De gebruiker kiest dus hooguit: Punchy / Clean
# / Premium." Each style bundles a font + a text_animation (and later could
# bundle more, e.g. a per-style motion treatment) behind one name.
# FONT_LIBRARY/FONT_GROUPS above stay in place unused by this picker — the
# client's own note: "Later kan Studio eventueel een beperkte fontoverride
# krijgen", i.e. a possible future feature, not v1.
# (key, label, description, font_file, text_animation)
STYLES = [
    ("punchy", "Punchy", "Stevig, uppercase, cascade-opbouw — voor een scherpe aanbieding", "stoer/BebasNeue-Regular.ttf", "capture"),
    ("clean", "Clean", "Rustig en strak — voor een zakelijke, nette uitstraling", "strak/Oswald-Regular.ttf", "pop"),
    ("premium", "Premium", "Subtiel en verfijnd — voor een premium uitstraling", "elegant/PlayfairDisplay-Regular.ttf", "pop"),
]
DEFAULT_STYLE_KEY = STYLES[0][0]
STYLE_LIBRARY = {key: (font_file, text_animation) for key, _, _, font_file, text_animation in STYLES}

# Display name per genre-folder — John's own taxonomy (assets/music/<slug>/),
# built from his real catalog (he produces his own music; some tracks are
# licensed Freepik/Magnific library tracks, covered by his Premium plan —
# no per-ad attribution required). Falls back to the two synthesized beds
# (see assets/music_placeholder*.m4a) only if that folder is ever empty.
GENRE_LABELS = {
    "dance": "Dance",
    "exotic": "Exotic",
    "funk": "Funk",
    "hiphop": "Hiphop",
    "jazz": "Jazz",
    "pop": "Pop",
    "rnb": "R&B",
    "vrolijk": "Vrolijk",
    "zakelijk": "Zakelijk",
}


def _load_music_groups():
    """Returns [(genre_label, [(key, track_label), ...]), ...], and the flat
    {key: Path} lookup used at render time — key is "<genre-slug>/<filename
    stem>", e.g. "dance/Afro House"."""
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    flat: dict[str, Path] = {}

    if MUSIC_DIR.exists():
        for genre_dir in sorted(MUSIC_DIR.iterdir()):
            if not genre_dir.is_dir():
                continue
            tracks = sorted(genre_dir.glob("*.m4a"))
            if not tracks:
                continue
            label = GENRE_LABELS.get(genre_dir.name, genre_dir.name.capitalize())
            entries = []
            for f in tracks:
                key = f"{genre_dir.name}/{f.stem}"
                flat[key] = f
                entries.append((key, f.stem))
            groups.append((label, entries))

    if not flat:
        # Defensive fallback so /generate never has zero music options.
        flat = {
            "default/Upbeat": ROOT / "assets" / "music_placeholder.m4a",
            "default/Chill": ROOT / "assets" / "music_placeholder_alt.m4a",
        }
        groups = [("Muziek", [(k, k.split("/", 1)[1]) for k in flat])]

    return groups, flat


def _load_fonts():
    """Returns [(category_label, [(key, label), ...]), ...] (same shape as
    MUSIC_GROUPS, so the template reuses the exact same optgroup loop) and
    the flat {key: relative-path-string} lookup used at render time.

    key is the path under assets/fonts/ — either a bare filename for the
    two legacy loose files ("Anton-Regular.ttf") or "<categorie>/filename"
    for anything in a category subfolder ("stoer/BebasNeue-Regular.ttf").
    render/pipeline.py resolves either shape the same way, via
    FONTS_DIR / edl.font, so adding a category never requires moving an
    existing file."""
    groups: dict[str, list[tuple[str, str]]] = {}
    flat: dict[str, str] = {}

    if FONTS_DIR.exists():
        for f in sorted(FONTS_DIR.glob("*.ttf")):
            category = _LEGACY_FONT_CATEGORY.get(f.stem, "overig")
            label = FONT_LABELS.get(f.stem, f.stem)
            flat[f.name] = f.name
            groups.setdefault(category, []).append((f.name, label))
        for cat_dir in sorted(FONTS_DIR.iterdir()):
            if not cat_dir.is_dir():
                continue
            for f in sorted(cat_dir.glob("*.ttf")):
                label = FONT_LABELS.get(f.stem, f.stem)
                key = f"{cat_dir.name}/{f.name}"
                flat[key] = key
                groups.setdefault(cat_dir.name, []).append((key, label))

    if not flat:
        # Defensive fallback so /generate and the form never see zero fonts
        # — render/pipeline.py's own default (Anton) still applies even if
        # this list is somehow empty, this is just for the dropdown.
        return [("Lettertype", [("", "Standaard")])], {"": ""}

    ordered = [
        (FONT_CATEGORY_LABELS.get(key, key.capitalize()), groups.pop(key))
        for key in _FONT_CATEGORY_ORDER
        if key in groups
    ]
    ordered += [(FONT_CATEGORY_LABELS.get(key, key.capitalize()), entries) for key, entries in groups.items()]
    return ordered, flat


MUSIC_GROUPS, MUSIC_LIBRARY = _load_music_groups()
DEFAULT_MUSIC_KEY = next(iter(MUSIC_LIBRARY))

FONT_GROUPS, FONT_LIBRARY = _load_fonts()
DEFAULT_FONT_KEY = "Anton-Regular.ttf" if "Anton-Regular.ttf" in FONT_LIBRARY else next(iter(FONT_LIBRARY))

ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024  # 400MB — a handful of raw phone clips


def _downscale_video_if_needed(path: Path, max_dim: int = MAX_UPLOAD_DIMENSION) -> None:
    """Re-encodes in place only if the clip is actually bigger than max_dim
    on its long side — most desktop-sourced test clips already are smaller,
    and re-encoding those would just waste time for nothing. Writes to a
    sibling file and renames over the original so a crash mid-encode never
    leaves a half-written file at the real path.

    The temp file keeps the ORIGINAL suffix — clip_filenames (and the EDL
    blocks built from it, before this function ever runs) reference that
    exact filename, so renaming the extension here would leave the EDL
    pointing at a file that no longer exists. Which means the video codec
    has to match what THAT container can actually hold, not just
    "libx264 for everything": a .webm upload (e.g. a browser MediaRecorder
    clip, vp9/opus) re-encoded with libx264 but still written into a .webm
    container fails outright — WebM's muxer only accepts VP8/VP9/AV1 video
    ("Could not write header (incorrect codec parameters ?)" / "Only VP8 or
    VP9 or AV1 video ... are supported for WebM."). .mp4/.mov/.m4v all
    happily hold H.264, so only .webm needs the VP9 branch below.
    """
    try:
        w, h = probe_resolution(path)
    except Exception:
        return  # unreadable/corrupt — let the real render step report it clearly
    if max(w, h) <= max_dim:
        return
    vf = f"scale={max_dim}:-2" if w >= h else f"scale=-2:{max_dim}"
    tmp = path.with_name(path.stem + "_ds" + path.suffix)
    if path.suffix.lower() == ".webm":
        # -deadline realtime -cpu-used 8: libvpx-vp9's fastest settings —
        # without them VP9 defaults to a far slower encode than libx264's
        # veryfast, and this can run against a multi-minute raw upload.
        video_args = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-deadline", "realtime", "-cpu-used", "8"]
    else:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    ffmpeg_run([
        "ffmpeg", "-y", "-i", str(path),
        "-vf", vf, *video_args,
        "-c:a", "copy",
        str(tmp),
    ])
    tmp.replace(path)


def _downscale_image_if_needed(path: Path, max_dim: int = MAX_UPLOAD_DIMENSION) -> None:
    """Same idea as _downscale_video_if_needed but for a photo upload — a
    12MP+ phone photo held at -loop 1 for several seconds of video encoding
    is needless work when nothing in this app ever outputs more than
    1920px on the long side."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            if max(img.size) <= max_dim:
                return
            img = img.convert("RGB") if img.mode in ("P", "CMYK") else img
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img.save(path)
    except Exception:
        return  # unreadable/corrupt — let the real render step report it clearly


def _status_path(job_dir: Path) -> Path:
    return job_dir / "status.json"


def _write_status(job_dir: Path, **fields) -> None:
    """Whole-file overwrite via a temp file + rename, so a /status/<job_id>
    read never sees a half-written JSON file — this is the only state a
    poll reads, so it has to survive being read from a different request
    (and, if Render ever runs >1 gunicorn worker, a different process) than
    the one that's writing it."""
    path = _status_path(job_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fields))
    tmp.replace(path)


def _read_status(job_dir: Path) -> dict | None:
    path = _status_path(job_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None  # caught mid-write by a previous version without the tmp+rename — treat as "not ready yet"


def _run_render_job(
    job_id: str, job_dir: Path, edl: EDL, output_path: Path, clip_filenames: list[str],
    logo_cleanup_status: str = "", unused_clip_count: int = 0,
) -> None:
    """The actual render, now off the request thread. Every step that can
    raise is inside this try block so a crash always reaches status.json
    instead of silently killing a bare background thread."""
    try:
        _write_status(job_dir, status="running")
        for fname in clip_filenames:
            ext = Path(fname).suffix.lower()
            fpath = job_dir / fname
            if ext in ALLOWED_VIDEO_EXT:
                _downscale_video_if_needed(fpath)
            elif ext in ALLOWED_IMAGE_EXT:
                _downscale_image_if_needed(fpath)
        # Every ad is Format 001's block structure now (see edl/format_001.py's
        # "Third revision" note) — this is the only render entry point left.
        render_format_001_edl(edl, assets_dir=job_dir, output_path=output_path, work_dir=job_dir / "_render_work")
        # Echoes exactly which font file this render actually used (not just
        # which key the form submitted) — so "I picked font X but got Anton"
        # is answerable from the status response alone, no Render log access
        # or code reading needed. Mirrors render/pipeline.py's own fallback
        # logic 1:1 (see _resolve_punch_font's print log for the same info
        # in the deploy logs).
        resolved_font_path = _resolve_punch_font(edl)
        # The REAL final duration, not edl.duration's nominal block-count
        # estimate — Rotator/Splitter edit styles legitimately produce a
        # shorter video than the naive block-duration sum once they're
        # applied to a footage run (see render/block_pipeline.py), so
        # trusting edl.duration here would over-report length for those two
        # styles specifically. Probing the actual rendered file is correct
        # for every edit style, not just a correction for those two.
        actual_video_duration = probe_duration(output_path)
        _write_status(
            job_dir,
            status="done",
            video_url=f"/result/{job_id}.mp4",
            font_requested=edl.font or "(standaard)",
            font_used=Path(resolved_font_path).name,
            # Same self-serve idea as font_requested/font_used above — "why
            # does my logo still have a white box" shouldn't need a Render
            # log lookup. See webapp/logo_cleanup.py for what each status
            # value means.
            logo_cleanup=logo_cleanup_status or "(geen logo)",
            # Block count Format 001 ended up building (see
            # edl/format_001.py's dynamic scaling) alongside the real,
            # probed final duration (see actual_video_duration above) — the
            # browser's own live estimate (webapp/static/app.js) uses the
            # same block-count arithmetic ahead of time; this is the
            # confirmed, post-render number.
            block_count=len(edl.blocks),
            video_duration=actual_video_duration,
            unused_clip_count=unused_clip_count,
        )
    except Exception as exc:  # noqa: BLE001 — surface a readable error to the polling UI
        traceback.print_exc()
        _write_status(job_dir, status="error", error=f"Render mislukt: {exc}")
    finally:
        work_dir = job_dir / "_render_work"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def _font_css_family(key: str) -> str:
    """A stable, CSS-safe @font-face family name for a font library key like
    "stoer/BebasNeue-Regular.ttf" or "Anton-Regular.ttf" — used so the
    homepage can show each option in its own real typeface (the "preview
    van de lettertypen" ask) instead of a plain text label."""
    return "admzfont-" + "".join(ch if ch.isalnum() else "-" for ch in key)


@app.get("/")
def index():
    return render_template(
        "index.html",
        # The dropdown is now just the six edit styles (no more separate
        # "Format001" choice — see edl/format_001.py). DEFAULT_EDIT_STYLE is
        # marked `selected` explicitly in index.html's <option> loop — a
        # past round of this app once left every <option> unmarked and let
        # the browser silently default to whichever name sorted first, which
        # at the time quietly routed every render through a completely
        # unrelated, hardcoded-9-second legacy code path. Not possible
        # anymore now that simple_generator isn't wired up at all, but an
        # explicit `selected` default costs nothing and avoids ever relying
        # on dropdown ordering again.
        templates=sorted(TEMPLATES.keys()),
        default_template=DEFAULT_EDIT_STYLE,
        formats=list(FORMAT_RESOLUTIONS.keys()),
        music_groups=MUSIC_GROUPS,
        styles=STYLES,
        default_style_key=DEFAULT_STYLE_KEY,
        font_css_family=_font_css_family,
    )


@app.get("/fonts/<path:filename>")
def font_file(filename: str):
    """Serves assets/fonts/ to the browser (that folder isn't under
    webapp/static/) purely so the homepage's @font-face preview can load
    the real files — render/pipeline.py never hits this route, it reads
    the same files straight off disk."""
    return send_from_directory(FONTS_DIR, filename)


@app.post("/generate")
def generate():
    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        footage_files = request.files.getlist("footage")
        footage_files = [f for f in footage_files if f and f.filename]
        if not footage_files:
            return jsonify(ok=False, error="Upload minstens één foto of video."), 400

        business_name = (request.form.get("business_name") or "").strip()
        offer = (request.form.get("offer") or "").strip()
        cta_text = (request.form.get("cta_text") or "").strip()
        usp_text = (request.form.get("usp") or "").strip()  # every ad is Format 001 now — see build_format_001_edl's own fallback if left blank
        # Falls back to the same edit style the form's own dropdown defaults
        # to (see index()) — only matters for a request with no "template"
        # field at all (malformed/scripted), since the real form always
        # submits a value now that one <option> is explicitly `selected`.
        template = request.form.get("template") or DEFAULT_EDIT_STYLE
        output_format = request.form.get("format") or "9:16"
        music_choice = request.form.get("music") or "upbeat"
        style_choice = request.form.get("style") or DEFAULT_STYLE_KEY
        if style_choice not in STYLE_LIBRARY:
            style_choice = DEFAULT_STYLE_KEY
        font_filename, text_animation = STYLE_LIBRARY[style_choice]

        if not business_name:
            return jsonify(ok=False, error="Bedrijfsnaam is verplicht."), 400
        if template not in TEMPLATES:
            return jsonify(ok=False, error=f"Onbekende edit stijl '{template}'."), 400
        if output_format not in FORMAT_RESOLUTIONS:
            return jsonify(ok=False, error=f"Onbekend formaat '{output_format}'."), 400

        headline = offer or business_name
        cta = cta_text or "Neem contact op"

        # Save uploaded footage into this job's own assets dir, in the order
        # the browser sent them (== the order the user dropped/selected
        # them) — the simple generator plays clips in that order, since
        # there's no real shot-classification yet (fase 2).
        clip_filenames: list[str] = []
        # Every clip gets assigned a slot of at least MIN_CLIP_DURATION
        # seconds (see edl/simple_generator.py) — a clip shorter than that
        # in real life can't fill its slot, which used to only surface much
        # later as a confusing ffmpeg crash (or a silently too-short ad) deep
        # in the render. Reject it up front instead, with a message the
        # uploader can actually act on.
        too_short: list[str] = []
        for i, f in enumerate(footage_files):
            ext = Path(secure_filename(f.filename)).suffix.lower()
            if ext not in ALLOWED_VIDEO_EXT and ext not in ALLOWED_IMAGE_EXT:
                return jsonify(ok=False, error=f"Bestandstype '{ext}' wordt nog niet ondersteund."), 400
            fname = f"clip_{i:02d}{ext}"
            f.save(job_dir / fname)
            if ext in ALLOWED_VIDEO_EXT:
                try:
                    clip_duration = probe_duration(job_dir / fname)
                except Exception:
                    clip_duration = None  # unreadable/corrupt file — let the render step report it
                if clip_duration is not None and clip_duration < MIN_CLIP_DURATION:
                    too_short.append(f"'{f.filename}' ({clip_duration:.1f}s)")
            clip_filenames.append(fname)

        if too_short:
            return jsonify(
                ok=False,
                error=(
                    f"Deze video('s) zijn korter dan de minimale {MIN_CLIP_DURATION:.0f} seconde per clip: "
                    + ", ".join(too_short)
                    + ". Upload langere beelden (of knip minder clips samen)."
                ),
            ), 400

        # Optional logo — cleaned up (flat background made transparent)
        # right here, before it ever reaches the EDL/renderer, so a plain
        # white-background export from Canva or a screenshot doesn't show
        # up as an ugly box in the render (see webapp/logo_cleanup.py for
        # why a full AI background-removal model was NOT used for this).
        logo_filename = ""
        logo_cleanup_status = ""
        logo_file = request.files.get("logo")
        if logo_file and logo_file.filename:
            ext = Path(secure_filename(logo_file.filename)).suffix.lower()
            if ext in ALLOWED_IMAGE_EXT or ext == ".png":
                saved_path = job_dir / f"logo{ext}"
                logo_file.save(saved_path)
                cleaned_path, logo_cleanup_status = clean_logo_background(saved_path)
                logo_filename = cleaned_path.name

        # Music — placeholder library only (fase 1 openstaande keuze: echte
        # licenties), copied into the job's own asset dir so render_edl's
        # single assets_dir keeps working unchanged.
        music_src = MUSIC_LIBRARY.get(music_choice, MUSIC_LIBRARY[DEFAULT_MUSIC_KEY])
        music_filename = ""
        if music_src.exists():
            music_filename = music_src.name
            shutil.copy2(music_src, job_dir / music_filename)

        # font_filename is already a bare "<categorie>/filename.ttf" key from
        # STYLE_LIBRARY (resolved from the chosen style, not a per-upload
        # file) — render/pipeline.py looks it up in its own assets/fonts/
        # the same way it already resolves the default Anton file, so
        # nothing needs copying into the job's asset dir here.
        # How many uploaded clips Format 001 WON'T use — only relevant past
        # MAX_USED_CLIPS (6 as of edl/format_001.py's "Fourth revision": the
        # base 3 footage slots + the 3 extra MOMENT blocks the 8-block cap
        # now allows). No pro-subscription/paywall
        # messaging here — this fase-1 test tool has no pricing tiers to
        # gate behind, so this is a plain capacity notice, carried through
        # to status.json the same way font_requested/font_used and
        # logo_cleanup already are (see _run_render_job). Unconditional now
        # — every render is Format 001, there's no other EDL shape left to
        # branch on.
        unused_clip_count = max(0, len(clip_filenames) - MAX_USED_CLIPS)

        edl = build_format_001_edl(
            business_name=business_name,
            headline=headline,
            cta=cta,
            clip_filenames=clip_filenames,
            edit_style=template,
            usp_text=usp_text,
            output_format=output_format,
            music_file=music_filename,
            logo=logo_filename,
            font_file=font_filename,
            text_animation=text_animation,
        )

        output_path = RESULT_DIR / f"{job_id}.mp4"

        # Everything above this line is just validating the request and
        # saving small files — fast regardless of connection quality. The
        # actual render (downscale + ffmpeg pipeline) can run 15–50+
        # seconds, well past what a mobile connection or a web proxy will
        # reliably hold a single request open for, so it happens in a
        # background thread; the browser gets a job_id immediately and
        # polls /status/<job_id> instead of waiting on one open connection.
        _write_status(job_dir, status="queued")
        thread = threading.Thread(
            target=_run_render_job,
            args=(job_id, job_dir, edl, output_path, clip_filenames, logo_cleanup_status, unused_clip_count),
            daemon=True,
        )
        thread.start()

        return jsonify(ok=True, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 — surface a readable error to the test tool's own UI
        traceback.print_exc()
        return jsonify(ok=False, error=f"Render mislukt: {exc}"), 500


@app.get("/status/<job_id>")
def status(job_id: str):
    job_dir = UPLOAD_DIR / job_id
    data = _read_status(job_dir)
    if data is None:
        return jsonify(ok=False, error="Onbekende job."), 404
    return jsonify(ok=True, **data)


@app.get("/result/<job_id>.mp4")
def result(job_id: str):
    return send_from_directory(RESULT_DIR, f"{job_id}.mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8420, debug=True)
