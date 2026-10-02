"""Template registry — the seam that lets `edl.template` actually change
how a video is assembled. Adding a template means adding a module here
with an `assemble(renderer, edl, resolution, ken_burns=False) -> Path`
function and registering it below; nothing in render/pipeline.py's core
methods (trim, concat, text, music) needs to change or branch on template
name. See papercut.py / rotator.py / splitter.py for the shape.
"""

from __future__ import annotations

from render.templates import flash, papercut, rotator, shake, splitter, zoom

TEMPLATES = {
    "Papercut": papercut.assemble,
    "Rotator": rotator.assemble,
    "Splitter": splitter.assemble,
    "Shake": shake.assemble,
    "Flash": flash.assemble,
    "Zoom": zoom.assemble,
}
