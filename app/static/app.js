/* ShelfSense "New audit" page: category, photo upload, live camera, planogram editor, Audit button.
   Plain JavaScript, no libraries. */
(function () {
  "use strict";

  var MAX_BYTES = 5 * 1024 * 1024;
  var TYPES = { "image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP" };

  function $(id) { return document.getElementById(id); }

  var form = $("audit-form");
  if (!form) return;

  var fileInput = $("file");
  var dropzone = $("dropzone");
  var preview = $("preview");
  var previewImg = $("preview-img");
  var fileName = $("file-name");
  var fileInfo = $("file-info");
  var fileError = $("file-error");

  var planogram = $("planogram");
  var hlCode = $("hl-code");
  var hlPre = hlCode.parentNode;
  var planoStatus = $("plano-status");
  var planoErrors = $("plano-errors");
  var planoMismatch = $("plano-mismatch");

  var submitBtn = $("audit-btn");
  var ctaHint = $("cta-hint");

  var fileOk = false;
  var planoOk = false;
  var objectUrl = null;

  /* ------------------------------------------------------------ category */

  var samples = {};
  try { samples = JSON.parse($("samples-data").textContent); } catch (e) { samples = {}; }
  var categorySel = $("category");
  var categoryName = $("category-name");
  var categoryHint = $("category-hint");

  function sampleText(key) {
    return JSON.stringify(samples[key] || samples.custom || { rows: [] }, null, 2);
  }

  // Same data, whatever order the keys are written in.
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") {
      var sorted = {};
      Object.keys(value).sort().forEach(function (key) { sorted[key] = canonical(value[key]); });
      return sorted;
    }
    return value;
  }

  function sameJson(a, b) {
    try { return JSON.stringify(canonical(JSON.parse(a))) === JSON.stringify(canonical(JSON.parse(b))); }
    catch (e) { return false; }
  }

  var lastSample = sampleText(categorySel.value);

  // Warn when the planogram is still another category's example (e.g. the grocery sample
  // while "Clothing" is selected): the AI could never match anything on the shelf.
  function updateMismatch() {
    var current = planogram.value;
    var chosen = categorySel.options[categorySel.selectedIndex].text;
    var text = "";
    Object.keys(samples).forEach(function (key) {
      if (key === categorySel.value || key === "custom") return;
      if (!sameJson(current, JSON.stringify(samples[key]))) return;
      var other = key;
      Array.prototype.forEach.call(categorySel.options, function (option) {
        if (option.value === key) other = option.text;
      });
      text = 'This planogram is the "' + other + '" example, but the category is "' + chosen + '". ' +
        'Press Load sample to use the ' + chosen + ' example, or edit the planogram to match your shelf.';
    });
    planoMismatch.textContent = text;
    planoMismatch.hidden = !text;
  }

  function onCategoryChange() {
    var option = categorySel.options[categorySel.selectedIndex];
    categoryHint.textContent = option.getAttribute("data-hint") || "";
    categoryName.hidden = categorySel.value !== "custom";
    // Swap in the new category's example, but never overwrite a planogram the user has edited.
    if (!planogram.value.trim() || sameJson(planogram.value, lastSample)) {
      planogram.value = sampleText(categorySel.value);
      onPlanogramChange();
    }
    lastSample = sampleText(categorySel.value);
    updateMismatch();
  }
  categorySel.addEventListener("change", onCategoryChange);

  /* ---------------------------------------------------------------- photo */

  function formatBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function showFileError(message) {
    fileError.textContent = message;
    fileError.hidden = false;
    dropzone.classList.add("invalid");
  }

  function clearFileError() {
    fileError.hidden = true;
    fileError.textContent = "";
    dropzone.classList.remove("invalid");
  }

  // When the page was opened from an earlier audit, its photo is reused unless you pick another.
  var reuseField = $("reuse-image");

  function showReusedPhoto(auditId) {
    previewImg.onload = function () {
      fileInfo.textContent = "Reused from an earlier audit · " +
        previewImg.naturalWidth + "×" + previewImg.naturalHeight + " px";
    };
    previewImg.src = "/audits/" + encodeURIComponent(auditId) + "/image";
    fileName.textContent = "Photo from audit " + auditId;
    fileInfo.textContent = "Reused from an earlier audit";
    dropzone.hidden = true;
    preview.hidden = false;
    fileOk = true;
    updateCta();
  }

  function resetFile() {
    if (reuseField) reuseField.value = "";
    if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    fileInput.value = "";
    previewImg.removeAttribute("src");
    preview.hidden = true;
    dropzone.hidden = false;
    fileOk = false;
    updateCta();
  }

  function rejectFile(message) {
    resetFile();
    showFileError(message);
  }

  function setFile(file) {
    clearFileError();
    if (!file) { resetFile(); return; }

    var kind = TYPES[file.type];
    if (!kind) {
      rejectFile('"' + file.name + '" is not a supported format. Please use a JPEG, PNG or WEBP image.');
      return;
    }
    if (file.size === 0) {
      rejectFile('"' + file.name + '" is empty. Please choose another image.');
      return;
    }
    if (file.size > MAX_BYTES) {
      rejectFile('"' + file.name + '" is ' + formatBytes(file.size) + ". The limit is 5 MB.");
      return;
    }

    if (reuseField) reuseField.value = ""; // a newly chosen photo replaces the reused one

    // Put the file into the real <input> so the form uploads it (needed for drag and drop and the camera).
    try {
      var dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
    } catch (err) {
      /* Older browsers: the file chooser path still works. */
    }

    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(file);
    previewImg.onload = function () {
      fileInfo.textContent = formatBytes(file.size) + " \u00b7 " + kind + " \u00b7 " +
        previewImg.naturalWidth + "\u00d7" + previewImg.naturalHeight + " px";
    };
    previewImg.onerror = function () {
      rejectFile('"' + file.name + '" could not be read as an image. Please try another file.');
    };
    previewImg.src = objectUrl;
    fileName.textContent = file.name;
    fileInfo.textContent = formatBytes(file.size) + " \u00b7 " + kind;

    dropzone.hidden = true;
    preview.hidden = false;
    fileOk = true;
    updateCta();
  }

  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  fileInput.addEventListener("change", function () { setFile(fileInput.files[0]); });
  $("replace-btn").addEventListener("click", function () { fileInput.click(); });
  $("remove-btn").addEventListener("click", function () { clearFileError(); resetFile(); });

  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (e) {
      e.preventDefault();
      dropzone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function () { dropzone.classList.remove("drag"); });
  });
  dropzone.addEventListener("drop", function (e) {
    e.preventDefault();
    var files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    setFile(files[0]);
    if (files.length > 1 && fileOk) {
      fileError.textContent = "Only the first image was used (" + files.length + " files were dropped).";
      fileError.hidden = false;
    }
  });
  // A file dropped outside the zone should not make the browser navigate away to it.
  window.addEventListener("dragover", function (e) { e.preventDefault(); });
  window.addEventListener("drop", function (e) { e.preventDefault(); });

  /* --------------------------------------------------------------- camera */

  var tabUpload = $("tab-upload");
  var tabCamera = $("tab-camera");
  var panelUpload = $("panel-upload");
  var panelCamera = $("panel-camera");
  var camMsg = $("camera-msg");
  var camStage = $("camera-stage");
  var camVideo = $("camera-video");
  var camStart = $("cam-start");
  var camStop = $("cam-stop");
  var camSwitch = $("cam-switch");
  var camCapture = $("cam-capture");
  var capturedBox = $("captured");
  var capturedImg = $("captured-img");
  var capturedInfo = $("captured-info");

  var camStream = null;
  var camDevices = [];
  var capturedBlob = null;
  var capturedUrl = null;

  function camSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function setCamMessage(text, isError) {
    camMsg.textContent = text || "";
    camMsg.classList.toggle("error", !!isError);
  }

  function showCam(state) {
    var live = state === "live";
    var captured = state === "captured";
    camStage.hidden = !live;
    camStart.hidden = live || captured;
    camCapture.hidden = !live;
    camStop.hidden = !live;
    camSwitch.hidden = !(live && camDevices.length > 1);
    capturedBox.hidden = !captured;
  }

  function stopStream() {
    if (camStream) {
      camStream.getTracks().forEach(function (track) { track.stop(); });
      camStream = null;
    }
    camVideo.srcObject = null;
  }

  function stopCamera() {
    stopStream();
    if (capturedBox.hidden) showCam("idle");
  }

  function explainCameraError(err) {
    var name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
      return "Camera permission was denied. Allow camera access in your browser's address bar, then press Start camera again.";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return "No camera was found on this device. You can upload a photo instead.";
    }
    if (name === "NotReadableError" || name === "TrackStartError") {
      return "The camera is being used by another app or browser tab. Close it and try again.";
    }
    return "The camera could not be started" + (name ? " (" + name + ")" : "") + ". You can upload a photo instead.";
  }

  async function refreshDevices() {
    try {
      var all = await navigator.mediaDevices.enumerateDevices();
      camDevices = all.filter(function (d) { return d.kind === "videoinput"; });
    } catch (err) {
      camDevices = [];
    }
  }

  async function startCamera(deviceId) {
    stopStream();
    if (!camSupported()) {
      setCamMessage("Camera access is not available in this browser or on this connection. " +
        "Cameras work on secure (HTTPS) pages and on localhost. Please upload a photo instead.", true);
      return;
    }
    setCamMessage("Starting the camera... your browser may ask for permission.");

    var constraints = deviceId
      ? { video: { deviceId: { exact: deviceId } }, audio: false }
      : { video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false };

    try {
      camStream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (err) {
      if (err && (err.name === "OverconstrainedError" || err.name === "ConstraintNotSatisfiedError")) {
        try {
          camStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
        } catch (err2) {
          setCamMessage(explainCameraError(err2), true);
          showCam("idle");
          return;
        }
      } else {
        setCamMessage(explainCameraError(err), true);
        showCam("idle");
        return;
      }
    }

    camVideo.srcObject = camStream;
    try { await camVideo.play(); } catch (e) { /* autoplay can be blocked; the stream still attaches */ }
    await refreshDevices();
    showCam("live");
    setCamMessage("Point the camera at the shelf, keep it steady and front-on, then press Capture photo.");
  }

  function capturePhoto() {
    var w = camVideo.videoWidth;
    var h = camVideo.videoHeight;
    if (!w || !h) {
      setCamMessage("The camera is not ready yet. Wait a moment and try again.", true);
      return;
    }
    var canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    canvas.getContext("2d").drawImage(camVideo, 0, 0, w, h);
    canvas.toBlob(function (blob) {
      if (!blob) {
        setCamMessage("The photo could not be captured. Please try again.", true);
        return;
      }
      capturedBlob = blob;
      if (capturedUrl) URL.revokeObjectURL(capturedUrl);
      capturedUrl = URL.createObjectURL(blob);
      capturedImg.src = capturedUrl;
      capturedInfo.textContent = formatBytes(blob.size) + " \u00b7 JPEG \u00b7 " + w + "\u00d7" + h + " px";
      stopStream(); // release the camera as soon as we have the photo
      showCam("captured");
      setCamMessage("Check the photo. Is the whole shelf visible and sharp?");
    }, "image/jpeg", 0.92);
  }

  function retake() {
    capturedBlob = null;
    if (capturedUrl) { URL.revokeObjectURL(capturedUrl); capturedUrl = null; }
    capturedImg.removeAttribute("src");
    showCam("idle");
    startCamera();
  }

  function confirmCapture() {
    if (!capturedBlob) return;
    var stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
    var file = new File([capturedBlob], "camera-" + stamp + ".jpg", { type: "image/jpeg" });
    capturedBox.hidden = true;
    setMode("upload"); // shows the photo in the normal preview card
    setFile(file);
    if (!fileOk) return;
    if (planoOk) {
      if (form.requestSubmit) form.requestSubmit(submitBtn); else submitBtn.click();
    } else {
      ctaHint.textContent = "Photo added. Fix the planogram, then press Audit Shelf.";
      planogram.focus();
    }
  }

  function setMode(mode) {
    var camera = mode === "camera";
    tabUpload.classList.toggle("active", !camera);
    tabCamera.classList.toggle("active", camera);
    tabUpload.setAttribute("aria-selected", camera ? "false" : "true");
    tabCamera.setAttribute("aria-selected", camera ? "true" : "false");
    panelUpload.hidden = camera;
    panelCamera.hidden = !camera;
    if (camera) {
      if (!camSupported()) {
        camStart.disabled = true;
        setCamMessage("Camera access is not available in this browser or on this connection. " +
          "Cameras work on secure (HTTPS) pages and on localhost. Please upload a photo instead.", true);
      } else if (!camStream && capturedBox.hidden) {
        setCamMessage("Press Start camera. Your browser will ask for permission.");
      }
    } else {
      stopStream();
      showCam("idle");
      capturedBox.hidden = true;
    }
  }

  tabUpload.addEventListener("click", function () { setMode("upload"); });
  tabCamera.addEventListener("click", function () { setMode("camera"); });
  camStart.addEventListener("click", function () { startCamera(); });
  camStop.addEventListener("click", function () {
    stopStream();
    showCam("idle");
    setCamMessage("Camera stopped. Press Start camera to use it again.");
  });
  camSwitch.addEventListener("click", function () {
    if (camDevices.length < 2) return;
    var current = camStream && camStream.getVideoTracks()[0] && camStream.getVideoTracks()[0].getSettings().deviceId;
    var index = camDevices.findIndex(function (d) { return d.deviceId === current; });
    startCamera(camDevices[(index + 1) % camDevices.length].deviceId);
  });
  camCapture.addEventListener("click", capturePhoto);
  $("cam-retake").addEventListener("click", retake);
  $("cam-confirm").addEventListener("click", confirmCapture);
  window.addEventListener("pagehide", stopStream); // never leave the camera on

  /* ------------------------------------------------------ planogram editor */

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Colours JSON: keys, strings, numbers, true/false/null and punctuation.
  function highlight(text) {
    var re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]/g;
    var out = "";
    var last = 0;
    var m;
    while ((m = re.exec(text)) !== null) {
      out += esc(text.slice(last, m.index));
      if (m[1] !== undefined) {
        if (m[2] !== undefined) {
          out += '<span class="tk-key">' + esc(m[1]) + "</span>" +
                 '<span class="tk-punct">' + esc(m[2]) + "</span>";
        } else {
          out += '<span class="tk-str">' + esc(m[1]) + "</span>";
        }
      } else if (m[3] !== undefined) {
        out += '<span class="tk-lit">' + m[3] + "</span>";
      } else if (/^[{}\[\],:]$/.test(m[0])) {
        out += '<span class="tk-punct">' + m[0] + "</span>";
      } else {
        out += '<span class="tk-num">' + m[0] + "</span>";
      }
      last = re.lastIndex;
    }
    out += esc(text.slice(last));
    return out + "\n"; // trailing newline keeps the last empty line aligned
  }

  function syncEditor() {
    hlCode.innerHTML = highlight(planogram.value);
    hlPre.scrollTop = planogram.scrollTop;
    hlPre.scrollLeft = planogram.scrollLeft;
  }

  // Turns the browser's JSON error into something a person can act on.
  function explainJsonError(text, err) {
    var msg = String(err && err.message || "Invalid JSON");
    var line = null;
    var col = null;
    var lc = /line (\d+) column (\d+)/.exec(msg);
    var pos = /position (\d+)/.exec(msg);
    if (lc) {
      line = +lc[1];
      col = +lc[2];
    } else if (pos) {
      var before = text.slice(0, +pos[1]);
      line = before.split("\n").length;
      col = +pos[1] - before.lastIndexOf("\n");
    }

    var hint;
    if (/end of JSON|Unexpected end/i.test(msg)) {
      hint = "The JSON ends too early. Check for a missing closing bracket ] or brace }.";
    } else if (/double-quoted|property name|Expected.*name/i.test(msg)) {
      hint = "Keys and text need double quotes, and there must be no comma after the last item.";
    } else if (/Expected ','|Expected '\]'|Expected '\}'|after (property value|array element)/i.test(msg)) {
      hint = "Looks like a missing comma or closing bracket near here.";
    } else if (/Unexpected token|Unexpected character|Unexpected non-whitespace/i.test(msg)) {
      hint = "Unexpected character. Check for a stray symbol, a single quote ' or a trailing comma.";
    } else {
      hint = msg;
    }
    return (line ? "Line " + line + ", column " + col + ": " : "") + hint;
  }

  // Checks the shape the server expects, with one clear message per problem.
  function checkShape(data) {
    var errs = [];
    function isObj(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
    function isInt(v) { return typeof v === "number" && isFinite(v) && Math.floor(v) === v; }

    if (!isObj(data)) return ['The planogram must be an object like { "rows": [ ... ] }.'];
    if (!Array.isArray(data.rows) || data.rows.length === 0) {
      return ['"rows" must be a list with at least one row.'];
    }
    data.rows.forEach(function (row, i) {
      var p = "rows[" + i + "]";
      if (!isObj(row)) { errs.push(p + " must be an object."); return; }
      if (!isInt(row.row) || row.row < 1) errs.push(p + ".row must be a whole number, 1 or more (1 is the top shelf row).");
      if (!Array.isArray(row.expected) || row.expected.length === 0) {
        errs.push(p + ".expected must be a list with at least one product.");
        return;
      }
      row.expected.forEach(function (e, j) {
        var q = p + ".expected[" + j + "]";
        if (!isObj(e)) { errs.push(q + ' must be an object with "product" and "facings".'); return; }
        if (typeof e.product !== "string" || !e.product.trim()) errs.push(q + ".product must be the product name, as text.");
        if (!isInt(e.facings) || e.facings < 1) errs.push(q + ".facings must be a whole number, 1 or more.");
        if (e.category !== undefined && typeof e.category !== "string") errs.push(q + ".category must be text.");
        if (e.aliases !== undefined &&
            (!Array.isArray(e.aliases) || e.aliases.some(function (a) { return typeof a !== "string"; }))) {
          errs.push(q + ".aliases must be a list of names, as text.");
        }
      });
    });
    return errs;
  }

  function addError(message) {
    var li = document.createElement("li");
    li.textContent = message;
    planoErrors.appendChild(li);
  }

  function validatePlanogram() {
    var text = planogram.value;
    planoErrors.innerHTML = "";
    planoStatus.className = "editor-status";

    if (!text.trim()) {
      planoOk = false;
      planoStatus.textContent = "Paste a planogram, or press Load sample.";
      return;
    }

    var data;
    try {
      data = JSON.parse(text);
    } catch (err) {
      planoOk = false;
      planoStatus.textContent = "Not valid JSON";
      planoStatus.classList.add("bad");
      addError(explainJsonError(text, err));
      return;
    }

    var problems = checkShape(data);
    if (problems.length) {
      planoOk = false;
      planoStatus.textContent = problems.length + (problems.length === 1 ? " problem" : " problems") + " found";
      planoStatus.classList.add("bad");
      problems.slice(0, 6).forEach(addError);
      if (problems.length > 6) addError("...and " + (problems.length - 6) + " more.");
      return;
    }

    var facings = 0;
    data.rows.forEach(function (r) { r.expected.forEach(function (e) { facings += e.facings; }); });
    planoOk = true;
    planoStatus.textContent = "Valid planogram \u00b7 " + data.rows.length +
      (data.rows.length === 1 ? " row" : " rows") + " \u00b7 " + facings + " facings";
    planoStatus.classList.add("good");
  }

  function onPlanogramChange() {
    syncEditor();
    validatePlanogram();
    updateMismatch();
    updateCta();
  }

  planogram.addEventListener("input", onPlanogramChange);
  planogram.addEventListener("scroll", function () {
    hlPre.scrollTop = planogram.scrollTop;
    hlPre.scrollLeft = planogram.scrollLeft;
  });

  $("sample-btn").addEventListener("click", function () {
    planogram.value = sampleText(categorySel.value);
    lastSample = planogram.value;
    onPlanogramChange();
  });

  $("format-btn").addEventListener("click", function () {
    try {
      planogram.value = JSON.stringify(JSON.parse(planogram.value), null, 2);
      onPlanogramChange();
    } catch (err) {
      validatePlanogram(); // shows exactly what is wrong
    }
  });

  $("clear-btn").addEventListener("click", function () {
    planogram.value = "";
    onPlanogramChange();
    planogram.focus();
  });

  /* ------------------------------------------------------- Audit button */

  function updateCta() {
    var ready = fileOk && planoOk;
    submitBtn.disabled = !ready;
    if (ready) {
      ctaHint.textContent = "Ready. The audit runs in the background and takes about 10 to 30 seconds.";
    } else if (!fileOk && !planoOk) {
      ctaHint.textContent = "Add a shelf photo and a valid planogram to continue.";
    } else if (!fileOk) {
      ctaHint.textContent = "Add a shelf photo to continue.";
    } else {
      ctaHint.textContent = "Fix the planogram to continue.";
    }
  }

  form.addEventListener("submit", function (e) {
    if (!(fileOk && planoOk)) {
      e.preventDefault();
      updateCta();
      return;
    }
    stopStream();
    submitBtn.disabled = true;
    submitBtn.textContent = "Uploading photo...";
  });

  // Coming back with the Back button: the form must be usable again.
  window.addEventListener("pageshow", function (e) {
    if (e.persisted) {
      submitBtn.textContent = "Audit Shelf";
      updateCta();
    }
  });

  /* --------------------------------------------------------------- start */

  categoryHint.textContent = categorySel.options[categorySel.selectedIndex].getAttribute("data-hint") || "";
  syncEditor();
  validatePlanogram();
  updateMismatch();
  if (fileInput.files && fileInput.files.length) setFile(fileInput.files[0]);
  else if (reuseField && reuseField.value) showReusedPhoto(reuseField.value);
  updateCta();
})();
