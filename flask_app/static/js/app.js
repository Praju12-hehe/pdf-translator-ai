(() => {
  const form = document.getElementById("translate-form");
  const fileInput = document.getElementById("file-input");
  const dropzone = document.getElementById("dropzone");
  const fileNameEl = document.getElementById("file-name");
  const submitBtn = document.getElementById("submit-btn");
  const statusEl = document.getElementById("status");
  const statusLabel = document.getElementById("status-label");
  const statusPercent = document.getElementById("status-percent");
  const statusMessage = document.getElementById("status-message");
  const progressBar = document.getElementById("progress-bar");
  const downloadBtn = document.getElementById("download-btn");

  let pollTimer = null;

  const formatName = (name) =>
    name.length > 56 ? name.slice(0, 26) + "…" + name.slice(-26) : name;

  const setFile = (file) => {
    if (!file) {
      fileNameEl.textContent = "Drop a PDF here";
      dropzone.classList.remove("has-file");
      submitBtn.disabled = true;
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      fileNameEl.textContent = "Only PDF files are accepted";
      dropzone.classList.remove("has-file");
      submitBtn.disabled = true;
      fileInput.value = "";
      return;
    }
    fileNameEl.textContent = formatName(file.name);
    dropzone.classList.add("has-file");
    submitBtn.disabled = false;
  };

  fileInput.addEventListener("change", (e) => {
    setFile(e.target.files[0]);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("is-drag");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("is-drag");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer?.files?.[0];
    if (file) {
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      setFile(file);
    }
  });

  const resetStatus = () => {
    statusEl.hidden = false;
    statusEl.classList.remove("is-error", "is-done");
    statusLabel.textContent = "Uploading…";
    statusPercent.textContent = "0%";
    statusMessage.textContent = "";
    progressBar.style.width = "0%";
    downloadBtn.hidden = true;
    downloadBtn.removeAttribute("href");
  };

  const setProgress = (pct, label, message) => {
    const clamped = Math.max(0, Math.min(100, Math.round(pct)));
    progressBar.style.width = clamped + "%";
    statusPercent.textContent = clamped + "%";
    if (label) statusLabel.textContent = label;
    if (message !== undefined) statusMessage.textContent = message;
  };

  const startPolling = (jobId) => {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`progress/${jobId}`);
        if (!res.ok) throw new Error("Lost the job");
        const data = await res.json();

        let label = "Translating…";
        if (data.status === "done") label = "Complete";
        else if (data.status === "error") label = "Error";
        else if (data.pages_total) {
          label = `Page ${data.pages_done || 0} / ${data.pages_total}`;
        }

        setProgress(data.progress || 0, label, data.message || "");

        if (data.status === "done") {
          clearInterval(pollTimer);
          pollTimer = null;
          statusEl.classList.add("is-done");
          downloadBtn.hidden = false;
          downloadBtn.href = `download/${jobId}`;
          submitBtn.disabled = false;
          submitBtn.textContent = "Translate another PDF";
        } else if (data.status === "error") {
          clearInterval(pollTimer);
          pollTimer = null;
          statusEl.classList.add("is-error");
          submitBtn.disabled = false;
        }
      } catch (err) {
        clearInterval(pollTimer);
        pollTimer = null;
        statusEl.classList.add("is-error");
        statusMessage.textContent = err.message || "Connection lost";
        submitBtn.disabled = false;
      }
    }, 900);
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = fileInput.files[0];
    if (!file) return;

    submitBtn.disabled = true;
    submitBtn.textContent = "Working…";
    resetStatus();

    const fd = new FormData();
    fd.append("file", file);
    fd.append(
      "language",
      document.querySelector('input[name="language"]:checked').value
    );

    try {
      const res = await fetch("translate", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");
      setProgress(5, "Translating…", "Job started");
      startPolling(data.job_id);
    } catch (err) {
      statusEl.classList.add("is-error");
      statusMessage.textContent = err.message;
      submitBtn.disabled = false;
      submitBtn.textContent = "Translate PDF";
    }
  });
})();
