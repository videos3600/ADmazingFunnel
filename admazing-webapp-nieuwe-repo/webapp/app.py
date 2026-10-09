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

from edl.simple_generator import build_simple_edl, MIN_CLIP_DURATION  # noqa: E402
from edl.schema import EDL  # noqa: E402
from render.ffmpeg_utils import probe_duration, probe_resolution, run as ffmpeg_run  # noqa: E402
from render.pipeline import render_edl, FORMAT_RESOLUTIONS, _resolve_punch_font  # noqa: E402
from render.templates import TEMPLATES  # noqa: E402

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
    leaves a half-written file at the real path."""
    try:
        w, h = probe_resolution(path)
    except Exception:
        return  # unreadable/corrupt — let the real render step report it clearly
    if max(w, h) <= max_dim:
        return
    vf = f"scale={max_dim}:-2" if w >= h else f"scale=-2:{max_dim}"
    tmp = path.with_name(path.stem + "_ds" + path.suffix)
    ffmpeg_run([
        "ffmpeg", "-y", "-i", str(path),
        "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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


def _run_render_job(job_id: str, job_dir: Path, edl: EDL, output_path: Path, clip_filenames: list[str]) -> None:
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
        render_edl(edl, assets_dir=job_dir, output_path=output_path, work_dir=job_dir / "_render_work")
        # Echoes exactly which font file this render actually used (not just
        # which key the form submitted) — so "I picked font X but got Anton"
        # is answerable from the status response alone, no Render log access
        # or code reading needed. Mirrors render/pipeline.py's own fallback
        # logic 1:1 (see _resolve_punch_font's print log for the same info
        # in the deploy logs).
        resolved_font_path = _resolve_punch_font(edl)
        _write_status(
            job_dir,
            status="done",
            video_url=f"/result/{job_id}.mp4",
            font_requested=edl.font or "(standaard)",
            font_used=Path(resolved_font_path).name,
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
        templates=sorted(TEMPLATES.keys()),
        formats=list(FORMAT_RESOLUTIONS.keys()),
        music_groups=MUSIC_GROUPS,
        font_groups=FONT_GROUPS,
        default_font_key=DEFAULT_FONT_KEY,
        font_css_family=_font_css_family,
        text_animations=TEXT_ANIMATIONS,
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
        template = request.form.get("template") or "Papercut"
        output_format = request.form.get("format") or "9:16"
        music_choice = request.form.get("music") or "upbeat"
        font_choice = request.form.get("font") or DEFAULT_FONT_KEY
        text_animation = request.form.get("text_animation") or DEFAULT_TEXT_ANIMATION
        if text_animation not in {key for key, _, _ in TEXT_ANIMATIONS}:
            text_animation = DEFAULT_TEXT_ANIMATION

        if not business_name:
            return jsonify(ok=False, error="Bedrijfsnaam is verplicht."), 400
        if template not in TEMPLATES:
            return jsonify(ok=False, error=f"Onbekend template '{template}'."), 400
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

        # Optional logo
        logo_filename = ""
        logo_file = request.files.get("logo")
        if logo_file and logo_file.filename:
            ext = Path(secure_filename(logo_file.filename)).suffix.lower()
            if ext in ALLOWED_IMAGE_EXT or ext == ".png":
                logo_filename = f"logo{ext}"
                logo_file.save(job_dir / logo_filename)

        # Music — placeholder library only (fase 1 openstaande keuze: echte
        # licenties), copied into the job's own asset dir so render_edl's
        # single assets_dir keeps working unchanged.
        music_src = MUSIC_LIBRARY.get(music_choice, MUSIC_LIBRARY[DEFAULT_MUSIC_KEY])
        music_filename = ""
        if music_src.exists():
            music_filename = music_src.name
            shutil.copy2(music_src, job_dir / music_filename)

        # Font — resolved to a bare filename only; render/pipeline.py looks
        # it up in its own assets/fonts/ (shipped with the app, not
        # per-upload) the same way it already resolves the default Anton
        # file, so nothing needs copying into the job's asset dir here.
        font_filename = FONT_LIBRARY.get(font_choice, FONT_LIBRARY[DEFAULT_FONT_KEY])

        edl = build_simple_edl(
            business_name=business_name,
            headline=headline,
            cta=cta,
            clip_filenames=clip_filenames,
            template=template,
            output_format=output_format,
            text_style="punch",
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
            args=(job_id, job_dir, edl, output_path, clip_filenames),
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
