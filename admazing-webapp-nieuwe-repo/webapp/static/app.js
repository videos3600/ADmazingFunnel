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
  // No real per-step signal from the backend (render_edl runs as one
  // blocking call) — this animates through the steps on a timer while the
  // actual request is in flight, and jumps straight to the last step
  // ("Video renderen…") and holds there until the response actually comes
  // back, rather than claiming a step finished before it could have.
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

      stopProgressAnimation();

      if (!resp.ok || !data.ok) {
        showView("form");
        formError.textContent = data.error || "Er ging iets mis bij het genereren.";
        return;
      }

      resultVideo.src = data.video_url;
      downloadLink.href = data.video_url;
      showView("result");
    } catch (err) {
      stopProgressAnimation();
      showView("form");
      formError.textContent = "Kon geen verbinding maken met de server.";
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
