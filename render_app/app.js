const $ = (selector) => document.querySelector(selector);

const state = {
  kind: "lost",
  photoData: "",
  uploadPhotoData: "",
  classes: [],
  detections: [],
  selectedDetection: null,
  photoToken: 0,
  fileToken: 0,
  cameraStream: null,
  activeReportId: 0,
  confirmingMatch: false,
  isAdmin: false,
  csrfToken: "",
  reports: [],
};

const form = $("#report-form");
const classSelect = $("#object-class");
const fileInput = $("#photo-file");
const video = $("#camera-video");
const message = $("#form-message");
const submitButton = $("#submit-report");
const matchesList = $("#matches-list");
const matchesEmpty = $("#matches-empty");
const reportsList = $("#reports-list");

function currentTheme() {
  return document.documentElement.dataset.theme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function updateThemeControl() {
  const dark = currentTheme() === "dark";
  const button = $("#theme-toggle");
  button.setAttribute("aria-pressed", String(dark));
  button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  $("#theme-icon").textContent = dark ? "☀" : "☾";
  $("#theme-label").textContent = dark ? "Light theme" : "Dark theme";
  document.querySelector('meta[name="theme-color"]').content = dark ? "#0c1628" : "#f3f6fb";
}

$("#theme-toggle").addEventListener("click", () => {
  const nextTheme = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = nextTheme;
  try {
    localStorage.setItem("lf-theme-v2", nextTheme);
  } catch {
    showMessage("Theme changed, but your browser could not save this preference.", true);
  }
  updateThemeControl();
});

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!document.documentElement.dataset.theme) updateThemeControl();
});

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(options.headers || {}),
  };
  if (["POST", "PUT", "DELETE"].includes(method) && state.csrfToken) {
    headers["X-CSRF-Token"] = state.csrfToken;
  }
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers,
  });
  const contentType = response.headers.get("content-type") || "";
  const result = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    throw new Error(result?.error || `Server error (${response.status})`);
  }
  return result;
}

function showMessage(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
}

function updateSubmitState() {
  submitButton.disabled = !state.photoData || !state.selectedDetection;
}

function createCanvas(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("This browser does not support image processing.");
  return { canvas, context };
}

function stopCamera() {
  if (state.cameraStream) {
    for (const track of state.cameraStream.getTracks()) track.stop();
    state.cameraStream = null;
  }
  video.srcObject = null;
  $("#camera-shot").disabled = true;
  $("#camera-wrap").hidden = true;
}

function showPhoto(dataUrl) {
  state.photoData = dataUrl;
  state.uploadPhotoData = "";
  state.detections = [];
  state.selectedDetection = null;
  state.photoToken += 1;
  $("#photo-preview").src = dataUrl;
  $("#photo-preview-wrap").hidden = false;
  $("#camera-wrap").hidden = true;
  resetDetectionOptions();
  $("#detection-result").textContent = "Detecting item and main color…";
  updateSubmitState();
}

function clearPhoto() {
  stopCamera();
  state.photoToken += 1;
  state.photoData = "";
  state.uploadPhotoData = "";
  state.detections = [];
  state.selectedDetection = null;
  fileInput.value = "";
  $("#photo-preview").removeAttribute("src");
  $("#photo-preview-wrap").hidden = true;
  resetDetectionOptions();
  $("#detection-result").textContent = "";
  updateSubmitState();
}

function resetDetectionOptions() {
  classSelect.replaceChildren(new Option("Add a photo to get started", ""));
  for (const item of state.classes) {
    classSelect.add(new Option(item.name, item.id));
  }
  classSelect.value = "";
  classSelect.disabled = true;
}

async function getNormalizedPhoto() {
  const image = new Image();
  image.src = state.photoData;
  await image.decode();
  const maxSide = 1600;
  const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight));
  const resized = createCanvas(
    Math.max(1, Math.round(image.naturalWidth * scale)),
    Math.max(1, Math.round(image.naturalHeight * scale)),
  );
  resized.context.drawImage(image, 0, 0, resized.canvas.width, resized.canvas.height);
  return {
    photo: resized.canvas.toDataURL("image/jpeg", 0.86),
  };
}

function showSelectedDetection() {
  const detection = state.selectedDetection;
  if (!detection) {
    $("#detection-result").textContent = "No item detected.";
    return;
  }
  const confidence = Math.round(detection.confidence * 100);
  const warning = detection.confidence < 0.5
    ? " · Low confidence — please check the category"
    : "";
  $("#detection-result").textContent =
    `${detection.object_name} · ${detection.color_name} · ${confidence}% confidence${warning}`;
}

function applyDetections(detections) {
  state.detections = detections;
  classSelect.replaceChildren();
  if (!detections.length) {
    classSelect.add(new Option("No item detected", ""));
    classSelect.disabled = true;
    state.selectedDetection = null;
    showSelectedDetection();
    return;
  }
  detections.forEach((detection, index) => {
    classSelect.add(new Option(
      `${detection.object_name} · ${Math.round(detection.confidence * 100)}%`,
      String(index),
    ));
  });
  classSelect.disabled = false;
  classSelect.value = "0";
  state.selectedDetection = detections[0];
  showSelectedDetection();
}

async function detectPhoto() {
  const token = state.photoToken;
  try {
    const photo = await getNormalizedPhoto();
    if (token !== state.photoToken) return;
    state.uploadPhotoData = photo.photo;
    showMessage("Detecting the item and main color on the server…");
    const response = await api("/api/detect", {
      method: "POST",
      body: JSON.stringify({ photo: photo.photo }),
    });
    if (token !== state.photoToken) return;
    applyDetections(response.detections);
    if (response.detections.length) {
      showMessage("Detection complete. Check the category and color before saving.");
    } else {
      showMessage("No item detected. Try another photo with the item clearly visible.", true);
    }
  } catch (error) {
    if (token !== state.photoToken) return;
    applyDetections([]);
    showMessage(`Could not detect the item: ${error.message}`, true);
  } finally {
    if (token === state.photoToken) updateSubmitState();
  }
}

function formatDate(value) {
  if (!value) return "Date unknown";
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("en-US", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function tag(kind, status) {
  const element = document.createElement("span");
  element.className = `status-tag${status === "matched" ? " matched" : ""}`;
  element.textContent = status === "matched" ? "Matched" : kind === "lost" ? "Lost" : "Found";
  return element;
}

function renderReports(reports) {
  $("#open-count").textContent = reports.filter((report) => report.status === "open").length;
  reportsList.replaceChildren();
  if (!reports.length) {
    const empty = document.createElement("p");
    empty.className = "field-hint";
    empty.textContent = "No reports yet. Add the first one above.";
    reportsList.append(empty);
    return;
  }
  for (const report of reports.slice(0, 6)) {
    const row = document.createElement("article");
    row.className = "report-row";
    const image = document.createElement("img");
    image.className = "report-thumb";
    image.src = report.photo_url;
    image.alt = "";
    const details = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = report.object_name;
    const sub = document.createElement("small");
    sub.textContent = `${report.kind === "lost" ? "Lost" : "Found"} · ${report.color_name} · ${formatDate(report.created_at)}`;
    details.append(title, sub);
    row.append(image, details, tag(report.kind, report.status));

    if (report.status === "open" && state.isAdmin) {
      const actions = document.createElement("div");
      actions.className = "report-actions";
      const edit = document.createElement("button");
      edit.className = "report-action";
      edit.type = "button";
      edit.textContent = "Edit";
      edit.setAttribute("aria-label", `Edit ${report.kind} report for ${report.object_name}`);
      const remove = document.createElement("button");
      remove.className = "report-action report-action-danger";
      remove.type = "button";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete ${report.kind} report for ${report.object_name}`);
      actions.append(edit, remove);
      row.append(actions);

      const editor = document.createElement("form");
      editor.className = "report-edit-form";
      editor.hidden = true;
      const kindLabel = document.createElement("label");
      kindLabel.className = "field-label";
      kindLabel.textContent = "Report type";
      const kindSelect = document.createElement("select");
      kindSelect.name = "kind";
      kindSelect.required = true;
      kindSelect.add(new Option("Lost", "lost"));
      kindSelect.add(new Option("Found", "found"));
      kindSelect.value = report.kind;
      kindLabel.append(kindSelect);

      const descriptionLabel = document.createElement("label");
      descriptionLabel.className = "field-label";
      descriptionLabel.textContent = "Description";
      const descriptionInput = document.createElement("textarea");
      descriptionInput.name = "description";
      descriptionInput.maxLength = 1000;
      descriptionInput.rows = 2;
      descriptionInput.value = report.description;
      descriptionLabel.append(descriptionInput);

      const editActions = document.createElement("div");
      editActions.className = "report-edit-actions";
      const save = document.createElement("button");
      save.className = "button button-primary";
      save.type = "submit";
      save.textContent = "Save changes";
      const cancel = document.createElement("button");
      cancel.className = "button button-secondary";
      cancel.type = "button";
      cancel.textContent = "Cancel";
      editActions.append(save, cancel);
      editor.append(kindLabel, descriptionLabel, editActions);
      row.append(editor);

      edit.addEventListener("click", () => {
        editor.hidden = !editor.hidden;
        if (!editor.hidden) descriptionInput.focus();
      });
      cancel.addEventListener("click", () => {
        editor.hidden = true;
        kindSelect.value = report.kind;
        descriptionInput.value = report.description;
      });
      editor.addEventListener("submit", (event) => {
        event.preventDefault();
        saveReportEdit(report, editor);
      });
      remove.addEventListener("click", () => deleteReport(report, remove));
    } else if (report.status === "matched" && state.isAdmin) {
      const note = document.createElement("small");
      note.className = "report-readonly-note";
      note.textContent = "Matched reports are read-only.";
      row.append(note);
    }
    reportsList.append(row);
  }
}

function renderMatches(matches) {
  matchesList.replaceChildren();
  matchesEmpty.hidden = matches.length > 0;
  for (const match of matches) {
    const report = match.report;
    const card = document.createElement("article");
    card.className = "match-item";
    const image = document.createElement("img");
    image.className = "match-photo";
    image.src = report.photo_url;
    image.alt = `Photo of ${report.object_name}`;
    const content = document.createElement("div");
    content.className = "match-main";
    const top = document.createElement("div");
    top.className = "match-topline";
    const kind = document.createElement("span");
    kind.className = "kind-tag";
    kind.textContent = report.kind === "lost" ? "Lost" : "Found";
    const score = document.createElement("span");
    score.className = "match-score";
    score.textContent = `Match score ${Math.round(match.score * 100)}%`;
    top.append(kind, score);
    const title = document.createElement("h3");
    title.textContent = report.object_name;
    const description = document.createElement("p");
    description.className = "match-description";
    description.textContent = report.description || "No description";
    const meta = document.createElement("div");
    meta.className = "match-meta";
    const chip = document.createElement("span");
    chip.className = "color-chip";
    chip.dataset.color = report.color_name;
    chip.setAttribute("aria-label", `Color: ${report.color_name}`);
    const colorName = document.createElement("span");
    colorName.textContent = report.color_name;
    const date = document.createElement("span");
    date.textContent = formatDate(report.created_at);
    meta.append(chip, colorName, date);
    const confirm = document.createElement("button");
    confirm.className = "confirm-button";
    confirm.type = "button";
    confirm.textContent = state.isAdmin ? "Confirm match" : "Admin sign in to confirm";
    confirm.addEventListener("click", () => {
      if (state.isAdmin) confirmMatch(report.id, confirm);
      else openAdminDialog();
    });
    content.append(top, title, description, meta, confirm);
    card.append(image, content);
    matchesList.append(card);
  }
}

async function refreshReports() {
  const reports = await api("/api/reports");
  state.reports = reports;
  renderReports(reports);
}

async function refreshActiveMatches() {
  if (!state.activeReportId) return;
  const matches = await api(`/api/matches/${state.activeReportId}`);
  renderMatches(matches);
}

async function saveReportEdit(report, editor) {
  const controls = [...editor.querySelectorAll("button, select, textarea")];
  controls.forEach((control) => { control.disabled = true; });
  let saved = false;
  try {
    const response = await api(`/api/reports/${report.id}`, {
      method: "PUT",
      body: JSON.stringify({
        kind: editor.elements.kind.value,
        description: editor.elements.description.value.trim(),
      }),
    });
    saved = true;
    if (state.activeReportId === report.id) {
      renderMatches(response.matches);
    } else {
      await refreshActiveMatches();
    }
    await refreshReports();
    showMessage(`Report #${report.id} updated.`);
  } catch (error) {
    showMessage(
      saved
        ? `Report updated, but the page could not be fully refreshed: ${error.message}`
        : error.message,
      true,
    );
    if (saved) {
      refreshReports().catch((refreshError) =>
        showMessage(`Report updated, but the report list could not be refreshed: ${refreshError.message}`, true));
    }
  } finally {
    controls.forEach((control) => { control.disabled = false; });
  }
}

async function deleteReport(report, button) {
  const kind = report.kind === "lost" ? "lost" : "found";
  if (!window.confirm(`Delete this ${kind} report for “${report.object_name}”? This cannot be undone.`)) {
    return;
  }
  button.disabled = true;
  try {
    const result = await api(`/api/reports/${report.id}`, { method: "DELETE" });
    if (state.activeReportId === report.id) {
      state.activeReportId = 0;
      renderMatches([]);
    }
    const refreshErrors = [];
    if (state.activeReportId) {
      try {
        await refreshActiveMatches();
      } catch (error) {
        refreshErrors.push(`matches: ${error.message}`);
      }
    }
    try {
      await refreshReports();
    } catch (error) {
      refreshErrors.push(`report list: ${error.message}`);
    }
    let status = `Report #${report.id} deleted.`;
    if (result.photo_cleanup_warning) {
      status += ` ${result.photo_cleanup_warning}`;
    }
    if (refreshErrors.length) status += ` The page could not be fully refreshed (${refreshErrors.join("; ")}).`;
    showMessage(status, Boolean(result.photo_cleanup_warning || refreshErrors.length));
  } catch (error) {
    try {
      await refreshReports();
      showMessage(error.message, true);
    } catch (refreshError) {
      showMessage(`${error.message} The report list could not be refreshed: ${refreshError.message}`, true);
    }
  } finally {
    button.disabled = false;
  }
}

async function confirmMatch(candidateId, button) {
  if (!state.activeReportId || state.confirmingMatch) return;
  state.confirmingMatch = true;
  document.querySelectorAll(".confirm-button").forEach((item) => {
    item.disabled = true;
  });
  try {
    await api("/api/matches/confirm", {
      method: "POST",
      body: JSON.stringify({ report_id: state.activeReportId, candidate_id: candidateId }),
    });
    showMessage("Match confirmed. Both reports are now closed.");
    state.activeReportId = 0;
    renderMatches([]);
    try {
      await refreshReports();
    } catch (error) {
      showMessage(`Match confirmed, but the report list could not be refreshed: ${error.message}`, true);
    }
  } catch (error) {
    showMessage(error.message, true);
    document.querySelectorAll(".confirm-button").forEach((item) => {
      item.disabled = false;
    });
  } finally {
    state.confirmingMatch = false;
  }
}

async function loadClasses() {
  const classes = await api("/api/classes");
  state.classes = classes;
  resetDetectionOptions();
}

function updateAdminControls() {
  $("#admin-login-open").hidden = state.isAdmin;
  $("#admin-logout").hidden = !state.isAdmin;
  refreshReports().catch((error) => showMessage(error.message, true));
  if (state.activeReportId) {
    refreshActiveMatches().catch((error) => showMessage(error.message, true));
  }
}

function openAdminDialog() {
  const dialog = $("#admin-dialog");
  $("#admin-login-message").textContent = "";
  if (!dialog.open) dialog.showModal();
  $("#admin-password").focus();
}

$("#admin-login-open").addEventListener("click", openAdminDialog);
$("#admin-login-cancel").addEventListener("click", () => $("#admin-dialog").close());
$("#admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("#admin-login-submit");
  const message = $("#admin-login-message");
  const password = $("#admin-password").value;
  submit.disabled = true;
  message.classList.remove("error");
  message.textContent = "Signing in…";
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    state.isAdmin = true;
    state.csrfToken = result.csrf_token;
    $("#admin-password").value = "";
    $("#admin-dialog").close();
    updateAdminControls();
    showMessage("Signed in as administrator.");
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
    $("#admin-password").focus();
  } finally {
    submit.disabled = false;
  }
});

$("#admin-logout").addEventListener("click", async () => {
  const button = $("#admin-logout");
  button.disabled = true;
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
    state.isAdmin = false;
    state.csrfToken = "";
    updateAdminControls();
    showMessage("Signed out.");
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    state.kind = button.dataset.kind;
    document.querySelectorAll(".segment").forEach((item) =>
      item.classList.toggle("active", item === button));
    $("#submit-label").textContent = state.kind === "lost"
      ? "Save lost item"
      : "Save found item";
  });
});

$("#description").addEventListener("input", (event) => {
  $("#char-count").textContent = `${event.target.value.length} / 1000`;
});
classSelect.addEventListener("change", () => {
  const index = Number(classSelect.value);
  state.selectedDetection = Number.isInteger(index) ? state.detections[index] || null : null;
  showSelectedDetection();
  updateSubmitState();
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files?.[0];
  if (!file) return;
  const fileToken = ++state.fileToken;
  if (!file.type.startsWith("image/") || file.size > 8 * 1024 * 1024) {
    showMessage("Choose an image no larger than 8 MB.", true);
    fileInput.value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    if (fileToken !== state.fileToken) return;
    const image = new Image();
    image.onload = () => {
      if (fileToken !== state.fileToken) return;
      try {
        const photoCanvas = createCanvas(image.naturalWidth, image.naturalHeight);
        photoCanvas.context.drawImage(image, 0, 0);
        showPhoto(photoCanvas.canvas.toDataURL("image/jpeg", 0.86));
        detectPhoto();
      } catch (error) {
        showMessage(error.message, true);
      }
    };
    image.onerror = () => {
      if (fileToken === state.fileToken) showMessage("Could not open the image.", true);
    };
    image.src = reader.result;
  };
  reader.onerror = () => {
    if (fileToken === state.fileToken) showMessage("Could not read the selected file.", true);
  };
  reader.readAsDataURL(file);
});

$("#camera-start").addEventListener("click", async () => {
  const startButton = $("#camera-start");
  const shotButton = $("#camera-shot");
  if (state.cameraStream || startButton.disabled) return;
  startButton.disabled = true;
  shotButton.disabled = true;
  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Camera access is not available in this browser. Use “Choose photo” instead.");
    }
    stopCamera();
    state.cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } },
      audio: false,
    });
    video.srcObject = state.cameraStream;
    $("#camera-wrap").hidden = false;
    await video.play();
    shotButton.disabled = !video.videoWidth || !video.videoHeight;
    showMessage("Camera is on. The photo will be uploaded to Render when you submit.");
  } catch (error) {
    showMessage(error.message || "Could not access the camera. Check your browser permissions.", true);
    stopCamera();
  } finally {
    startButton.disabled = false;
  }
});

$("#camera-shot").addEventListener("click", () => {
  if (!video.videoWidth || !video.videoHeight) {
    showMessage("The camera is not ready yet. Please wait a moment.", true);
    return;
  }
  try {
    const photoCanvas = createCanvas(video.videoWidth, video.videoHeight);
    photoCanvas.context.drawImage(video, 0, 0);
    showPhoto(photoCanvas.canvas.toDataURL("image/jpeg", 0.86));
    stopCamera();
    detectPhoto();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#photo-remove").addEventListener("click", clearPhoto);
$("#refresh-reports").addEventListener("click", () => {
  refreshReports().catch((error) => showMessage(error.message, true));
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.photoData || !state.selectedDetection || !state.uploadPhotoData) return;
  submitButton.disabled = true;
  showMessage("Saving your report and looking for matches…");
  try {
    stopCamera();
    const detection = state.selectedDetection;
    const response = await api("/api/reports", {
      method: "POST",
      body: JSON.stringify({
        kind: state.kind,
        object_class: detection.object_class,
        description: $("#description").value.trim(),
        photo: state.uploadPhotoData,
        color: detection.color,
        features: detection.features,
        detection_token: detection.detection_token,
      }),
    });
    state.activeReportId = response.report.id;
    renderMatches(response.matches);
    clearPhoto();
    form.reset();
    $("#char-count").textContent = "0 / 1000";
    updateSubmitState();
    showMessage(response.matches.length
      ? `Report #${response.report.id} saved. Review the possible matches.`
      : `Report #${response.report.id} saved. No matching reports yet.`);
    try {
      await refreshReports();
    } catch (error) {
      showMessage(`Report saved, but the list could not be refreshed: ${error.message}`, true);
    }
  } catch (error) {
    showMessage(error.message, true);
  } finally {
    updateSubmitState();
  }
});

Promise.all([loadClasses(), refreshReports(), api("/api/health"), api("/api/auth")])
  .then(([, , health, auth]) => {
    $("#open-count").textContent = health.open_reports;
    state.isAdmin = auth.is_admin;
    state.csrfToken = auth.csrf_token;
    $("#admin-login-open").hidden = state.isAdmin;
    $("#admin-logout").hidden = !state.isAdmin;
    renderReports(state.reports);
    if (!health.detection.available) {
      showMessage(`Server detection is unavailable: ${health.detection.error}`, true);
    }
  })
  .catch((error) => {
    showMessage(`Could not connect to the service: ${error.message}`, true);
    reportsList.replaceChildren();
    const warning = document.createElement("p");
    warning.className = "form-message error";
    warning.textContent = "Please reload the page or try again later.";
    reportsList.append(warning);
  });

updateThemeControl();
window.addEventListener("pagehide", stopCamera);
