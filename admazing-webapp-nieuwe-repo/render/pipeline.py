"""
Fase-1 render engine: turns an EDL into an MP4 with FFmpeg only.

This is the "editor" side of the AI-is-director / render-engine-is-editor
split from the concept doc. It never decides *what* goes where — it only
executes exactly what the EDL says. Swapping the AI layer (fase 2: real
classification + LLM copy) never requires touching this file, as long as
the EDL shape stays the same.

Remotion is the planned upgrade path for richer text animation / motion
graphics (technisch bouwplan, fase 1 architecture table) — this module
proves the pipeline end to end with FFmpeg alone first, which is enough for
the Papercut template's needs (hard cuts, lower-third text, logo overlay,
music bed).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from edl.schema import EDL
from render.ffmpeg_utils import escape_drawtext as _escape_drawtext
from render.ffmpeg_utils import probe_duration
from render.ffmpeg_utils import run as _run
from render.templates import TEMPLATES
from render.text_fx import render_punch_overlay

FORMAT_RESOLUTIONS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}

FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Condensed/impact display font for text_style="punch" — the "in your face"
# treatment (technisch bouwplan: reactie op "meer in your face, minder
# PowerPoint"). Converted from the @fontsource/anton npm package (woff2 ->
# ttf via fontTools) since the sandbox can't reach GitHub raw directly.
DISPLAY_FONT_FILE = str(Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Anton-Regular.ttf")


class Renderer:
    def __init__(self, assets_dir: Path, work_dir: Path | None = None):
        self.assets_dir = Path(assets_dir)
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="admazing_render_"))
        self.work_dir.mkdir(parents=True, exist_ok=True)

    # -- steps -----------------------------------------------------------

    def _trim_and_normalize_clip(
        self,
        source: str,
        duration: float,
        index: int,
        resolution: tuple[int, int],
        ken_burns: bool = False,
    ) -> Path:
        w, h = resolution
        src_path = self.assets_dir / source
        if not src_path.exists():
            raise FileNotFoundError(f"EDL references clip '{source}' but it is not in {self.assets_dir}")

        # A still photo (common upload — not every customer has video
        # footage) is a single frame to ffmpeg. Without "-loop 1" it reads
        # that one frame and hits EOF immediately, so the segment comes out
        # a fraction of a second long no matter what "-t duration" asks for
        # — the photo "flashes" rather than holding its assigned slot. "-t"
        # only ever caps a stream's length, it can't pad a shorter one, so
        # the fix has to be on the input side: tell ffmpeg to treat the
        # image as a (functionally) infinite source to cut "duration"
        # seconds from, same as any video clip.
        is_image = src_path.suffix.lower() in {".png", ".jpg", ".jpeg"}
        input_args = ["-loop", "1", "-framerate", "30", "-i", str(src_path)] if is_image else ["-i", str(src_path)]

        out_path = self.work_dir / f"seg_{index:02d}.mp4"
        if ken_burns:
            # Slow continuous push-in (text_style="punch" only) — a static
            # clip reads as a slide; a clip that's always drifting in reads
            # as footage someone shot for this ad. Overscan first so zoompan
            # has resolution to zoom into without upscaling artifacts.
            vf = (
                f"scale={int(w*1.15)}:{int(h*1.15)}:force_original_aspect_ratio=increase,"
                f"crop={int(w*1.15)}:{int(h*1.15)},"
                f"zoompan=z='min(zoom+0.0012,1.12)':d=1:s={w}x{h}:fps=30,"
                f"setsar=1"
            )
        else:
            vf = (
                f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},"
                f"setsar=1,fps=30"
            )
        _run([
            "ffmpeg", "-y",
            *input_args,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            str(out_path),
        ])
        return out_path

    def _concat_clips(self, segment_paths: list[Path]) -> Path:
        list_file = self.work_dir / "concat_list.txt"
        list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in segment_paths))
        out_path = self.work_dir / "concatenated.mp4"
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy",
            str(out_path),
        ])
        return out_path

    def _apply_punch_text_and_logo(
        self, video_path: Path, edl: EDL, resolution: tuple[int, int], duration: float | None = None
    ) -> Path:
        """text_style="punch": real per-frame compositing (render/text_fx.py)
        instead of drawtext — scale-bounce pop-in, blurred drop shadows,
        accent-highlighted keyword, stamped CTA badge. See text_fx.py for why
        drawtext alone can't do this (no per-frame scale/easing).

        `duration` overrides edl.duration for how long the overlay video
        runs — needed because a template like Rotator/Splitter can produce
        a real runtime shorter than the EDL author assumed (see
        render/templates/rotator.py, splitter.py)."""
        w, h = resolution
        overlay_path = render_punch_overlay(
            edl, resolution, fps=30, work_dir=self.work_dir, font_path=DISPLAY_FONT_FILE, duration=duration
        )

        out_path = self.work_dir / "with_text.mp4"
        inputs = ["-i", str(video_path), "-i", str(overlay_path)]

        if edl.logo and (self.assets_dir / edl.logo).exists():
            inputs += ["-i", str(self.assets_dir / edl.logo)]
            filter_complex = (
                "[0:v][1:v]overlay=0:0[txt];"
                f"[2:v]scale={int(w*0.16)}:-1[logo];"
                f"[txt][logo]overlay=W-w-{int(w*0.05)}:{int(h*0.05)}[out]"
            )
        else:
            filter_complex = "[0:v][1:v]overlay=0:0[out]"

        _run([
            "ffmpeg", "-y", *inputs,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            str(out_path),
        ])
        return out_path

    def _apply_text_and_logo(
        self, video_path: Path, edl: EDL, resolution: tuple[int, int], duration: float | None = None
    ) -> Path:
        if edl.text_style == "punch":
            return self._apply_punch_text_and_logo(video_path, edl, resolution, duration=duration)

        w, h = resolution
        filters = []

        safe_width = w * 0.88
        # DejaVu Sans Bold averages ~0.6em per character; back-solve a fontsize
        # that keeps the longest cue inside the safe width instead of a fixed
        # size that overflows on longer headlines.
        avg_char_width_factor = 0.6

        for cue in edl.text_cues:
            text = _escape_drawtext(cue.content)
            char_count = max(len(cue.content), 1)
            width_capped_size = int(safe_width / (char_count * avg_char_width_factor))

            if cue.kind == "cta":
                y = f"h-(h*0.16)"
                fontsize = max(min(int(h * 0.045), width_capped_size), 22)
                box_color = "0xFF0099@0.92"
                font_color = "0x0A0A0D" if edl.text_style == "boxed" else "0xFF0099"
            else:
                y = f"h-(h*0.24)"
                fontsize = max(min(int(h * 0.05), width_capped_size), 22)
                box_color = "black@0.35"
                font_color = "white"

            if edl.text_style == "minimal":
                # No background band — just the type, with a soft drop shadow
                # so it still reads over any clip. This is what "Make it
                # cleaner" switches to.
                style_part = (
                    "box=0:"
                    "shadowcolor=black@0.55:shadowx=0:shadowy=3"
                )
            else:
                style_part = f"box=1:boxcolor={box_color}:boxborderw=18"

            filters.append(
                "drawtext="
                f"fontfile={FONT_FILE}:"
                f"text='{text}':"
                f"fontcolor={font_color}:fontsize={fontsize}:"
                f"{style_part}:"
                f"x=(w-text_w)/2:y={y}:"
                f"enable='between(t,{cue.start},{cue.end})'"
            )

        out_path = self.work_dir / "with_text.mp4"
        inputs = ["-i", str(video_path)]

        if edl.logo and (self.assets_dir / edl.logo).exists():
            inputs += ["-i", str(self.assets_dir / edl.logo)]
            text_chain = ",".join(filters) if filters else "null"
            filter_complex = (
                f"[0:v]{text_chain}[txt];"
                f"[1:v]scale={int(w*0.16)}:-1[logo];"
                f"[txt][logo]overlay=W-w-{int(w*0.05)}:{int(h*0.05)}[out]"
            )
            _run([
                "ffmpeg", "-y", *inputs,
                "-filter_complex", filter_complex,
                "-map", "[out]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(out_path),
            ])
        else:
            vf = ",".join(filters) if filters else "null"
            _run([
                "ffmpeg", "-y", *inputs,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                str(out_path),
            ])
        return out_path

    def _add_music(self, video_path: Path, edl: EDL, duration: float | None = None) -> Path:
        out_path = self.work_dir / "final.mp4"
        total_duration = duration if duration is not None else edl.duration
        if edl.music and edl.music.file and (self.assets_dir / edl.music.file).exists():
            music_path = self.assets_dir / edl.music.file
            # Clamp the fade-out to the clip's own length: a clip shorter
            # than the usual 0.6s fade (e.g. a quick test render with very
            # short source clips) previously produced a negative afade
            # 'st', which ffmpeg rejects outright ("Numerical result out
            # of range") and fails the whole render.
            safe_duration = max(total_duration, 0.0)
            fade_dur = min(0.6, safe_duration / 2)
            fade_start = max(0.0, safe_duration - fade_dur)
            if fade_dur > 0:
                audio_filter = f"[1:a]volume=0.55,afade=t=out:st={fade_start:.2f}:d={fade_dur:.2f}[a]"
            else:
                audio_filter = "[1:a]volume=0.55[a]"
            _run([
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-stream_loop", "-1", "-i", str(music_path),
                "-filter_complex", audio_filter,
                "-map", "0:v", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                "-movflags", "+faststart",
                str(out_path),
            ])
        else:
            _run([
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-c:v", "copy",
                "-movflags", "+faststart",
                str(out_path),
            ])
        return out_path

    # -- entry point -------------------------------------------------------

    def render(self, edl: EDL, output_path: Path) -> Path:
        resolution = FORMAT_RESOLUTIONS[edl.format]
        ken_burns = edl.text_style == "punch"

        assemble = TEMPLATES.get(edl.template)
        if assemble is None:
            raise ValueError(f"No renderer registered for template '{edl.template}' — see render/templates/")
        assembled = assemble(self, edl, resolution, ken_burns=ken_burns)

        # The template's real runtime, not edl.duration's estimate — Rotator
        # and Splitter both legitimately produce a different total length
        # (see their docstrings). Everything downstream uses this, not the EDL's.
        actual_duration = probe_duration(assembled)

        with_overlays = self._apply_text_and_logo(assembled, edl, resolution, duration=actual_duration)
        finished = self._add_music(with_overlays, edl, duration=actual_duration)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            finished.replace(output_path)
        except OSError:
            # work_dir and output_path can be on different filesystems (e.g.
            # /tmp vs. a mounted output dir) — os.replace/Path.replace can't
            # cross that, so fall back to a copy.
            import shutil
            shutil.copy2(finished, output_path)
            finished.unlink()
        return output_path


def render_edl(edl: EDL, assets_dir: Path, output_path: Path, work_dir: Path | None = None) -> Path:
    renderer = Renderer(assets_dir=assets_dir, work_dir=work_dir)
    return renderer.render(edl, output_path)


def edl_to_json(edl: EDL, path: Path) -> None:
    Path(path).write_text(json.dumps(edl.to_dict(), indent=2))
