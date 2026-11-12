"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const STORAGE_KEY = "lost-found-pages-reports-v1";
const MAX_PHOTO_BYTES = 480_000;
const MAX_FILE_BYTES = 8 * 1024 * 1024;
const CATEGORIES = [
  "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
  "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird",
  "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe",
  "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis",
  "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
  "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork", "knife",
  "spoon", "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
  "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant", "bed",
  "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
  "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book",
  "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
];

const form = $("#report-form");
const categorySelect = $("#object-class");
const fileInput = $("#photo-file");
const video = $("#camera-video");
const message = $("#form-message");
const reportList = $("#reports-list");
const matchList = $("#matches-list");
const matchesEmpty = $("#matches-empty");
let reports = [];
let currentKind = "lost";
let currentPhoto = "";
let cameraStream = null;
let activeReportId = null;
let fileToken = 0;
let confirmingMatch = false;

function showMessage(text, isError = false) {
  message.textContent = text;
  message.classList.toggle("error", isError);
}

function readReports() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(parsed)) throw new Error("Stored reports are not a list.");
    return parsed.filter((report) =>
      report && typeof report.id === "string" &&
      ["lost", "found"].includes(report.kind) &&
      ["open", "matched"].includes(report.status) &&
      CATEGORIES.includes(report.category) &&
      typeof report.description === "string" &&
      /^#[0-9a-f]{6}$/i.test(report.color) &&
      (report.photo === "" || (typeof report.photo === "string" && report.photo.startsWith("data:image/jpeg;base64,"))));
  } catch {
    showMessage("Could not read saved reports from this browser. You can still create new reports.", true);
    return [];
  }
}

function persist(nextReports) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(nextReports));
    reports = nextReports;
    return true;
  } catch {
    showMessage("Could not save this report in browser storage. Remove a large photo or clear old demo reports and try again.", true);
    return false;
  }
}

function colorName(hex) {
  const red = parseInt(hex.slice(1, 3), 16);
  const green = parseInt(hex.slice(3, 5), 16);
  const blue = parseInt(hex.slice(5, 7), 16);
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  if (max < 48) return "black";
  if (delta < 25) return max > 210 ? "white" : "gray";
  let hue;
  if (max === red) hue = 60 * (((green - blue) / delta) % 6);
  else if (max === green) hue = 60 * ((blue - red) / delta + 2);
  else hue = 60 * ((red - green) / delta + 4);
  if (hue < 0) hue += 360;
  if (hue < 15 || hue >= 345) return "red";
  if (hue < 45) return "orange";
  if (hue < 70) return "yellow";
  if (hue < 165) return "green";
  if (hue < 200) return "teal";
  if (hue < 255) return "blue";
  if (hue < 290) return "purple";
  return "magenta";
}

function updateColorName(input = $("#main-color"), label = $("#color-name")) {
  label.textContent = colorName(input.value);
}

function updateThemeControl() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#theme-toggle").setAttribute("aria-pressed", String(dark));
  $("#theme-toggle").setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  $("#theme-icon").textContent = dark ? "☀" : "☾";
  $("#theme-label").textContent = dark ? "Light theme" : "Dark theme";
  $('meta[name="theme-color"]').content = dark ? "#0c1628" : "#f3f6fb";
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unknown";
  return date.toLocaleString("en-US", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

function statusTag(report) {
  const tag = document.createElement("span");
  tag.className = `status-tag${report.status === "matched" ? " matched" : ""}`;
  tag.textContent = report.status === "matched" ? "Matched" : report.kind === "lost" ? "Lost" : "Found";
  return tag;
}

function appendReportImage(parent, report, match = false) {
  if (report.photo) {
    const image = document.createElement("img");
    image.className = match ? "match-photo" : "report-thumb";
    image.src = report.photo;
    image.alt = match ? `Photo of ${report.category}` : "";
    parent.append(image);
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = match ? "match-placeholder" : "report-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    placeholder.textContent = "⌕";
    parent.append(placeholder);
  }
}

function categoryOption(value) {
  return value.split(" ").map((word) => word === "tv" ? "TV" :
    word === "cell" ? "Cell" : word[0].toUpperCase() + word.slice(1)).join(" ");
}

function renderReports() {
  $("#open-count").textContent = reports.filter((report) => report.status === "open").length;
  reportList.replaceChildren();
  if (!reports.length) {
    const empty = document.createElement("p");
    empty.className = "field-hint";
    empty.textContent = "No reports yet. Add the first one above.";
    reportList.append(empty);
    return;
  }

  for (const report of [...reports].sort((a, b) => b.createdAt - a.createdAt).slice(0, 12)) {
    const row = document.createElement("article");
    row.className = "report-row";
    appendReportImage(row, report);
    const details = document.createElement("div");
    details.className = "report-details";
    const title = document.createElement("strong");
    title.textContent = categoryOption(report.category);
    const sub = document.createElement("small");
    sub.textContent = `${report.kind === "lost" ? "Lost" : "Found"} · ${colorName(report.color)} · ${formatDate(report.createdAt)}`;
    details.append(title, sub);
    row.append(details, statusTag(report));

    if (report.status === "open") {
      const actions = document.createElement("div");
      actions.className = "report-actions";
      const edit = document.createElement("button");
      edit.className = "report-action";
      edit.type = "button";
      edit.textContent = "Edit";
      edit.setAttribute("aria-label", `Edit ${report.kind} report for ${categoryOption(report.category)}`);
      const remove = document.createElement("button");
      remove.className = "report-action report-action-danger";
      remove.type = "button";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete ${report.kind} report for ${categoryOption(report.category)}`);
      actions.append(edit, remove);
      row.append(actions);
      edit.addEventListener("click", () => toggleEditor(row, report, edit));
      remove.addEventListener("click", () => deleteReport(report));
    } else {
      const note = document.createElement("small");
      note.className = "report-readonly-note";
      note.textContent = "Matched reports are read-only.";
      row.append(note);
    }
    reportList.append(row);
  }
}

function toggleEditor(row, report, editButton) {
  const existing = $(".report-edit-form", row);
  if (existing) {
    existing.remove();
    editButton.focus();
    return;
  }
  const editor = document.createElement("form");
  editor.className = "report-edit-form";

  const kindLabel = document.createElement("label");
  kindLabel.className = "field-label";
  kindLabel.textContent = "Report type";
  const kind = document.createElement("select");
  kind.name = "kind";
  kind.add(new Option("Lost", "lost"));
  kind.add(new Option("Found", "found"));
  kind.value = report.kind;
  kindLabel.append(kind);

  const categoryLabel = document.createElement("label");
  categoryLabel.className = "field-label";
  categoryLabel.textContent = "Item category";
  const category = document.createElement("select");
  category.name = "category";
  for (const item of CATEGORIES) category.add(new Option(categoryOption(item), item));
  category.value = report.category;
  categoryLabel.append(category);

  const descriptionLabel = document.createElement("label");
  descriptionLabel.className = "field-label";
  descriptionLabel.textContent = "Description";
  const description = document.createElement("textarea");
  description.name = "description";
  description.maxLength = 1000;
  description.rows = 2;
  description.value = report.description;
  descriptionLabel.append(description);

  const colorLabel = document.createElement("label");
  colorLabel.className = "field-label";
  colorLabel.textContent = "Main color";
  const color = document.createElement("input");
  color.name = "color";
  color.type = "color";
  color.value = report.color;
  colorLabel.append(color);

  const actions = document.createElement("div");
  actions.className = "report-edit-actions";
  const save = document.createElement("button");
  save.className = "button button-primary";
  save.type = "submit";
  save.textContent = "Save changes";
  const cancel = document.createElement("button");
  cancel.className = "button button-secondary";
  cancel.type = "button";
  cancel.textContent = "Cancel";
  actions.append(save, cancel);
  editor.append(kindLabel, categoryLabel, descriptionLabel, colorLabel, actions);
  row.append(editor);
  cancel.addEventListener("click", () => {
    editor.remove();
    editButton.focus();
  });
  editor.addEventListener("submit", (event) => {
    event.preventDefault();
    const updated = {
      ...report,
      kind: kind.value,
      category: category.value,
      description: description.value.trim(),
      color: color.value,
    };
    const next = reports.map((item) => item.id === report.id ? updated : item);
    if (persist(next)) {
      renderReports();
      refreshActiveMatches();
      showMessage(`Report ${report.id} updated.`);
    }
  });
  description.focus();
}

function colorSimilarity(first, second) {
  const a = first.match(/[0-9a-f]{2}/gi).map((part) => parseInt(part, 16));
  const b = second.match(/[0-9a-f]{2}/gi).map((part) => parseInt(part, 16));
  const distance = Math.sqrt(a.reduce((sum, value, index) =>
    sum + ((value - b[index]) / 255) ** 2, 0) / 3);
  return Math.max(0, 1 - distance);
}

function findMatches(source) {
  return reports
    .filter((candidate) =>
      candidate.id !== source.id &&
      candidate.status === "open" &&
      candidate.kind !== source.kind &&
      candidate.category === source.category)
    .map((report) => {
      const colorScore = colorSimilarity(source.color, report.color);
      const textScore = source.description && source.description.trim().toLowerCase() ===
        report.description.trim().toLowerCase() ? 1 : 0;
      return { report, score: 0.65 + colorScore * 0.3 + textScore * 0.05 };
    })
    .sort((a, b) => b.score - a.score)
    .slice(0, 10);
}

function renderMatches(matches) {
  matchList.replaceChildren();
  matchesEmpty.hidden = matches.length > 0;
  for (const match of matches) {
    const report = match.report;
    const card = document.createElement("article");
    card.className = "match-item";
    appendReportImage(card, report, true);
    const content = document.createElement("div");
    content.className = "match-main";
    const top = document.createElement("div");
    top.className = "match-topline";
    const kind = document.createElement("span");
    kind.className = "kind-tag";
    kind.textContent = report.kind === "lost" ? "Lost" : "Found";
    const score = document.createElement("span");
    score.className = "match-score";
    score.textContent = `Demo similarity ${Math.round(match.score * 100)}%`;
    top.append(kind, score);
    const title = document.createElement("h3");
    title.textContent = categoryOption(report.category);
    const description = document.createElement("p");
    description.className = "match-description";
    description.textContent = report.description || "No description";
    const meta = document.createElement("div");
    meta.className = "match-meta";
    const chip = document.createElement("span");
    chip.className = "color-chip";
    chip.style.background = report.color;
    chip.setAttribute("aria-label", `Color: ${colorName(report.color)}`);
    const color = document.createElement("span");
    color.textContent = colorName(report.color);
    const date = document.createElement("span");
    date.textContent = formatDate(report.createdAt);
    meta.append(chip, color, date);
    const confirm = document.createElement("button");
    confirm.className = "confirm-button";
    confirm.type = "button";
    confirm.textContent = "Confirm match";
    confirm.addEventListener("click", () => confirmMatch(report.id));
    content.append(top, title, description, meta, confirm);
    card.append(content);
    matchList.append(card);
  }
}

function refreshActiveMatches() {
  const source = reports.find((report) => report.id === activeReportId && report.status === "open");
  renderMatches(source ? findMatches(source) : []);
}

function clearPhoto() {
  currentPhoto = "";
  fileInput.value = "";
  $("#photo-preview").removeAttribute("src");
  $("#photo-preview-wrap").hidden = true;
}

function stopCamera() {
  if (cameraStream) {
    for (const track of cameraStream.getTracks()) track.stop();
    cameraStream = null;
  }
  video.srcObject = null;
  $("#camera-shot").disabled = true;
  $("#camera-wrap").hidden = true;
}

async function shrinkPhoto(file) {
  const image = new Image();
  const objectUrl = URL.createObjectURL(file);
  try {
    image.src = objectUrl;
    await image.decode();
    const maxSide = 960;
    const scale = Math.min(1, maxSide / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("This browser cannot resize photos.");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    let quality = 0.72;
    let data = canvas.toDataURL("image/jpeg", quality);
    while (data.length * 0.75 > MAX_PHOTO_BYTES && quality > 0.4) {
      quality -= 0.08;
      data = canvas.toDataURL("image/jpeg", quality);
    }
    if (data.length * 0.75 > MAX_PHOTO_BYTES) {
      throw new Error("The photo is too large to save in browser storage.");
    }
    return data;
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function setPhoto(file) {
  if (!file.type.startsWith("image/") || file.size > MAX_FILE_BYTES) {
    showMessage("Choose an image no larger than 8 MB.", true);
    return;
  }
  const token = ++fileToken;
  currentPhoto = "";
  $("#photo-preview").removeAttribute("src");
  $("#photo-preview-wrap").hidden = true;
  try {
    const data = await shrinkPhoto(file);
    if (token !== fileToken) return;
    currentPhoto = data;
    $("#photo-preview").src = data;
    $("#photo-preview-wrap").hidden = false;
    showMessage("Photo resized and ready. It stays in this browser.");
  } catch (error) {
    if (token === fileToken) showMessage(error.message || "Could not read this photo.", true);
  }
}

function confirmMatch(candidateId) {
  if (confirmingMatch || !activeReportId) return;
  const source = reports.find((report) => report.id === activeReportId && report.status === "open");
  const candidate = reports.find((report) => report.id === candidateId && report.status === "open");
  if (!source || !candidate) {
    showMessage("These reports are no longer available for matching.", true);
    refreshActiveMatches();
    return;
  }
  if (!window.confirm("Confirm this possible match? Both reports will be marked as matched.")) return;
  confirmingMatch = true;
  const now = new Date().toISOString();
  const next = reports.map((report) => {
    if (report.id === source.id) return { ...report, status: "matched", matchedId: candidate.id, matchedAt: now };
    if (report.id === candidate.id) return { ...report, status: "matched", matchedId: source.id, matchedAt: now };
    return report;
  });
  if (persist(next)) {
    renderReports();
    renderMatches([]);
    activeReportId = null;
    showMessage("Match confirmed. Both reports are now closed.");
  }
  confirmingMatch = false;
}

function deleteReport(report) {
  if (report.status !== "open") return;
  if (!window.confirm(`Delete this ${report.kind} report for “${categoryOption(report.category)}”? This cannot be undone.`)) return;
  if (!persist(reports.filter((item) => item.id !== report.id))) return;
  if (activeReportId === report.id) {
    activeReportId = null;
    renderMatches([]);
  } else {
    refreshActiveMatches();
  }
  renderReports();
  showMessage("Report deleted from this browser.");
}

function initialize() {
  for (const item of CATEGORIES) categorySelect.add(new Option(categoryOption(item), item));
  reports = readReports();
  renderReports();
  updateThemeControl();
  updateColorName();

  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("lf-pages-theme", next);
    } catch {
      showMessage("Theme changed, but this browser could not save the preference.", true);
    }
    updateThemeControl();
  });

  $("#main-color").addEventListener("input", () => updateColorName());
  document.querySelectorAll(".segment").forEach((button) => {
    button.addEventListener("click", () => {
      currentKind = button.dataset.kind;
      document.querySelectorAll(".segment").forEach((item) =>
        item.classList.toggle("active", item === button));
      $("#submit-label").textContent = currentKind === "lost"
        ? "Save lost item and find matches"
        : "Save found item and find matches";
    });
  });
  $("#description").addEventListener("input", (event) => {
    $("#char-count").textContent = `${event.target.value.length} / 1000`;
  });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (file) setPhoto(file);
  });
  $("#photo-remove").addEventListener("click", clearPhoto);
  $("#camera-start").addEventListener("click", async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showMessage("Camera access is not available here. Use “Choose photo” instead.", true);
      return;
    }
    try {
      stopCamera();
      cameraStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" } }, audio: false,
      });
      video.srcObject = cameraStream;
      $("#camera-wrap").hidden = false;
      await video.play();
      $("#camera-shot").disabled = !video.videoWidth || !video.videoHeight;
      showMessage("Camera is on. The photo will stay in this browser.");
    } catch (error) {
      stopCamera();
      showMessage(error.message || "Could not access the camera. Check your browser permissions.", true);
    }
  });
  $("#camera-shot").addEventListener("click", async () => {
    if (!video.videoWidth || !video.videoHeight) {
      showMessage("The camera is not ready yet. Please wait a moment.", true);
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext("2d");
    if (!context) {
      showMessage("This browser cannot capture a photo.", true);
      return;
    }
    context.drawImage(video, 0, 0);
    canvas.toBlob(async (blob) => {
      if (!blob) {
        showMessage("Could not capture a photo.", true);
        return;
      }
      await setPhoto(new File([blob], "camera.jpg", { type: "image/jpeg" }));
      stopCamera();
    }, "image/jpeg", 0.85);
  });
  $("#refresh-reports").addEventListener("click", () => {
    reports = readReports();
    renderReports();
    refreshActiveMatches();
    showMessage("Reports refreshed from this browser’s storage.");
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const category = categorySelect.value;
    if (!CATEGORIES.includes(category)) {
      showMessage("Choose an item category.", true);
      return;
    }
    const report = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
      kind: currentKind,
      status: "open",
      category,
      description: $("#description").value.trim(),
      color: $("#main-color").value,
      photo: currentPhoto,
      createdAt: Date.now(),
    };
    const next = [...reports, report];
    if (!persist(next)) return;
    activeReportId = report.id;
    renderReports();
    renderMatches(findMatches(report));
    form.reset();
    currentKind = "lost";
    document.querySelectorAll(".segment").forEach((button) =>
      button.classList.toggle("active", button.dataset.kind === "lost"));
    $("#submit-label").textContent = "Save report and find matches";
    $("#char-count").textContent = "0 / 1000";
    updateColorName();
    clearPhoto();
    showMessage(findMatches(report).length
      ? "Report saved in this browser. Review the possible matches."
      : "Report saved in this browser. No matching reports yet.");
  });

  window.addEventListener("pagehide", stopCamera);
  if (navigator.storage && navigator.storage.persist) {
    navigator.storage.persist().catch(() => {});
  }
}

initialize();
