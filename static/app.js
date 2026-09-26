/* G-Status — student page behaviour: live refresh + history drawer */
(function () {
  "use strict";

  var cfg = window.G_STATUS || {};
  var refreshMs = Math.max(15, Math.min(30, cfg.refreshSeconds || 20)) * 1000;

  var dataEl = document.getElementById("studentData");
  var student = {};
  try {
    student = JSON.parse(dataEl.textContent);
  } catch (err) {
    student = { history: [] };
  }

  var els = {
    drawer: document.getElementById("drawer"),
    overlay: document.getElementById("drawerOverlay"),
    openBtn: document.getElementById("historyBtn"),
    closeBtn: document.getElementById("drawerClose"),
    list: document.getElementById("historyList"),
    filterActivity: document.getElementById("filterActivity"),
    filterRange: document.getElementById("filterRange"),
    filterSearch: document.getElementById("filterSearch"),
    sumCount: document.getElementById("sumCount"),
    sumEarned: document.getElementById("sumEarned"),
    sumDeducted: document.getElementById("sumDeducted"),
    sumTotal: document.getElementById("sumTotal"),
    footTotal: document.getElementById("footTotal"),
    statusLabel: document.getElementById("statusLabel"),
    studentName: document.getElementById("studentName"),
    drawerName: document.getElementById("drawerName"),
    pointsValue: document.getElementById("pointsValue"),
    fieldGrid: document.getElementById("fieldGrid"),
    livePill: document.getElementById("livePill"),
    updatedAt: document.getElementById("updatedAt"),
    refreshBtn: document.getElementById("refreshBtn"),
    toast: document.getElementById("toast")
  };

  /* ---------------- helpers ---------------- */
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function startOf(range) {
    var now = new Date();
    var d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    if (range === "today") return d;
    if (range === "week") {
      var day = (d.getDay() + 6) % 7; // Monday start
      return new Date(d.getFullYear(), d.getMonth(), d.getDate() - day);
    }
    if (range === "month") return new Date(d.getFullYear(), d.getMonth(), 1);
    return null;
  }

  /* ---------------- rendering ---------------- */
  function populateActivityFilter() {
    if (!els.filterActivity) return;
    var current = els.filterActivity.value;
    var names = [];
    (student.history || []).forEach(function (item) {
      var name = (item.activity || "").trim();
      if (name && names.indexOf(name) === -1) names.push(name);
    });
    names.sort();
    els.filterActivity.innerHTML =
      '<option value="">All Activities</option>' +
      names
        .map(function (n) {
          return '<option value="' + escapeHtml(n) + '">' + escapeHtml(n) + "</option>";
        })
        .join("");
    if (names.indexOf(current) !== -1) els.filterActivity.value = current;
  }

  function visibleHistory() {
    var items = student.history || [];
    var activity = els.filterActivity ? els.filterActivity.value : "";
    var range = els.filterRange ? els.filterRange.value : "all";
    var term = (els.filterSearch ? els.filterSearch.value : "").trim().toLowerCase();
    var from = startOf(range);

    return items.filter(function (item) {
      if (activity && (item.activity || "").trim() !== activity) return false;
      if (from) {
        if (!item.timestamp_iso) return false;
        var dt = new Date(item.timestamp_iso);
        if (isNaN(dt.getTime()) || dt < from) return false;
      }
      if (term) {
        var haystack = [item.activity, item.description, item.timestamp_label, item.points_label]
          .join(" ")
          .toLowerCase();
        if (haystack.indexOf(term) === -1) return false;
      }
      return true;
    });
  }

  function renderHistory() {
    if (!els.list) return;
    var items = visibleHistory();

    if (!items.length) {
      els.list.innerHTML =
        '<p class="empty-history">No activity records match these filters yet.</p>';
      return;
    }

    els.list.innerHTML = items
      .map(function (item) {
        var pts = Number(item.points) || 0;
        var cls = !item.has_points ? "zero" : pts > 0 ? "plus" : pts < 0 ? "minus" : "zero";
        var badge = item.has_points
          ? '<span class="pts ' + cls + '">' + escapeHtml(item.points_label) + " ⭐</span>"
          : "";
        return (
          '<article class="history-item' + (pts < 0 ? " negative" : "") + '">' +
          '<span class="icon">' + escapeHtml(item.icon || "⭐") + "</span>" +
          "<div>" +
          '<div class="title">' + escapeHtml(item.activity) + "</div>" +
          '<div class="meta">' + escapeHtml(item.timestamp_label || "Date not recorded") + "</div>" +
          (item.description ? '<div class="note">' + escapeHtml(item.description) + "</div>" : "") +
          "</div>" +
          badge +
          "</article>"
        );
      })
      .join("");
  }

  function renderSummary() {
    if (els.sumCount) els.sumCount.textContent = student.activity_count;
    if (els.sumEarned) els.sumEarned.textContent = student.points_earned;
    if (els.sumDeducted) els.sumDeducted.textContent = student.points_deducted;
    if (els.sumTotal) els.sumTotal.textContent = "⭐ " + student.total_points;
    if (els.footTotal) els.footTotal.textContent = "⭐ " + student.total_points;
  }

  function renderProfile() {
    if (els.studentName) els.studentName.textContent = student.name || "Student";
    if (els.drawerName) els.drawerName.textContent = student.name || "Student";
    if (els.pointsValue) els.pointsValue.textContent = "⭐ " + student.total_points;
    if (els.statusLabel) {
      els.statusLabel.textContent = (student.status || "STATUS UNKNOWN").toUpperCase();
      els.statusLabel.className = "status-label status-" + (student.status_key || "unknown");
    }
    var ring = document.querySelector(".status-ring");
    if (ring) ring.className = "status-ring status-" + (student.status_key || "unknown");

    if (els.fieldGrid && student.fields) {
      els.fieldGrid.innerHTML = student.fields
        .map(function (field) {
          return (
            '<div class="field"><dt>' +
            escapeHtml(field.label) +
            "</dt><dd>" +
            escapeHtml(field.value) +
            "</dd></div>"
          );
        })
        .join("");
    }
  }

  function renderAll() {
    renderProfile();
    renderSummary();
    populateActivityFilter();
    renderHistory();
  }

  /* ---------------- drawer ---------------- */
  var lastFocused = null;

  function openDrawer() {
    lastFocused = document.activeElement;
    els.overlay.hidden = false;
    requestAnimationFrame(function () {
      els.overlay.classList.add("show");
      els.drawer.classList.add("open");
    });
    els.drawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    if (els.closeBtn) els.closeBtn.focus();
  }

  function closeDrawer() {
    els.drawer.classList.remove("open");
    els.overlay.classList.remove("show");
    els.drawer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
    setTimeout(function () {
      els.overlay.hidden = true;
    }, 320);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  if (els.openBtn) els.openBtn.addEventListener("click", openDrawer);
  if (els.closeBtn) els.closeBtn.addEventListener("click", closeDrawer);
  if (els.overlay) els.overlay.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && els.drawer.classList.contains("open")) closeDrawer();
  });

  [els.filterActivity, els.filterRange].forEach(function (el) {
    if (el) el.addEventListener("change", renderHistory);
  });
  if (els.filterSearch) els.filterSearch.addEventListener("input", renderHistory);

  /* ---------------- live refresh ---------------- */
  function showToast() {
    if (!els.toast) return;
    els.toast.hidden = false;
    els.toast.classList.add("show");
    setTimeout(function () {
      els.toast.classList.remove("show");
      setTimeout(function () {
        els.toast.hidden = true;
      }, 350);
    }, 3200);
  }

  function markUpdated(ok) {
    if (!els.livePill || !els.updatedAt) return;
    var now = new Date();
    var stamp = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
    if (ok) {
      els.livePill.classList.remove("stale");
      els.livePill.innerHTML = '<span class="dot"></span> LIVE';
      els.updatedAt.textContent = "Last updated: " + stamp;
    } else {
      els.livePill.classList.add("stale");
      els.livePill.innerHTML = '<span class="dot"></span> RETRYING';
      els.updatedAt.textContent = "Showing last known data";
    }
  }

  var fetching = false;

  function refresh(force) {
    if (fetching) return;
    fetching = true;
    if (force && els.refreshBtn) els.refreshBtn.disabled = true;

    fetch("/api/student/" + encodeURIComponent(cfg.memberId) + (force ? "?force=1" : ""), {
      headers: { Accept: "application/json" },
      cache: "no-store"
    })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok || !result.body.ok) {
          markUpdated(false);
          return;
        }
        var next = result.body.student;
        var previousCount = (student.history || []).length;
        student = next;
        renderAll();
        markUpdated(true);
        if ((student.history || []).length > previousCount) showToast();
      })
      .catch(function () {
        markUpdated(false);
      })
      .finally(function () {
        fetching = false;
        if (els.refreshBtn) els.refreshBtn.disabled = false;
      });
  }

  if (els.refreshBtn) {
    els.refreshBtn.addEventListener("click", function () {
      refresh(true);
    });
  }

  setInterval(function () {
    if (!document.hidden) refresh(false);
  }, refreshMs);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh(false);
  });

  renderAll();
  markUpdated(true);
})();
