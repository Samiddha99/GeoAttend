/* ==========================================================================
   GeoAttend — front-end core (jQuery)
   Everything talks to the server over AJAX; full page loads are avoided.
   ========================================================================== */
window.GA = (function ($) {
  "use strict";

  /* ---------------------------------------------------------------- CSRF */
  function cookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }
  const csrf = () => cookie("csrftoken") || $('input[name=csrfmiddlewaretoken]').val() || "";

  $.ajaxSetup({
    beforeSend: function (xhr, settings) {
      if (!/^(GET|HEAD|OPTIONS|TRACE)$/.test(settings.type) && !this.crossDomain) {
        xhr.setRequestHeader("X-CSRFToken", csrf());
      }
      xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    }
  });

  /* --------------------------------------------------------------- Toast */
  function toast(message, type, timeout) {
    if (!$("#ga-toasts").length) $("body").append('<div id="ga-toasts"></div>');
    type = type || "info";
    const icons = {
      ok: "fa-circle-check", err: "fa-circle-exclamation",
      warn: "fa-triangle-exclamation", info: "fa-circle-info"
    };
    const $t = $(
      '<div class="ga-toast ' + type + '">' +
        '<i class="fa-solid ' + (icons[type] || icons.info) + '"></i>' +
        '<div class="flex-grow-1">' + message + "</div>" +
        '<i class="fa-solid fa-xmark text-muted-2 ms-1" style="cursor:pointer"></i>' +
      "</div>"
    );
    $t.find(".fa-xmark").on("click", () => $t.remove());
    $("#ga-toasts").append($t);
    setTimeout(function () {
      $t.fadeOut(200, () => $t.remove());
    }, timeout || (type === "err" ? 7000 : 4000));
    return $t;
  }

  /* -------------------------------------------------------------- Loader */
  let busy = 0;
  function overlay(on) {
    if (!$("#ga-overlay").length) {
      $("body").append('<div class="ga-overlay" id="ga-overlay"><div class="ga-spinner"></div></div>');
    }
    busy += on ? 1 : -1;
    if (busy < 0) busy = 0;
    $("#ga-overlay").toggleClass("show", busy > 0);
  }

  function btnBusy($btn, on, busyText) {
    if (!$btn || !$btn.length) return;
    if (on) {
      $btn.data("ga-html", $btn.html()).prop("disabled", true)
        .html('<span class="spinner-border spinner-border-sm me-2"></span>' + (busyText || "Working…"));
    } else {
      $btn.prop("disabled", false).html($btn.data("ga-html") || $btn.html());
    }
  }

  /**
   * Spin a button's icon until the work finishes.
   *
   * btnBusy() swaps the label for "Working…", which is wrong for the icon-only
   * refresh buttons — it blows out their width. This spins the icon in place
   * and disables the button instead.
   *
   * Pass the promise the click kicked off. For a screen that fires several
   * requests, hand over $.when(a, b, c) so the button keeps spinning until the
   * last one lands rather than the first:
   *
   *   $("#btn-refresh").on("click", function(){ GA.spin(this, load()); });
   */
  const SPIN_MIN_MS = 350;

  function spin(btn, promise) {
    const $btn = $(btn);
    if (!$btn.length) return promise;
    const $icon = $btn.find("i").first();
    // Some icons already spin as decoration — don't strip that on the way out.
    const wasSpinning = $icon.hasClass("fa-spin");
    const startedAt = Date.now();

    $btn.prop("disabled", true);
    if (!wasSpinning) $icon.addClass("fa-spin");

    const stop = function () {
      // A cached response can return in 20 ms; stopping instantly reads as a
      // glitch rather than a refresh, so hold the spin briefly.
      const wait = Math.max(0, SPIN_MIN_MS - (Date.now() - startedAt));
      setTimeout(function () {
        $btn.prop("disabled", false);
        if (!wasSpinning) $icon.removeClass("fa-spin");
      }, wait);
    };

    if (promise && typeof promise.always === "function") promise.always(stop);
    else stop();                       // nothing to wait on — just blink
    return promise;
  }

  /* ----------------------------------------------------------------- AJAX */
  function request(opts) {
    const o = $.extend({ method: "GET", data: {}, quiet: false, loader: false }, opts);
    if (o.loader) overlay(true);
    const settings = {
      url: o.url,
      type: o.method,
      dataType: "json",
      data: o.data
    };
    if (o.data instanceof FormData) {
      settings.processData = false;
      settings.contentType = false;
    }
    return $.ajax(settings)
      .then(function (res) {
        if (res && res.success === false) return $.Deferred().reject({ responseJSON: res }).promise();
        return res;
      })
      .fail(function (xhr) {
        const res = xhr.responseJSON || {};
        if (xhr.status === 401 || res.login_required) {
          toast("Your session has expired. Redirecting to sign-in…", "warn");
          setTimeout(() => (window.location = "/auth/login/?next=" + encodeURIComponent(location.pathname)), 1200);
          return;
        }
        if (!o.quiet) toast(res.message || "Network error. Please try again.", "err");
        // Nothing is going to render now, so any skeleton left on the page
        // would shimmer forever and read as "still loading". Clear them and
        // say so instead. Requests still in flight re-render regardless.
        clearStuckLoaders(res.message);
      })
      .always(function () {
        if (o.loader) overlay(false);
      });
  }

  const get = (url, data, opts) => request($.extend({ url: url, method: "GET", data: data || {} }, opts || {}));
  const post = (url, data, opts) => request($.extend({ url: url, method: "POST", data: data || {} }, opts || {}));

  /* ---------------------------------------------------------- Form utils */
  function clearErrors($form) {
    $form.find(".is-invalid").removeClass("is-invalid");
    $form.find(".invalid-feedback.ga-dyn").remove();
  }

  function showErrors($form, errors, message) {
    clearErrors($form);
    let first = null;
    $.each(errors || {}, function (field, msg) {
      const $input = $form.find('[name="' + field + '"]');
      if ($input.length) {
        $input.addClass("is-invalid");
        $input.closest(".mb-3, .col, .form-group, .mb-2").append(
          '<div class="invalid-feedback ga-dyn d-block">' + msg + "</div>"
        );
        if (!first) first = $input;
      } else if (!message) {
        toast(msg, "err");
      }
    });
    if (first) first.trigger("focus");
    if (message) toast(message, "err");
  }

  /** Submit any <form> over AJAX. */
  function submit($form, options) {
    const o = $.extend({ url: null, onSuccess: null, onError: null,
                         busyText: "Please wait…", loader: false }, options || {});
    const $btn = o.button || $form.find('[type="submit"]').first();
    clearErrors($form);
    btnBusy($btn, true, o.busyText);
    const url = o.url || $form.attr("action") || location.href;
    const data = o.formData ? new FormData($form[0]) : $form.serialize();
    return post(url, data, { quiet: true, loader: o.loader })
      .done(function (res) {
        if (res.message) toast(res.message, "ok");
        if (o.onSuccess) o.onSuccess(res);
        else if (res.data && res.data.redirect) window.location = res.data.redirect;
      })
      .fail(function (xhr) {
        const res = (xhr && xhr.responseJSON) || {};
        showErrors($form, res.errors, res.message || "Please check the form and try again.");
        if (o.onError) o.onError(res, xhr);
      })
      .always(() => btnBusy($btn, false));
  }

  /* ------------------------------------------------------------- Confirm */
  function confirm(opts) {
    const o = $.extend({
      title: "Are you sure?", body: "", confirmText: "Yes, continue",
      cancelText: "Cancel", variant: "danger", icon: "fa-triangle-exclamation"
    }, opts || {});
    const d = $.Deferred();
    const id = "ga-confirm-" + Date.now();
    const html =
      '<div class="modal fade" id="' + id + '" tabindex="-1"><div class="modal-dialog modal-dialog-centered modal-sm-plus">' +
      '<div class="modal-content"><div class="modal-body text-center p-4">' +
      '<div class="mb-3"><i class="fa-solid ' + o.icon + ' fa-2x text-' + o.variant + '"></i></div>' +
      '<h5 class="fw-bold mb-2">' + o.title + "</h5>" +
      '<p class="text-muted-2 mb-4">' + o.body + "</p>" +
      '<div class="d-flex gap-2 justify-content-center">' +
      '<button class="btn btn-light px-4" data-bs-dismiss="modal">' + o.cancelText + "</button>" +
      '<button class="btn btn-' + o.variant + ' px-4 ga-yes">' + o.confirmText + "</button>" +
      "</div></div></div></div></div>";
    const $m = $(html).appendTo("body");
    const modal = new bootstrap.Modal($m[0]);
    $m.find(".ga-yes").on("click", function () { d.resolve(); modal.hide(); });
    $m.on("hidden.bs.modal", function () { $m.remove(); if (d.state() === "pending") d.reject(); });
    modal.show();
    return d.promise();
  }

  /* -------------------------------------------------------------- Format */
  const esc = (s) => $("<div>").text(s === null || s === undefined ? "" : s).html();

  function pctPill(value) {
    const v = Number(value || 0);
    const cls = v >= 85 ? "pill-green" : v >= 75 ? "pill-blue" : v >= 60 ? "pill-amber" : "pill-red";
    return '<span class="ga-pill ' + cls + '">' + v.toFixed(1) + "%</span>";
  }

  function bar(value) {
    const v = Math.max(0, Math.min(100, Number(value || 0)));
    const c = v >= 85 ? "#10b981" : v >= 75 ? "#3b82f6" : v >= 60 ? "#f59e0b" : "#ef4444";
    return '<div class="d-flex align-items-center gap-2">' +
      '<div class="ga-bar flex-grow-1"><span style="width:' + v + "%;background:" + c + '"></span></div>' +
      '<small class="fw-600" style="min-width:44px">' + v.toFixed(1) + "%</small></div>";
  }

  function statusPill(status) {
    const map = {
      OPEN: ["pill-green", "fa-circle-dot", "Open"],
      CLOSED: ["pill-grey", "fa-circle-check", "Closed"],
      CANCELLED: ["pill-red", "fa-ban", "Cancelled"],
      active: ["pill-green", "fa-circle-check", "Active"],
      invited: ["pill-amber", "fa-envelope", "Invited"],
      vacant: ["pill-grey", "fa-user-slash", "Vacant"],
      PENDING: ["pill-amber", "fa-hourglass-half", "Pending"],
      ACCEPTED: ["pill-green", "fa-circle-check", "Accepted"],
      REVOKED: ["pill-red", "fa-ban", "Revoked"],
      EXPIRED: ["pill-grey", "fa-clock", "Expired"],
      PRESENT: ["pill-green", "fa-check", "Present"],
      MANUAL: ["pill-violet", "fa-user-pen", "Manual"],
      ABSENT: ["pill-red", "fa-xmark", "Absent"],
      created: ["pill-green", "fa-plus", "Created"],
      updated: ["pill-blue", "fa-rotate", "Updated"],
      error: ["pill-red", "fa-xmark", "Error"]
    };
    const m = map[status] || ["pill-grey", "fa-circle", status];
    return '<span class="ga-pill ' + m[0] + '"><i class="fa-solid ' + m[1] + '"></i>' + m[2] + "</span>";
  }

  function avatar(name, cls) {
    const parts = String(name || "?").replace(/\./g, " ").split(" ").filter(Boolean);
    const ini = (parts.slice(0, 2).map((p) => p[0]).join("") || "U").toUpperCase();
    return '<div class="ga-avatar ' + (cls || "sm") + '">' + ini + "</div>";
  }

  /**
   * A phone number with click-to-call and click-to-WhatsApp buttons.
   * `dial` comes from the server: {raw, tel, wa, error}.
   */
  function phone(dial, opts) {
    const o = $.extend({ missing: "—", icon: "" }, opts || {});
    if (!dial || !dial.raw) {
      return '<span class="text-muted-2">' + o.missing + "</span>";
    }
    const label = (o.icon ? '<i class="' + o.icon + ' me-1"></i>' : "") + esc(dial.tel || dial.raw);
    if (!dial.tel) {
      return '<span class="ga-pill pill-amber" title="' + esc(dial.error) +
        '"><i class="fa-solid fa-triangle-exclamation"></i>' + esc(dial.raw) + "</span>";
    }
    return '<div class="ga-phone">' +
      '<span class="num">' + label + "</span>" +
      '<a class="ga-dial" href="tel:' + esc(dial.tel) + '" title="Call ' + esc(dial.tel) + '">' +
      '<i class="fa-solid fa-phone"></i></a>' +
      '<a class="ga-dial wa" href="https://wa.me/' + esc(dial.wa) +
      '" target="_blank" rel="noopener" title="WhatsApp ' + esc(dial.tel) + '">' +
      '<i class="fa-brands fa-whatsapp"></i></a></div>';
  }

  function mmss(sec) {
    sec = Math.max(0, Math.floor(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  /* ----------------------------------------------- Interactive DataTable */
  /**
   * GA.table("#el", {
   *   columns:[{key,label,render,sortable,className,width}],
   *   rows:[], perPage:15, search:"#input", empty:"..."
   * })
   */

  /* ----------------------------------------------------------------- CSV */
  /**
   * A readable string for one CSV value, whatever shape it arrives in.
   *
   * The naive String(value) turns an object into "[object Object]", and an
   * array of objects into a row of them — which is exactly what a column like
   * "Subjects · batches" holds. Pick the most label-like field instead, and
   * fall back to JSON so a value is never silently lost.
   */
  const CSV_LABEL_KEYS = ["label", "name", "code", "subject", "title", "text", "value"];

  function csvText(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.map(csvText).filter(Boolean).join(", ");
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value !== "object") return String(value);

    for (const key of CSV_LABEL_KEYS) {
      if (value[key] !== undefined && value[key] !== null && typeof value[key] !== "object") {
        return String(value[key]);
      }
    }
    try { return JSON.stringify(value); } catch (e) { return ""; }
  }

  function csvCell(value) {
    if (value === null || value === undefined) return "";
    const text = String(value);
    // Quote when the value could otherwise break the row or the column split.
    return /[",\n\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  /**
   * Turn rows into a CSV file and hand it to the browser.
   *
   * The BOM is what makes Excel open UTF-8 correctly — without it, accented
   * names and the "—" placeholders arrive as mojibake.
   */
  function downloadCsv(filename, header, body) {
    const lines = [header.map(csvCell).join(",")]
      .concat(body.map(row => row.map(csvCell).join(",")));
    const blob = new Blob(["\ufeff" + lines.join("\r\n")],
                          { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function table(selector, config) {
    const $host = $(selector);
    $host.data("ga-table", true);          // lets GA.loading() pick the right skeleton
    const state = {
      rows: config.rows || [],
      view: [],
      sortKey: config.sortKey || null,
      sortDir: config.sortDir || "asc",
      page: 1,
      perPage: config.perPage || 15,
      query: ""
    };
    const cols = config.columns || [];

    function valueOf(row, col) {
      return col.value ? col.value(row) : row[col.key];
    }

    function apply() {
      let rows = state.rows.slice();
      if (state.query) {
        const q = state.query.toLowerCase();
        rows = rows.filter(function (r) {
          return cols.some(function (c) {
            const v = valueOf(r, c);
            return v !== null && v !== undefined && String(v).toLowerCase().indexOf(q) > -1;
          });
        });
      }
      if (config.filter) rows = rows.filter(config.filter);
      if (state.sortKey) {
        const col = cols.find((c) => c.key === state.sortKey) || { key: state.sortKey };
        const dir = state.sortDir === "asc" ? 1 : -1;
        rows.sort(function (a, b) {
          let x = valueOf(a, col), y = valueOf(b, col);
          if (typeof x === "string" || typeof y === "string") {
            x = String(x === undefined || x === null ? "" : x).toLowerCase();
            y = String(y === undefined || y === null ? "" : y).toLowerCase();
            return x < y ? -dir : x > y ? dir : 0;
          }
          return ((x || 0) - (y || 0)) * dir;
        });
      }
      state.view = rows;
      const pages = Math.max(1, Math.ceil(rows.length / state.perPage));
      if (state.page > pages) state.page = pages;
      render();
    }

    function render() {
      const start = (state.page - 1) * state.perPage;
      const slice = state.view.slice(start, start + state.perPage);
      let head = "<thead><tr>";
      cols.forEach(function (c) {
        const sorted = state.sortKey === c.key;
        head += '<th class="' + (c.sortable === false ? "" : "sortable ") + (sorted ? "sorted " : "") +
          (c.className || "") + '" data-key="' + c.key + '"' + (c.width ? ' style="width:' + c.width + '"' : "") + ">" +
          c.label +
          (c.sortable === false ? "" :
            '<i class="fa-solid ' + (sorted ? (state.sortDir === "asc" ? "fa-arrow-up-short-wide" : "fa-arrow-down-wide-short") : "fa-sort") + ' sort-ic"></i>') +
          "</th>";
      });
      head += "</tr></thead>";

      let body = "<tbody>";
      if (!slice.length) {
        body += '<tr><td colspan="' + cols.length + '"><div class="ga-empty">' +
          '<i class="fa-solid fa-inbox"></i>' + (config.empty || "No records found.") + "</div></td></tr>";
      } else {
        slice.forEach(function (row, i) {
          body += '<tr data-index="' + (start + i) + '">';
          cols.forEach(function (c) {
            const raw = valueOf(row, c);
            body += '<td class="' + (c.className || "") + '">' +
              (c.render ? c.render(raw, row) : esc(raw)) + "</td>";
          });
          body += "</tr>";
        });
      }
      body += "</tbody>";

      // Rendering *is* the end of loading — clear any skeleton or dim before
      // we replace the contents, so callers never have to pair GA.loading()
      // with a matching GA.done().
      $host.removeClass("ga-busy ga-busy-quiet").data("ga-filled", true);
      // Above the table, not in the pager: on a long list the pager is off
      // screen, and pages that already carry their own export button put it
      // above too, so one position keeps them from reading as duplicates.
      const toolbar = config.csv === false ? "" :
        '<div class="d-flex justify-content-end pb-2">' +
        '<button type="button" class="btn btn-light btn-sm ga-csv" ' +
        'title="Download these ' + state.view.length + ' rows as CSV">' +
        '<i class="fa-solid fa-file-csv me-1"></i>CSV</button></div>';
      $host.html(toolbar +
        '<div class="ga-table-wrap"><table class="ga-table">' + head + body + "</table></div>" +
        pager());

      $host.find("th.sortable").on("click", function () {
        const key = $(this).data("key");
        if (state.sortKey === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        else { state.sortKey = key; state.sortDir = "asc"; }
        apply();
      });
      $host.find(".ga-csv").on("click", function () { exportCsv(); });
      $host.find("[data-page]").on("click", function (e) {
        e.preventDefault();
        state.page = parseInt($(this).data("page"), 10);
        apply();
      });
      if (config.onRowClick) {
        $host.find("tbody tr[data-index]").css("cursor", "pointer").on("click", function (e) {
          if ($(e.target).closest("button,a,input,select").length) return;
          config.onRowClick(state.view[$(this).data("index")]);
        });
      }
      if (config.onRender) config.onRender($host, state);
    }

    /**
     * The columns worth putting in a CSV.
     *
     * Action columns (edit/delete buttons) have no label and no data, so they
     * are dropped — a column of empty strings helps nobody.
     */
    function csvColumns() {
      return cols.filter(c => String(c.label || "").trim() !== "");
    }

    /**
     * Export what is on screen: current search, filters and sort order, every
     * page — not just the visible one, and not the unfiltered set either.
     *
     * Values come from the row rather than the rendered cell, so a pill or a
     * button never lands in the file as markup. A column whose `render` builds
     * text from several fields is the one case this cannot see, so `csv` on the
     * column can supply a plain-text value instead.
     */
    function exportCsv(filename) {
      const columns = csvColumns();
      if (!state.view.length) { toast("Nothing to export.", "warn"); return; }
      const header = columns.map(c => c.label);
      const body = state.view.map(function (row) {
        return columns.map(function (c) {
          if (typeof c.csv === "function") return c.csv(valueOf(row, c), row);
          return csvText(valueOf(row, c));
        });
      });
      downloadCsv(filename || csvFilename(), header, body);
    }

    function csvFilename() {
      const base = (config.csvName ||
                    $host.closest(".card").find(".card-header").first().text().trim() ||
                    document.title || "table")
        .replace(/[^\w\s-]/g, "").trim().replace(/\s+/g, "-").toLowerCase()
        .slice(0, 60) || "table";
      return base + "-" + new Date().toISOString().slice(0, 10) + ".csv";
    }

    function pager() {
      const total = state.view.length;
      const pages = Math.max(1, Math.ceil(total / state.perPage));
      if (total === 0) return "";
      const from = (state.page - 1) * state.perPage + 1;
      const to = Math.min(total, state.page * state.perPage);
      let html = '<div class="d-flex flex-wrap justify-content-between align-items-center pt-3 gap-2">' +
        '<small class="text-muted-2">Showing <b>' + from + "</b>–<b>" + to + "</b> of <b>" + total +
        "</b></small>";
      if (pages > 1) {
        html += '<nav><ul class="pagination pagination-sm mb-0">';
        html += '<li class="page-item ' + (state.page === 1 ? "disabled" : "") + '"><a class="page-link" href="#" data-page="' + (state.page - 1) + '">&laquo;</a></li>';
        const win = 2;
        for (let p = 1; p <= pages; p++) {
          if (p === 1 || p === pages || Math.abs(p - state.page) <= win) {
            html += '<li class="page-item ' + (p === state.page ? "active" : "") + '"><a class="page-link" href="#" data-page="' + p + '">' + p + "</a></li>";
          } else if (Math.abs(p - state.page) === win + 1) {
            html += '<li class="page-item disabled"><span class="page-link">…</span></li>';
          }
        }
        html += '<li class="page-item ' + (state.page === pages ? "disabled" : "") + '"><a class="page-link" href="#" data-page="' + (state.page + 1) + '">&raquo;</a></li>';
        html += "</ul></nav>";
      }
      return html + "</div>";
    }

    if (config.search) {
      $(config.search).on("input", function () {
        state.query = $(this).val().trim();
        state.page = 1;
        apply();
      });
    }

    apply();
    return {
      setRows: function (rows) { state.rows = rows || []; state.page = 1; apply(); },
      getRows: function () { return state.view; },
      exportCsv: exportCsv,
      refresh: apply,
      state: state
    };
  }

  /* -------------------------------------------------------------- Charts */
  const charts = {};
  const palette = ["#4f46e5", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6"];

  /* ------------------------------------------------------- Loading states */
  /**
   * Mark panels as loading.
   *
   * First load draws a skeleton in the shape of the content; a refilter dims
   * what is already there instead, so the page keeps its height and the stale
   * numbers do not masquerade as current ones.
   *
   * The kind is inferred: a <canvas> is a chart, a GA.table host is a table,
   * anything else is treated as a value. GA.chart and GA.table clear the state
   * themselves when they render, so most callers never need GA.done().
   *
   *   GA.loading("#chart-trend, #tbl-subjects, #kpi-overall");
   *
   * Pass {once:true} on pages that poll on a timer — the first load gets a
   * skeleton and every refresh after that is left alone, because dimming the
   * panel every few seconds just looks like flicker.
   */
  function loading(target, opts) {
    const o = $.extend({ rows: 6, cols: 4, once: false }, opts || {});
    $(target).each(function () {
      const $el = $(this);
      const isChart = $el.is("canvas");
      const $host = isChart ? $el.parent() : $el;

      // Seen data before? Then dim rather than replace.
      if ($host.data("ga-filled")) {
        if (o.once) return;
        $host.addClass("ga-busy" + ($host.data("ga-kind") === "stat" ? " ga-busy-quiet" : ""));
        return;
      }
      if (isChart) {
        $host.data("ga-kind", "chart").append('<div class="ga-skeleton ga-skel-chart ga-skel"></div>');
        $el.css("visibility", "hidden");
        return;
      }
      if ($el.data("ga-table") || $el.find(".ga-table").length) {
        $host.data("ga-kind", "table").html(skeletonRows(o.rows, o.cols));
        return;
      }
      // A value: keep the element's own box, swap the text for a bar.
      $host.data("ga-kind", "stat")
        .data("ga-text", $host.html())
        .html('<span class="ga-skeleton ga-skel-stat ga-skel d-inline-block"></span>');
    });
  }

  /**
   * Called when a request fails: replace every unresolved skeleton with a
   * short message. A skeleton that shimmers forever is worse than an error,
   * because it tells the user to keep waiting for something that is not coming.
   */
  function clearStuckLoaders(message) {
    $(".ga-busy").removeClass("ga-busy ga-busy-quiet");
    $(".ga-skel").each(function () {
      const $skel = $(this), $host = $skel.parent();
      $skel.remove();
      if ($host.children().length && !$host.is(":empty")) return;   // something rendered
      $host.html('<div class="ga-empty"><i class="fa-solid fa-plug-circle-exclamation"></i>' +
        esc(message || "Could not load this. Please retry.") + "</div>");
    });
    $("canvas").css("visibility", "");
  }

  function skeletonRows(rows, cols) {
    const widths = ["22%", "30%", "16%", "18%", "14%", "20%"];
    let html = '<div class="ga-table-wrap ga-skel"><div class="ga-skel-row ga-skel-head">';
    for (let c = 0; c < cols; c++) {
      html += '<span class="ga-skeleton" style="width:' + widths[c % widths.length] + '"></span>';
    }
    html += "</div>";
    for (let r = 0; r < rows; r++) {
      html += '<div class="ga-skel-row">';
      for (let c = 0; c < cols; c++) {
        html += '<span class="ga-skeleton" style="width:' + widths[(c + r) % widths.length] + '"></span>';
      }
      html += "</div>";
    }
    return html + "</div>";
  }

  /** Clear a loading state. Charts and tables do this for themselves. */
  function done(target) {
    $(target).each(function () {
      const $el = $(this);
      const $host = $el.is("canvas") ? $el.parent() : $el;
      $host.removeClass("ga-busy ga-busy-quiet");
      $host.children(".ga-skel").remove();
      $host.data("ga-filled", true);
      if ($el.is("canvas")) $el.css("visibility", "");
    });
  }

  /**
   * True when there is genuinely nothing to plot.
   *
   * Deliberately not "every value is 0" — a subject with 0% attendance is real
   * data and must still draw a bar. The exception is doughnut/pie, where a
   * zero total cannot render any geometry at all.
   */
  function chartIsEmpty(type, data) {
    if (!data || !data.datasets || !data.datasets.length) return true;
    const points = data.datasets.reduce((n, ds) => n + ((ds.data && ds.data.length) || 0), 0);
    if (!points) return true;
    if (type === "doughnut" || type === "pie" || type === "polarArea") {
      const total = data.datasets.reduce(
        (s, ds) => s + (ds.data || []).reduce((a, v) => a + (Number(v) || 0), 0), 0);
      return total === 0;
    }
    return false;
  }

  function chart(id, type, data, options) {
    const el = document.getElementById(id);
    if (!el) return null;
    const $el = $(el), $host = $el.parent();
    done(el);                                   // whatever we do next, loading is over

    $host.children(".ga-chart-empty").remove();
    if (chartIsEmpty(type, data)) {
      if (charts[id]) { charts[id].destroy(); delete charts[id]; }
      $el.css("visibility", "hidden");
      $host.append(
        '<div class="ga-empty ga-chart-empty d-flex flex-column justify-content-center h-100">' +
        '<i class="fa-solid fa-chart-simple"></i>' +
        esc((options && options.empty) || "No data for this selection.") + "</div>");
      return null;
    }
    $el.css("visibility", "");
    if (charts[id]) charts[id].destroy();
    const base = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: type !== "bar" && type !== "line", labels: { usePointStyle: true, boxWidth: 8, font: { size: 11 } } },
        tooltip: {
          backgroundColor: "#0f172a", padding: 10, cornerRadius: 8, titleFont: { size: 12 },
          bodyFont: { size: 12 }, displayColors: true
        }
      },
      scales: (type === "doughnut" || type === "pie" || type === "radar" || type === "polarArea") ? {} : {
        x: { grid: { display: false }, ticks: { font: { size: 10 }, color: "#94a3b8" } },
        y: { beginAtZero: true, grid: { color: "#f1f5f9" }, ticks: { font: { size: 10 }, color: "#94a3b8" } }
      }
    };
    // `empty` is ours, not Chart.js's — strip it before handing options over.
    const opts = $.extend(true, {}, options || {});
    delete opts.empty;
    charts[id] = new Chart(el, { type: type, data: data, options: $.extend(true, base, opts) });
    return charts[id];
  }

  /* ------------------------------------------------------------ Location */
  /**
   * Resolve with the best position fix we can get.
   *
   * getCurrentPosition() hands back the *first* fix the OS produces. On a phone
   * that is almost always the WiFi/cell estimate (±100–2000 m), which arrives in
   * about a second, while the real GPS lock lands 5–30 s later. Taking that
   * first answer is why a student standing in the classroom gets told their fix
   * is too imprecise.
   *
   * So when `desiredAccuracy` is set we watch instead: keep the sharpest fix
   * seen, finish the moment it is good enough, and fall back to the best we got
   * when the timeout expires. Callers that just want a rough position (the
   * teacher generating a session) can leave `desiredAccuracy` at 0 and get the
   * original single-shot behaviour.
   *
   * @param {number} [opts.desiredAccuracy] metres; 0 = take the first fix
   * @param {function} [opts.onProgress] called with each improved fix
   */
  function location_(opts) {
    const o = $.extend({
      timeout: 15000,
      highAccuracy: true,
      maximumAge: 0,
      desiredAccuracy: 0,
      onProgress: null
    }, opts || {});
    const d = $.Deferred();

    if (!navigator.geolocation) {
      d.reject({ code: 0, message: "This browser does not support location services." });
      return d.promise();
    }
    if (!window.isSecureContext && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") {
      d.reject({ code: 0, message: "Location requires a secure (HTTPS) connection." });
      return d.promise();
    }

    const errMsg = function (err) {
      const msgs = {
        1: "Location permission was denied. Please allow location access in your browser and retry.",
        2: "Your location is unavailable right now. Move to an open area and retry.",
        3: "Timed out while getting your location. Please retry."
      };
      return { code: err.code, message: msgs[err.code] || err.message };
    };
    const read = function (pos) {
      return {
        latitude: pos.coords.latitude,
        longitude: pos.coords.longitude,
        accuracy: pos.coords.accuracy
      };
    };
    const geoOpts = {
      enableHighAccuracy: o.highAccuracy,
      timeout: o.timeout,
      maximumAge: o.maximumAge
    };

    if (!o.desiredAccuracy) {
      navigator.geolocation.getCurrentPosition(
        function (pos) { d.resolve(read(pos)); },
        function (err) { d.reject(errMsg(err)); },
        geoOpts
      );
      return d.promise();
    }

    let best = null, watchId = null, timer = null, settled = false;
    const stop = function () {
      if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
      if (timer) { clearTimeout(timer); timer = null; }
    };
    const finish = function () {
      if (settled) return;
      settled = true;
      stop();
      // Hand back the best fix even if it never reached the target — the server
      // decides whether it is good enough, and its message names the number.
      if (best) d.resolve(best);
      else d.reject({ code: 3, message: "Timed out while getting your location. Please retry." });
    };

    watchId = navigator.geolocation.watchPosition(
      function (pos) {
        const fix = read(pos);
        if (!best || fix.accuracy < best.accuracy) {
          best = fix;
          if (o.onProgress) { try { o.onProgress(best); } catch (e) { /* display only */ } }
        }
        if (best.accuracy <= o.desiredAccuracy) finish();
      },
      function (err) {
        // A single failed update is not fatal while we already hold a fix —
        // let the timer decide. With nothing in hand, give up immediately.
        if (best) return;
        if (settled) return;
        settled = true;
        stop();
        d.reject(errMsg(err));
      },
      geoOpts
    );
    timer = setTimeout(finish, o.timeout);

    return d.promise();
  }

  /* --------------------------------------------------- Device signature */
  function deviceHash() {
    try {
      const c = document.createElement("canvas");
      const ctx = c.getContext("2d");
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60";
      ctx.fillRect(0, 0, 62, 20);
      ctx.fillStyle = "#069";
      ctx.fillText("GeoAttend", 2, 2);
      const bits = [
        c.toDataURL().slice(-96),
        navigator.hardwareConcurrency || 0,
        screen.width + "x" + screen.height + "x" + screen.colorDepth,
        Intl.DateTimeFormat().resolvedOptions().timeZone,
        navigator.language,
        navigator.maxTouchPoints || 0
      ].join("|");
      let h = 0;
      for (let i = 0; i < bits.length; i++) { h = (h << 5) - h + bits.charCodeAt(i); h |= 0; }
      return "d" + Math.abs(h).toString(36) + bits.length.toString(36);
    } catch (e) {
      return "unknown";
    }
  }

  /* ------------------------------------------------------------ Countdown */
  function countdown($el, seconds, onEnd) {
    let left = seconds;
    function tick() {
      $el.text(mmss(left));
      if (left <= 30) $el.addClass("text-danger");
      if (left <= 0) { clearInterval(timer); if (onEnd) onEnd(); return; }
      left--;
    }
    tick();
    const timer = setInterval(tick, 1000);
    return { stop: () => clearInterval(timer), reset: (s) => { left = s; $el.removeClass("text-danger"); } };
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => toast("Copied to clipboard.", "ok"));
    } else {
      const $t = $("<textarea>").val(text).css({ position: "fixed", opacity: 0 }).appendTo("body");
      $t[0].select();
      document.execCommand("copy");
      $t.remove();
      toast("Copied to clipboard.", "ok");
    }
  }

  function query(obj) {
    const clean = {};
    $.each(obj, function (k, v) { if (v !== "" && v !== null && v !== undefined && v !== "all") clean[k] = v; });
    return clean;
  }


  /* --------------------------------------------------- Absence reasons */
  /**
   * Show one class's absence reason, and let a student give one.
   *
   * Lives here rather than in a template because three screens need it — the
   * student dashboard, "My attendance", and the staff view of a student — and
   * three copies of a modal is three places for them to drift apart.
   *
   * @param {object} row      a class-history row from the API
   * @param {boolean} opts.allowSubmit  viewer may submit (a student, in window)
   * @param {function} opts.onSubmitted called after a successful submit
   */
  /* ------------------------------------------- Evidence on an absence */
  //
  // Mirrors attendance.services.validate_attachments. The server checks all of
  // this again and its answer is the one that counts — these numbers exist so
  // a mistake costs a moment rather than a 20 MB upload over a phone
  // connection.
  const ATTACH = {
    maxFiles: 5,
    maxMb: 20,
    accept: ".pdf,.jpg,.jpeg,.png,.webp,.heic,.heif"
  };

  /** Download links for the files on a request. */
  function attachmentList(items, opts) {
    const o = $.extend({ empty: "", label: "ATTACHMENTS" }, opts || {});
    if (!items || !items.length) return o.empty;
    return '<div class="mt-2"><div class="fs-12 text-muted-2 fw-600 mb-1">' + o.label +
      "</div>" +
      items.map(function (a) {
        return '<a class="ga-chip d-inline-flex align-items-center gap-1 mb-1 me-1" ' +
          'href="/attendance/api/attachments/' + a.id + '/" ' +
          'title="Download ' + esc(a.name) + '"><i class="fa-solid ' +
          (a.is_image ? "fa-image" : "fa-file-pdf") + '"></i>' + esc(a.name) +
          ' <span class="text-muted-2">(' + esc(a.size) + ")</span></a>";
      }).join("") + "</div>";
  }

  /** The file input and its rules, for a form that accepts evidence. */
  function attachField(id) {
    return '<label class="form-label mt-3">Evidence ' +
      '<span class="text-muted-2">(optional)</span></label>' +
      '<input type="file" class="form-control" id="' + id + '" name="files[]" multiple ' +
      'accept="' + ATTACH.accept + '">' +
      '<div class="form-text">A doctor\'s note, a ticket, a letter — up to ' +
      ATTACH.maxFiles + " PDFs or photos, " + ATTACH.maxMb + " MB in total.</div>" +
      '<div class="invalid-feedback d-block" id="' + id + '-err"></div>';
  }

  /**
   * Check the chosen files before uploading them.
   *
   * @returns {{ok: boolean, files: File[], message: string=}}
   */
  function attachCheck(input) {
    const files = Array.prototype.slice.call((input && input.files) || []);
    if (!files.length) return { ok: true, files: files };
    if (files.length > ATTACH.maxFiles) {
      return { ok: false, files: files,
               message: "Please choose at most " + ATTACH.maxFiles + " files — you chose " +
                        files.length + "." };
    }
    const total = files.reduce(function (n, f) { return n + f.size; }, 0);
    if (total > ATTACH.maxMb * 1024 * 1024) {
      return { ok: false, files: files,
               message: "Those files come to " + (total / 1048576).toFixed(1) +
                        " MB — the limit is " + ATTACH.maxMb + " MB in total." };
    }
    return { ok: true, files: files };
  }

  /** Build the multipart body for a submit that may carry evidence. */
  function attachForm(fields, files) {
    const body = new FormData();
    $.each(fields || {}, function (k, v) {
      if ($.isArray(v)) v.forEach(function (item) { body.append(k, item); });
      else body.append(k, v);
    });
    (files || []).forEach(function (f) { body.append("files[]", f); });
    return body;
  }

  function absenceReason(row, opts) {
    const o = $.extend({ allowSubmit: false, onSubmitted: null }, opts || {});
    const canSubmit = o.allowSubmit && row.can_explain && !row.reason_status;

    if (!$("#ga-reason-modal").length) {
      $("body").append(
        '<div class="modal fade" id="ga-reason-modal" tabindex="-1">' +
        '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title">' +
        '<i class="fa-solid fa-comment-dots me-2 text-primary"></i>Absence reason</h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '<div class="modal-body" id="ga-reason-body"></div>' +
        '<div class="modal-footer" id="ga-reason-foot"></div>' +
        "</div></div></div>");
    }
    const $modal = $("#ga-reason-modal");
    const context = '<div class="ga-pill pill-violet mb-2">' + esc(row.subject) +
      " · " + esc(row.date) + "</div>";

    let body, foot;
    if (row.reason_status) {
      const tone = row.reason_status === "APPROVED" ? "pill-green"
                 : row.reason_status === "REJECTED" ? "pill-red" : "pill-amber";
      body = context +
        '<div class="mb-2"><span class="ga-pill ' + tone + '">' +
        esc(row.reason_status_label) + "</span></div>" +
        '<div class="border rounded-3 p-2 mb-2" style="background:#f8fafc">' +
        '<div class="fs-12 text-muted-2 fw-600 mb-1">YOUR REASON</div>' +
        '<div style="white-space:pre-wrap">' + esc(row.reason) + "</div></div>" +
        (row.reason_remark
          ? '<div class="border rounded-3 p-2"><div class="fs-12 text-muted-2 fw-600 mb-1">' +
            "REMARK FROM " + esc(row.reason_reviewed_by || "the reviewer").toUpperCase() +
            '</div><div>' + esc(row.reason_remark) + "</div></div>"
          : (row.reason_status === "PENDING"
              ? '<div class="text-muted-2 fs-13">Waiting for your teacher to review it.</div>'
              : "")) +
        attachmentList(row.reason_attachments) +
        (row.reason_status === "REJECTED"
          ? '<div class="text-muted-2 fs-13 mt-2">A decision is final — this class ' +
            "cannot be explained again.</div>"
          : "");
      foot = '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Close</button>';
    } else if (canSubmit) {
      body = context +
        '<label class="form-label">Why were you absent? *</label>' +
        '<textarea class="form-control" id="ga-reason-text" rows="4" maxlength="1000" ' +
        'placeholder="Explain briefly — your teacher will review it."></textarea>' +
        '<div class="invalid-feedback d-block" id="ga-reason-err"></div>' +
        attachField("ga-reason-files") +
        '<div class="form-text">You can only do this once, and it cannot be edited ' +
        "afterwards. It does not change your attendance percentage — it is recorded " +
        "alongside it for your teacher.</div>";
      foot = '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Cancel</button>' +
        '<button type="button" class="btn btn-primary" id="ga-reason-send">Submit reason</button>';
    } else {
      // Nothing was submitted and nothing can be. Say which, rather than
      // showing an empty box that looks broken.
      body = context +
        '<div class="ga-empty py-4"><i class="fa-solid fa-inbox"></i>' +
        (o.allowSubmit
          ? "No reason was given, and the window for this class has closed."
          : "The student did not give a reason for this class.") + "</div>";
      foot = '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Close</button>';
    }

    $("#ga-reason-body").html(body);
    $("#ga-reason-foot").html(foot);

    $("#ga-reason-send").off("click").on("click", function () {
      const $btn = $(this), text = $("#ga-reason-text").val();
      $("#ga-reason-err").text("");
      $("#ga-reason-files-err").text("");
      const picked = attachCheck($("#ga-reason-files")[0]);
      if (!picked.ok) { $("#ga-reason-files-err").text(picked.message); return; }
      // "Uploading" rather than "Sending" once there are files: a 20 MB upload
      // on a phone is long enough that the wording matters.
      btnBusy($btn, true, picked.files.length ? "Uploading…" : "Sending…");
      post("/attendance/api/sessions/" + row.session_id + "/reason/",
           attachForm({ reason: text }, picked.files), { quiet: true })
        .done(function (res) {
          toast(res.message, "ok");
          bootstrap.Modal.getInstance($modal[0]).hide();
          if (o.onSubmitted) o.onSubmitted(res.data && res.data.row);
        })
        .fail(function (xhr) {
          const r = (xhr && xhr.responseJSON) || {};
          $("#ga-reason-err").text(r.message || "Could not submit that.");
        })
        .always(function () { btnBusy($btn, false); });
    });

    bootstrap.Modal.getOrCreateInstance($modal[0]).show();
  }

  /**
   * The Status cell for a class-history row, clickable when absent.
   *
   * Colour alone carries the state: amber once a reason has been given (in any
   * review state), red while there is none. The label stays plain "Absent" so
   * the column keeps a steady width and does not turn into a wall of text —
   * the detail is one click away, and in the tooltip for anyone hovering.
   */
  function absenceCell(row) {
    if (row.status !== "ABSENT") return statusPill(row.status);
    const explained = Boolean(row.reason_status);
    const tip = explained
      ? row.reason_status_label + " — click to read the reason"
      : (row.can_explain ? "No reason given yet — click to add one"
                         : "No reason was given for this class");
    return '<span class="ga-pill ' + (explained ? "pill-amber" : "pill-red") +
      ' ga-reason-open" style="cursor:pointer" data-session="' + row.session_id +
      '" title="' + esc(tip) + '"><i class="fa-solid fa-circle-xmark"></i>Absent</span>';
  }


  /**
   * One subject's verdict on a planned absence.
   *
   * A tooltip cannot hold a multi-line reason plus a remark, is invisible on
   * touch, and cannot be copied — so the chip opens this instead.
   *
   * @param {object} planned   a planned-absence row
   * @param {object} decision  the subject decision within it
   */
  function plannedDecision(planned, decision) {
    if (!planned || !decision) return;
    if (!$("#ga-planned-modal").length) {
      $("body").append(
        '<div class="modal fade" id="ga-planned-modal" tabindex="-1">' +
        '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title">' +
        '<i class="fa-solid fa-calendar-plus me-2 text-primary"></i>Planned absence</h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '<div class="modal-body" id="ga-planned-body"></div>' +
        '<div class="modal-footer">' +
        '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Close</button>' +
        "</div></div></div></div>");
    }
    const tone = decision.status === "APPROVED" ? "pill-green"
               : decision.status === "REJECTED" ? "pill-red" : "pill-amber";
    const who = decision.reviewed_by
      ? '<div class="text-muted-2 fs-13 mt-1">Decided by ' + esc(decision.reviewed_by) + "</div>"
      : "";
    $("#ga-planned-body").html(
      '<div class="mb-2"><span class="ga-pill pill-violet">' + esc(decision.subject) +
      " — " + esc(decision.subject_name) + "</span>" +
      '<span class="ga-pill pill-blue ms-1">' + esc(planned.from_date) + " – " +
      esc(planned.to_date) + "</span></div>" +
      '<div class="mb-3"><span class="ga-pill ' + tone + '">' +
      esc(decision.status_label) + "</span>" + who + "</div>" +
      '<div class="border rounded-3 p-2 mb-2" style="background:#f8fafc">' +
      '<div class="fs-12 text-muted-2 fw-600 mb-1">REASON GIVEN</div>' +
      '<div style="white-space:pre-wrap">' + esc(planned.reason) + "</div></div>" +
      (decision.remark
        ? '<div class="border rounded-3 p-2"><div class="fs-12 text-muted-2 fw-600 mb-1">' +
          "REMARK</div><div style=\"white-space:pre-wrap\">" + esc(decision.remark) +
          "</div></div>"
        : (decision.status === "PENDING"
            ? '<div class="text-muted-2 fs-13">Not yet reviewed for this subject.</div>'
            : '<div class="text-muted-2 fs-13">No remark was left.</div>')) +
      attachmentList(planned.attachments));
    bootstrap.Modal.getOrCreateInstance($("#ga-planned-modal")[0]).show();
  }

  /**
   * One subject's reason inside a grouped missed-class row.
   *
   * Grouping a day into one row means the reason, the remark and the verdict
   * can no longer be columns — they differ per subject. The chip opens this
   * instead, which also survives a touchscreen, where a tooltip does not.
   *
   * @param {object} group  a grouped row (one student, one day)
   * @param {object} item   the one class within it
   */
  function reasonDetail(group, item) {
    if (!group || !item) return;
    if (!$("#ga-rdetail-modal").length) {
      $("body").append(
        '<div class="modal fade" id="ga-rdetail-modal" tabindex="-1">' +
        '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title">' +
        '<i class="fa-solid fa-comment-dots me-2 text-primary"></i>Absence reason</h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
        '<div class="modal-body" id="ga-rdetail-body"></div>' +
        '<div class="modal-footer">' +
        '<button type="button" class="btn btn-light" data-bs-dismiss="modal">Close</button>' +
        "</div></div></div></div>");
    }
    const tone = item.status === "APPROVED" ? "pill-green"
               : item.status === "REJECTED" ? "pill-red" : "pill-amber";
    const who = item.reviewed_by
      ? '<div class="text-muted-2 fs-13 mt-1">Decided by ' + esc(item.reviewed_by) +
        (item.reviewed_at ? " · " + esc(item.reviewed_at) : "") + "</div>"
      : "";
    const student = group.student
      ? '<div class="fw-600">' + esc(group.student) + "</div>" +
        '<div class="text-muted-2 fs-13 mb-2">' +
        esc(group.class_roll || group.email) +
        (group.batch ? " · " + esc(group.batch) : "") + "</div>"
      : "";
    $("#ga-rdetail-body").html(
      '<div class="mb-2"><span class="ga-pill pill-violet">' + esc(item.subject) +
      " — " + esc(item.subject_name) + "</span>" +
      '<span class="ga-pill pill-blue ms-1">' + esc(group.date) + "</span></div>" +
      student +
      '<div class="mb-3"><span class="ga-pill ' + tone + '">' +
      esc(item.status_label) + "</span>" + who + "</div>" +
      '<div class="border rounded-3 p-2 mb-2" style="background:#f8fafc">' +
      '<div class="fs-12 text-muted-2 fw-600 mb-1">REASON GIVEN</div>' +
      '<div style="white-space:pre-wrap">' + esc(item.reason) + "</div></div>" +
      (item.review_remark
        ? '<div class="border rounded-3 p-2"><div class="fs-12 text-muted-2 fw-600 mb-1">' +
          "REMARK</div><div style=\"white-space:pre-wrap\">" + esc(item.review_remark) +
          "</div></div>"
        : (item.status === "PENDING"
            ? '<div class="text-muted-2 fs-13">Not reviewed yet.</div>'
            : '<div class="text-muted-2 fs-13">No remark was left.</div>')) +
      attachmentList(item.attachments) +
      '<div class="text-muted-2 fs-12 mt-2">Submitted ' + esc(item.submitted_at) + "</div>");
    bootstrap.Modal.getOrCreateInstance($("#ga-rdetail-modal")[0]).show();
  }

  /** A clickable chip for one class within a grouped missed-class row. */
  function reasonChip(item) {
    const tone = item.status === "APPROVED" ? "pill-green"
               : item.status === "REJECTED" ? "pill-red" : "pill-amber";
    return '<span class="ga-pill ' + tone + ' ga-reason-chip" style="cursor:pointer" ' +
      'data-reason="' + item.id + '" title="' + esc(item.subject_name) + " — " +
      esc(item.status_label) + '">' + esc(item.subject) + "</span>";
  }

  /** A clickable chip for one subject decision. */
  function plannedChip(decision) {
    const tone = decision.status === "APPROVED" ? "pill-green"
               : decision.status === "REJECTED" ? "pill-red" : "pill-amber";
    return '<span class="ga-pill ' + tone + ' ga-planned-open" style="cursor:pointer" ' +
      'data-decision="' + decision.id + '">' + esc(decision.subject) + "</span>";
  }


  /**
   * Set the sidebar's pending-review count.
   *
   * Rendered server-side on first paint; the reasons page then calls this from
   * data it already loads, so approving something updates the sidebar without
   * a reload and without an extra request.
   */
  function reviewBadge(n) {
    const $b = $("#ga-review-badge");
    if (!$b.length) return;
    n = Number(n) || 0;
    $b.text(n > 99 ? "99+" : n)
      .attr("title", n === 1 ? "1 awaiting your review" : n + " awaiting your review")
      .toggleClass("d-none", n === 0);
  }

  /* --------------------------------------------------------------- Boot */
  $(function () {
    $("#ga-menu-toggle").on("click", function () {
      $(".ga-sidebar").toggleClass("open");
      if ($(".ga-sidebar").hasClass("open")) {
        $('<div class="ga-backdrop"></div>').appendTo("body").on("click", function () {
          $(".ga-sidebar").removeClass("open");
          $(this).remove();
        });
      } else { $(".ga-backdrop").remove(); }
    });
    $("[data-bs-toggle='tooltip']").each(function () { new bootstrap.Tooltip(this); });
    $(document).on("click", "[data-copy]", function () { copy($(this).data("copy")); });
  });

  return {
    csrf, toast, overlay, btnBusy, request, get, post, submit, showErrors, clearErrors,
    confirm, table, chart, charts, palette, location: location_, deviceHash, countdown,
    esc, pctPill, bar, statusPill, avatar, phone, mmss, copy, query,
    loading, done, chartIsEmpty, spin, absenceReason, absenceCell,
    plannedDecision, plannedChip, reasonDetail, reasonChip,
    attachmentList, attachField, attachCheck, attachForm, ATTACH,
    reviewBadge, downloadCsv, csvText
  };
})(jQuery);
