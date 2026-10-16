"""Logo background cleanup — runs once per /generate call, right after the
uploaded logo is saved, BEFORE it ever reaches the EDL or the renderer.
render/block_pipeline.py's _logo_layer() still just loads whatever file
edl.logo points to; this is what decides which file that ends up being.

Product decision (confirmed with the client, Oct 2026): a real AI
background-removal model (rembg) was measured and rejected here — ~250MB
of extra dependencies and a much slower cold start on Render.com, to solve
a case (photo-like/gradient backgrounds) that barely occurs for a BUSINESS
LOGO upload. Most real logos come off Canva, a website export, or a
screenshot, with a flat, solid-colour background — which this heuristic
handles for close to free (no new dependencies; well under a second even
on a large file — see the timing numbers given to the client).

The heuristic deliberately does NOT try harder than "flat background,
sampled from the four corners". A gradient, a photo, or a logo that
doesn't agree with itself corner-to-corner is left untouched rather than
risking a half-erased logo — see clean_logo_background()'s docstring for
the exact bail-out conditions and how to read its returned status.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# How close a pixel's RGB has to be to the sampled background colour to
# count as background (sum of absolute per-channel difference, 0-765).
# Loose enough to absorb JPEG compression noise around a flat fill.
COLOR_THRESHOLD = 36

# The four corners have to agree this closely with EACH OTHER (worst-case
# pairwise difference) before the background even counts as "flat" —
# otherwise this is a gradient/photo and the whole approach doesn't apply.
CORNER_AGREEMENT_THRESHOLD = 24

# If the detected "background" covers less than this fraction of the
# image, there's nothing meaningful to remove (logo fills the frame edge
# to edge, or it's already transparent in a way the alpha check missed).
MIN_BACKGROUND_FRACTION = 0.04

# If it covers more than this, the heuristic is more likely misreading a
# large flat area of the LOGO ITSELF as background (e.g. a mostly
# single-colour logo on white) — leaving it alone beats erasing most of it.
MAX_BACKGROUND_FRACTION = 0.85


def clean_logo_background(path: Path) -> tuple[Path, str]:
    """Attempts to make a flat-colour background transparent.

    Returns (final_path, status):
      - final_path is `path` unchanged whenever nothing was done, or a
        sibling ".png" file when the background was actually removed (a
        JPEG can't hold the new alpha channel, so the result is always a
        PNG regardless of the input extension).
      - status is one of:
          "cleaned"             — background removed
          "already-transparent" — alpha channel already varies, left alone
          "not-flat"            — corners disagree too much, left alone
          "low-confidence"      — background fraction outside the safe
                                   range, left alone
          "unreadable"          — couldn't open the file as an image at all

    Never raises on a readable image — a cleanup step that crashes a whole
    render over a logo file would be worse than just not cleaning it up;
    the caller always gets a usable path back, cleaned or not.
    """
    try:
        img = Image.open(path).convert("RGBA")
    except Exception:
        return path, "unreadable"

    arr = np.array(img)
    alpha = arr[:, :, 3]
    if alpha.min() < 250:
        # Already has real transparency somewhere (a proper transparent
        # PNG someone already made correctly) — don't touch it.
        return path, "already-transparent"

    h, w = arr.shape[0], arr.shape[1]
    corners = np.array(
        [arr[0, 0, :3], arr[0, w - 1, :3], arr[h - 1, 0, :3], arr[h - 1, w - 1, :3]],
        dtype=int,
    )
    max_pair_diff = max(
        int(np.abs(corners[i] - corners[j]).sum())
        for i in range(4) for j in range(i + 1, 4)
    )
    if max_pair_diff > CORNER_AGREEMENT_THRESHOLD:
        return path, "not-flat"

    bg_color = corners.mean(axis=0)
    diff = np.abs(arr[:, :, :3].astype(int) - bg_color.astype(int)).sum(axis=2)
    mask = diff < COLOR_THRESHOLD
    fraction = float(mask.mean())
    if fraction < MIN_BACKGROUND_FRACTION or fraction > MAX_BACKGROUND_FRACTION:
        return path, "low-confidence"

    arr[:, :, 3] = np.where(mask, 0, 255)
    out_path = path.with_suffix(".png")
    Image.fromarray(arr, "RGBA").save(out_path)
    if out_path != path:
        path.unlink(missing_ok=True)
    return out_path, "cleaned"
