/* ShelfSense audit page: live status, saved to-do list, box filters, and manual corrections. */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var detail = data && data.detail;
          if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg; }).join("; ");
          throw new Error(detail || "Request failed (" + response.status + ")");
        }
        return data;
      });
    });
  }

  /* ---- Processing: follow the real stage of the background job ---- */
  var processing = $("processing");
  if (processing) {
    var auditId = processing.getAttribute("data-audit-id");
    var stepEls = Array.prototype.slice.call($("progress-steps").children);
    var STAGE_INDEX = { queued: 0, detecting: 1, verifying: 2, scoring: 3, done: 4 };

    var poll = function () {
      fetch("/api/audits/" + encodeURIComponent(auditId), { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (audit) {
          if (audit.status !== "processing") { window.location.reload(); return; }
          var index = STAGE_INDEX[audit.stage] || 0;
          stepEls.forEach(function (el, i) {
            el.className = i + 1 <= index ? "done" : (i === index ? "active" : "");
          });
          setTimeout(poll, 1500);
        })
        .catch(function () { setTimeout(poll, 3000); });
    };
    setTimeout(poll, 1000);
  }

  /* ---- "The shelf really is empty": record the 0% the AI reported ---- */
  var emptyBtn = $("confirm-empty-btn");
  if (emptyBtn) {
    emptyBtn.addEventListener("click", function () {
      emptyBtn.disabled = true;
      emptyBtn.textContent = "Recording...";
      post("/api/audits/" + encodeURIComponent(emptyBtn.getAttribute("data-audit-id")) + "/corrections",
           { items: [], confirmed_empty: true })
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          emptyBtn.disabled = false;
          emptyBtn.textContent = "Yes, the shelf is empty (record 0%)";
          window.alert("Could not record that: " + err.message);
        });
    });
  }

  /* ---- Confirm this result ---- */
  var confirmBtn = $("confirm-btn");
  if (confirmBtn) {
    confirmBtn.addEventListener("click", function () {
      var want = confirmBtn.getAttribute("data-confirmed") !== "true";
      confirmBtn.disabled = true;
      post("/api/audits/" + encodeURIComponent(confirmBtn.getAttribute("data-audit-id")) + "/confirm", { confirmed: want })
        .then(function (data) {
          var on = !!data.confirmed_at;
          confirmBtn.setAttribute("data-confirmed", on ? "true" : "false");
          confirmBtn.textContent = on ? "Undo confirmation" : "Confirm this result";
          confirmBtn.classList.toggle("ghost", on);
          $("confirmed-chip").hidden = !on;
          var note = $("confirmed-note");
          note.hidden = !on;
          if (on) note.textContent = "Confirmed just now.";
        })
        .catch(function (err) {
          window.alert("Could not save the confirmation: " + err.message);
        })
        .then(function () { confirmBtn.disabled = false; });
    });
  }

  /* ---- To-do list: ticks are saved in the database ---- */
  var todo = $("todo");
  var list = $("tasks");
  if (todo && list) {
    var auditIdTodo = todo.getAttribute("data-audit-id");
    var countEl = $("todo-count");
    var barEl = $("todo-bar");
    var doneEl = $("todo-done");
    var msgEl = $("todo-msg");

    var render = function (done, total) {
      countEl.textContent = done + " of " + total + " done";
      barEl.style.width = (total ? (done / total) * 100 : 0) + "%";
      doneEl.hidden = done !== total;
    };

    list.addEventListener("change", function (e) {
      var box = e.target;
      if (!box || box.type !== "checkbox") return;
      var li = box.closest(".task");
      var wanted = box.checked;
      li.classList.toggle("done", wanted);
      msgEl.textContent = "";
      post("/api/audits/" + encodeURIComponent(auditIdTodo) + "/tasks/" + li.getAttribute("data-task-id"), { done: wanted })
        .then(function (progress) { render(progress.done, progress.total); })
        .catch(function (err) {
          // The save failed: put the box back so the page never shows progress that was not saved.
          box.checked = !wanted;
          li.classList.toggle("done", !wanted);
          msgEl.textContent = "Could not save that change (" + err.message + "). Try reloading the page.";
        });
    });
  }

  /* ---- Shelf photo: show or hide each type of box ---- */
  var view = $("shelf-view");
  var legend = $("legend");
  if (view && legend) {
    legend.addEventListener("click", function (e) {
      var btn = e.target.closest(".legend-btn");
      if (!btn) return;
      var on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      view.classList.toggle("hide-" + btn.getAttribute("data-status"), !on);
    });
  }

  /* ---- Corrections ---- */
  var review = $("review");
  if (review) {
    var body = $("items-body");
    var template = $("item-template");
    var reviewMsg = $("review-msg");
    var saveBtn = $("save-corrections");
    var auditIdReview = review.getAttribute("data-audit-id");

    $("add-item").addEventListener("click", function () {
      var row = template.content.firstElementChild.cloneNode(true);
      body.appendChild(row);
      row.querySelector(".i-name").focus();
    });

    body.addEventListener("click", function (e) {
      var remove = e.target.closest(".i-remove");
      if (!remove) return;
      var tr = remove.closest("tr");
      var data = {};
      try { data = JSON.parse(tr.getAttribute("data-item") || "{}"); } catch (err) { data = {}; }
      if (data["new"]) {
        tr.remove(); // a row you added yourself can simply be deleted
      } else {
        // An AI detection is not deleted, only ignored, so the choice can be undone.
        var ignored = tr.classList.toggle("dropped");
        remove.textContent = ignored ? "Undo" : "Ignore";
      }
    });

    var acceptAll = $("accept-uncertain");
    if (acceptAll) {
      acceptAll.addEventListener("click", function () {
        Array.prototype.forEach.call(body.querySelectorAll("tr.uncertain:not(.dropped) .i-count"), function (box) {
          box.checked = true;
        });
        reviewMsg.textContent = "Uncertain items ticked. Check them, then press Save corrections and recompute.";
        reviewMsg.className = "review-msg";
      });
    }

    var collect = function () {
      var items = [];
      var problem = null;
      Array.prototype.forEach.call(body.querySelectorAll("tr"), function (tr) {
        var data = {};
        try { data = JSON.parse(tr.getAttribute("data-item") || "{}"); } catch (e) { data = {}; }

        var name = tr.querySelector(".i-name").value.trim();
        var qty = parseInt(tr.querySelector(".i-qty").value, 10);
        var row = parseInt(tr.querySelector(".i-row").value, 10);
        var assigned = tr.querySelector(".i-assign").value || null;
        var counted = tr.querySelector(".i-count").checked;

        tr.classList.remove("invalid");
        if (!name || isNaN(qty) || qty < 0 || isNaN(row) || row < 1) {
          tr.classList.add("invalid");
          problem = "Every item needs a name, a row of 1 or more, and a quantity of 0 or more.";
          return;
        }

        var uncertain = !!data.uncertain;
        var state = uncertain ? (counted ? "accept" : "keep") : (counted ? "keep" : "drop");
        if (tr.classList.contains("dropped")) state = "drop"; // "Ignore" always removes it from the count
        var edited = !!data["new"] || data.source === "user" ||
          name !== data.name || qty !== data.quantity || row !== data.row ||
          (assigned || null) !== (data.assigned_to || null);

        items.push({
          row: row,
          name: name,
          quantity: qty,
          state: state,
          edited: edited,
          assigned_to: assigned,
          category: data.category || "",
          confidence: data.confidence === undefined ? null : data.confidence,
          count_confidence: data.count_confidence === undefined ? null : data.count_confidence,
          verified: !!data.verified,
          position: data.position || "",
          attributes: data.attributes || [],
          matched_expected: data.matched_expected || null,
          box: data.box || null,
          source: data.source || "model",
        });
      });
      return { items: items, problem: problem };
    };

    saveBtn.addEventListener("click", function () {
      var collected = collect();
      reviewMsg.className = "review-msg";
      if (collected.problem) {
        reviewMsg.textContent = collected.problem;
        reviewMsg.classList.add("error");
        return;
      }
      saveBtn.disabled = true;
      reviewMsg.textContent = "Recomputing...";
      post("/api/audits/" + encodeURIComponent(auditIdReview) + "/corrections", {
        items: collected.items,
        confirmed_empty: $("confirm-empty").checked,
      })
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          saveBtn.disabled = false;
          reviewMsg.textContent = "Could not save the corrections: " + err.message;
          reviewMsg.classList.add("error");
        });
    });
  }
})();
