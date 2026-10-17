(function () {
  const dropzone = document.getElementById("dropzone");
  const footageInput = document.getElementById("footage-input");
  const mediaGrid = document.getElementById("media-grid");
  const dropzoneHint = document.getElementById("dropzone-hint");
  const form = document.getElementById("generate-form");
  const formError = document.getElementById("form-error");
  const progressView = document.getElementById("progress-view");
  const progressSteps = Array.from(document.querySelectorAll("#progress-steps li"));
  const resultView = document.getElementById("result-view");
  const resultVideo = document.getElementById("result-video");
  const downloadLink = document.getElementById("download-link");
  const makeAnotherBtn = document.getElementById("make-another");
  const durationNote = document.getElementById("duration-note");

  // The USP field is always relevant now — every ad is Format 001's block
  // structure (see edl/format_001.py's "Third revision"), which always has
  // a USP block, regardless of which edit style is picked in the dropdown
  // below. There's no more per-template toggle to sync.

  // Live "how long will this be" estimate, updated on every add/remove —
  // mirrors edl/format_001.py's build_format_001_edl EXACTLY: base 6 blocks
  // (HOOK/HERO/EXPERIENCE/USP/DESIRE/CTA, cycling through whatever's
  // uploaded if fewer than 3 clips), +1 extra "MOMENT" block per clip past
  // the base 3, capped at 2 extras — so 8 blocks / 32s is the ceiling on
  // 5+ uploaded clips. If MAX_BLOCKS / the base role count / BLOCK_DURATION
  // ever change there, update the constants below to match (there's no
  // shared config between Python and this file). Every edit style in the
  // dropdown shares this same block-based timing now — the "Template"
  // dropdown no longer has a non-block-based option, so this no longer
  // branches on which one is selected.
  function formatDurationNote(nClips) {
    if (nClips === 0) {
      return "Elke foto of video blijft ongeveer 4 seconden in beeld in de uiteindelijke advertentie.";
    }
    const BASE_BLOCKS = 6;
    const BASE_FOOTAGE = 3;
    const MAX_EXTRA_BLOCKS = 2;
    const BLOCK_SECONDS = 4;
    const MAX_USED_CLIPS = BASE_FOOTAGE + MAX_EXTRA_BLOCKS; // 5
    const extraBlocks = Math.max(0, Math.min(nClips - BASE_FOOTAGE, MAX_EXTRA_BLOCKS));
    const blocks = BASE_BLOCKS + extraBlocks;
    const seconds = blocks * BLOCK_SECONDS;
    const unused = Math.max(0, nClips - MAX_USED_CLIPS);
    let text = `Je advertentie wordt ongeveer ${seconds} seconden (${blocks} blokken van 4 sec).`;
    if (unused > 0) {
      text += ` Let op: alleen de eerste ${MAX_USED_CLIPS} bestanden worden gebruikt — ${unused} bestand${unused === 1 ? "" : "en"} ${unused === 1 ? "valt" : "vallen"} erbuiten.`;
    }
    return text;
  }

  function syncDurationNote() {
    if (!durationNote) return;
    durationNote.textContent = formatDurationNote(selectedFiles.length);
  }

  // Selected files live here (not directly in the <input>) so drag-and-drop
  // and click-to-browse can both add to the same growing list, with a
  // remove (x) per file — a native file input can't do partial removal.
  // selectedUrls mirrors selectedFiles 1:1 (same index) — an object URL per
  // file, created once on add and revoked once on removal/reset, so the
  // person can actually SEE each photo/video they added, not just its
  // filename.
  let selectedFiles = [];
  let selectedUrls = [];

  function syncInputFromSelection() {
    const dt = new DataTransfer();
    selectedFiles.forEach((f) => dt.items.add(f));
    footageInput.files = dt.files;
  }

  function renderMediaGrid() {
    mediaGrid.innerHTML = "";
    if (dropzoneHint) dropzoneHint.hidden = selectedFiles.length > 0;
    syncDurationNote();

    selectedFiles.forEach((f, i) => {
      const tile = document.createElement("div");
      tile.className = "media-tile";
      // Clicks on an existing thumbnail (e.g. hitting native video controls
      // to preview it) shouldn't re-trigger the dropzone's click-to-browse.
      tile.addEventListener("click", (e) => e.stopPropagation());

      if (f.type.startsWith("video/")) {
        const video = document.createElement("video");
        video.src = selectedUrls[i];
        video.muted = true;
        video.playsInline = true;
        video.controls = true;
        video.preload = "metadata";
        tile.appendChild(video);
      } else {
        const img = document.createElement("img");
        img.src = selectedUrls[i];
        img.alt = f.name;
        tile.appendChild(img);
      }

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "media-tile-remove";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", `Verwijder ${f.name}`);
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        URL.revokeObjectURL(selectedUrls[i]);
        selectedFiles.splice(i, 1);
        selectedUrls.splice(i, 1);
        syncInputFromSelection();
        renderMediaGrid();
      });
      tile.appendChild(removeBtn);

      mediaGrid.appendChild(tile);
    });

    const addTile = document.createElement("button");
    addTile.type = "button";
    addTile.className = "media-tile-add";
    addTile.textContent = "+";
    addTile.setAttribute("aria-label", "Foto of video toevoegen");
    addTile.addEventListener("click", (e) => {
      e.stopPropagation();
      footageInput.click();
    });
    mediaGrid.appendChild(addTile);
  }

  function addFiles(fileListLike) {
    Array.from(fileListLike).forEach((f) => {
      selectedFiles.push(f);
      selectedUrls.push(URL.createObjectURL(f));
    });
    syncInputFromSelection();
    renderMediaGrid();
  }

  // Shows the single "+" add-tile even before anything has been uploaded.
  renderMediaGrid();

  dropzone.addEventListener("click", () => footageInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      footageInput.click();
    }
  });

  footageInput.addEventListener("change", () => {
    addFiles(footageInput.files);
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer && e.dataTransfer.files) {
      addFiles(e.dataTransfer.files);
    }
  });

  // ---- fake-but-honest progress checklist ----
  // Still no real per-step signal from the backend (status.json only ever
  // says queued/running/done/error, not which internal step) — this
  // animates through the steps on a timer while the job is in flight, and
  // jumps straight to the last step ("Video renderen…") and holds there
  // until the poll actually reports done/error, rather than claiming a
  // step finished before it could have.
  let progressTimer = null;

  function startProgressAnimation() {
    progressSteps.forEach((li) => li.classList.remove("active", "done"));
    let i = 0;
    progressSteps[0].classList.add("active");
    progressTimer = setInterval(() => {
      if (i >= progressSteps.length - 1) {
        clearInterval(progressTimer);
        return;
      }
      progressSteps[i].classList.remove("active");
      progressSteps[i].classList.add("done");
      i += 1;
      progressSteps[i].classList.add("active");
    }, 900);
  }

  function stopProgressAnimation() {
    if (progressTimer) clearInterval(progressTimer);
    progressSteps.forEach((li) => li.classList.add("done"));
  }

  function showView(view) {
    form.hidden = view !== "form";
    progressView.hidden = view !== "progress";
    resultView.hidden = view !== "result";
  }

  // ---- status polling ----
  // The render runs server-side in a background thread (see webapp/app.py)
  // precisely so a flaky mobile connection can't kill it mid-render — this
  // loop reflects that: one failed poll is just skipped, not fatal. Only
  // after several IN A ROW do we give up and tell the person to check their
  // connection (by then the job itself may well still finish; they can
  // just reopen the page, the backend never lost it).
  const POLL_INTERVAL_MS = 1500;
  const MAX_CONSECUTIVE_POLL_ERRORS = 8; // ~12s of unreachable server before giving up

  async function pollStatus(jobId) {
    let consecutiveErrors = 0;
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));

      let resp, data;
      try {
        resp = await fetch(`/status/${jobId}`, { cache: "no-store" });
        data = await resp.json();
      } catch (err) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= MAX_CONSECUTIVE_POLL_ERRORS) {
          throw new Error("Kon de status niet meer ophalen — controleer je internetverbinding. De video kan trouwens nog steeds klaar komen; probeer het later opnieuw.");
        }
        continue;
      }
      consecutiveErrors = 0;

      if (!resp.ok || !data.ok) {
        throw new Error(data.error || "Kon de status niet ophalen.");
      }
      if (data.status === "done") return data;
      if (data.status === "error") throw new Error(data.error || "Render mislukt.");
      // "queued" / "running" — keep polling
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    formError.textContent = "";

    if (selectedFiles.length === 0) {
      formError.textContent = "Upload minstens één foto of video.";
      return;
    }

    showView("progress");
    startProgressAnimation();

    try {
      const formData = new FormData(form);
      const resp = await fetch("/generate", { method: "POST", body: formData });
      const data = await resp.json();

      if (!resp.ok || !data.ok) {
        stopProgressAnimation();
        showView("form");
        formError.textContent = data.error || "Er ging iets mis bij het genereren.";
        return;
      }

      const result = await pollStatus(data.job_id);

      stopProgressAnimation();
      resultVideo.src = result.video_url;
      downloadLink.href = result.video_url;
      showView("result");
    } catch (err) {
      stopProgressAnimation();
      showView("form");
      formError.textContent = err.message || "Kon geen verbinding maken met de server.";
    }
  });

  makeAnotherBtn.addEventListener("click", () => {
    selectedUrls.forEach((url) => URL.revokeObjectURL(url));
    selectedFiles = [];
    selectedUrls = [];
    syncInputFromSelection();
    renderMediaGrid();
    resultVideo.pause();
    resultVideo.removeAttribute("src");
    resultVideo.load();
    form.reset();
    showView("form");
  });
})();
