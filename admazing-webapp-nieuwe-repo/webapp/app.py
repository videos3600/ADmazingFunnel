"""
Standalone upload-funnel test tool (bouwplan: "kleinste ding om écht te
testen" — zie de sectie daarover). Deliberately NOT a WordPress plugin: this
is a small, self-contained Flask app that wraps the existing EDL/render
pipeline in exactly the funnel steps from the concept doc's MVP-scope
("Homepage -> Upload -> bedrijfsinformatie -> template -> Generate ->
Preview -> Download" — checkout left out on purpose, see the bouwplan).

Runs the render SYNCHRONOUSLY inside the request (no job queue/websockets) —
fine for the handful of short clips this is meant to be tested with; a real
queue is a fase-1-productie concern, not something this test tool needs.
"""

from __future__ import annotations

import sys
import shutil
import traceback
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from edl.simple_generator import build_simple_edl, MIN_CLIP_DURATION  # noqa: E402
from render.ffmpeg_utils import probe_duration  # noqa: E402
from render.pipeline import render_edl, FORMAT_RESOLUTIONS  # noqa: E402
from render.templates import TEMPLATES  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
RESULT_DIR = APP_DIR / "results"
UPLOAD_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

MUSIC_DIR = ROOT / "assets" / "music"

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


MUSIC_GROUPS, MUSIC_LIBRARY = _load_music_groups()
DEFAULT_MUSIC_KEY = next(iter(MUSIC_LIBRARY))

ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 400 * 1024 * 1024  # 400MB — a handful of raw phone clips


@app.get("/")
def index():
    return render_template(
        "index.html",
        templates=sorted(TEMPLATES.keys()),
        formats=list(FORMAT_RESOLUTIONS.keys()),
        music_groups=MUSIC_GROUPS,
    )


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
        )

        output_path = RESULT_DIR / f"{job_id}.mp4"
        render_edl(edl, assets_dir=job_dir, output_path=output_path, work_dir=job_dir / "_render_work")

        return jsonify(ok=True, video_url=f"/result/{job_id}.mp4")
    except Exception as exc:  # noqa: BLE001 — surface a readable error to the test tool's own UI
        traceback.print_exc()
        return jsonify(ok=False, error=f"Render mislukt: {exc}"), 500
    finally:
        # Clean up the render's own scratch dir but keep the uploaded
        # sources + result around for debugging a failed job.
        work_dir = job_dir / "_render_work"
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/result/<job_id>.mp4")
def result(job_id: str):
    return send_from_directory(RESULT_DIR, f"{job_id}.mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8420, debug=True)
