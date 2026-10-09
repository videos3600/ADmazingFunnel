(function () {
  const dropzone = document.getElementById("dropzone");
  const footageInput = document.getElementById("footage-input");
  const fileList = document.getElementById("file-list");
  const form = document.getElementById("generate-form");
  const formError = document.getElementById("form-error");
  const progressView = document.getElementById("progress-view");
  const progressSteps = Array.from(document.querySelectorAll("#progress-steps li"));
  const resultView = document.getElementById("result-view");
  const resultVideo = document.getElementById("result-video");
  const downloadLink = document.getElementById("download-link");
  const makeAnotherBtn = document.getElementById("make-another");

  // Selected files live here (not directly in the <input>) so drag-and-drop
  // and click-to-browse can both add to the same growing list, with a
  // remove (x) per file — a native file input can't do partial removal.
  let selectedFiles = [];

  function syncInputFromSelection() {
    const dt = new DataTransfer();
    selectedFiles.forEach((f) => dt.items.add(f));
    footageInput.files = dt.files;
  }

  function renderFileList() {
    fileList.innerHTML = "";
    selectedFiles.forEach((f, i) => {
      const li = document.createElement("li");
      const label = document.createElement("span");
      label.textContent = f.name;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "×";
      removeBtn.setAttribute("aria-label", `Verwijder ${f.name}`);
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        selectedFiles.splice(i, 1);
        syncInputFromSelection();
        renderFileList();
      });
      li.appendChild(label);
      li.appendChild(removeBtn);
      fileList.appendChild(li);
    });
  }

  function addFiles(fileListLike) {
    Array.from(fileListLike).forEach((f) => selectedFiles.push(f));
    syncInputFromSelection();
    renderFileList();
  }

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
    selectedFiles = [];
    syncInputFromSelection();
    renderFileList();
    resultVideo.pause();
    resultVideo.removeAttribute("src");
    resultVideo.load();
    form.reset();
    showView("form");
  });
})();
