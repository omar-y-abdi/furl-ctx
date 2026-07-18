/* furl showcase, vanilla classic script. Works from the file scheme and http.
   Reads window.__FURL_DATA__ produced by site/data/generate.py. */
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.remove("no-js");

  var DATA = window.__FURL_DATA__;
  if (!DATA || !DATA.samples) {
    root.classList.add("no-js");
    return;
  }

  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- small helpers --------------------------------------------------- */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function commas(n) { return Math.round(n).toLocaleString("en-US"); }
  function pctFmt(n) { return (Math.round(n * 10) / 10).toFixed(1); }

  /* ---- bind aggregate + meta numbers from data ------------------------- */
  function bindNumbers() {
    var agg = DATA.aggregate || {};
    document.querySelectorAll("[data-agg]").forEach(function (n) {
      var key = n.getAttribute("data-agg");
      var v = agg[key];
      if (v == null) return;
      var suffix = n.getAttribute("data-suffix") || "";
      n.textContent = (/percent/.test(key) ? pctFmt(v) : commas(v)) + suffix;
    });
    document.querySelectorAll("[data-meta]").forEach(function (n) {
      var key = n.getAttribute("data-meta");
      if (DATA[key] != null) n.textContent = DATA[key];
    });
  }

  /* ---- reveal on scroll ------------------------------------------------ */
  function setupReveal() {
    var items = document.querySelectorAll(".reveal");
    if (REDUCED || !("IntersectionObserver" in window)) {
      items.forEach(function (n) { n.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.14, rootMargin: "0px 0px -8% 0px" });
    items.forEach(function (n, i) {
      n.style.transitionDelay = Math.min(i, 5) * 55 + "ms";
      io.observe(n);
    });
  }

  /* ---- count-up animation --------------------------------------------- */
  function animateCount(node, from, to, dur, opts) {
    opts = opts || {};
    var fmt = opts.decimals ? pctFmt : commas;
    var suffix = opts.suffix || "";
    if (REDUCED) { node.textContent = fmt(to) + suffix; return; }
    var start = null;
    function step(ts) {
      if (start == null) start = ts;
      var t = Math.min(1, (ts - start) / dur);
      var e = 1 - Math.pow(1 - t, 3);
      node.textContent = fmt(from + (to - from) * e) + suffix;
      if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ---- build the code viewport (before -> fold) ------------------------ */
  function buildRow(line, idx) {
    var r = el("div", "row " + (line.role === "keep" ? "krow" : "frow") + (line.anomaly ? " anomaly" : ""));
    r.appendChild(el("span", "ln", String(idx + 1)));
    r.appendChild(el("span", "tx", esc(line.text) || "&nbsp;"));
    return r;
  }

  function markerNode(sample) {
    var wrap = el("div", "marker");
    var inner = el("div", "marker-inner");
    var corner =
      '<svg class="marker-corner" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M4 4h11l5 5v11H4z" stroke="rgba(255,178,77,.5)" stroke-width="1.4"/>' +
      '<path d="M15 4v5h5" stroke="rgba(255,178,77,.5)" stroke-width="1.4"/></svg>';
    var foldHtml = esc(sample.marker_text).replace(/(\d[\d,]*)/, "<b>$1</b>");

    if (sample.fold_mode === "table" && sample.after_display) {
      var block = el("div", "table-block");
      block.appendChild(el("div", "table-cap", "furl compact table &middot; " + foldHtml));
      var rows = el("div", "table-rows");
      sample.after_display.forEach(function (l, i) {
        var cls = "trow" + (i === 0 ? " schema" : "") + (l.marker ? " marker-row" : "");
        rows.appendChild(el("div", cls, esc(l.text) || "&nbsp;"));
      });
      block.appendChild(rows);
      block.appendChild(el("div", "marker-hash", "hash " + esc(sample.hash)));
      inner.appendChild(block);
    } else {
      var card = el("div", "marker-card");
      card.appendChild(el("span", "marker-fold", foldHtml));
      card.appendChild(el("span", "marker-hash", "hash " + esc(sample.hash)));
      card.insertAdjacentHTML("beforeend", corner);
      inner.appendChild(card);
    }
    wrap.appendChild(inner);
    return wrap;
  }

  function buildCode(sample) {
    var code = el("div", "code");
    var lines = sample.before_lines;
    var bands = [];
    var i = 0, n = lines.length;
    var largest = null, largestLen = -1;
    while (i < n) {
      if (lines[i].role === "keep") {
        code.appendChild(buildRow(lines[i], i));
        i++;
      } else {
        var band = el("div", "band");
        var bandInner = el("div", "band-inner");
        var count = 0;
        while (i < n && lines[i].role === "fold") {
          var r = buildRow(lines[i], i);
          r.style.setProperty("--d", Math.min(count * 5, 240) + "ms");
          bandInner.appendChild(r);
          count++; i++;
        }
        band.appendChild(bandInner);
        code.appendChild(band);
        bands.push(band);
        if (count > largestLen) { largestLen = count; largest = band; }
      }
    }
    var marker = markerNode(sample);
    if (largest && largest.nextSibling) code.insertBefore(marker, largest.nextSibling);
    else code.appendChild(marker);
    return code;
  }

  /* ---- retrieve reveal ------------------------------------------------- */
  function highlightCall(call) {
    // color the retrieve( ... ) call: function name amber, string/number args brighter
    var m = call.match(/^(\w+)\((.*)\)$/);
    if (!m) return esc(call);
    return '<span class="rt-fn">' + esc(m[1]) + "</span>(" +
      esc(m[2]).replace(/(&quot;.*?&quot;|\b\d+\b)/g, '<span class="rt-arg">$1</span>') + ")";
  }

  function renderResult(sample) {
    var out = esc(sample.retrieval.result);
    // amber-highlight the numeric line-number prefixes "NNN:"
    out = out.replace(/(^|\n)(\d+):/g, '$1<span class="rt-lnum">$2:</span>');
    return out;
  }

  function byteExactNote(sample) {
    if (sample.retrieval.kind === "select") return "Any row is retrievable by value, exact.";
    if (sample.retrieval.full_byte_exact)
      return "The full original, all " + commas(sample.original_line_count) + " lines, retrieves byte-exact.";
    return "Retrieves exactly what you ask for.";
  }

  function buildRetrieve(sample, getFolded, requestFold) {
    var sec = el("div", "retrieve");
    sec.appendChild(el("p", "rt-eyebrow", "Nothing was deleted"));
    sec.appendChild(el("p", "rt-caption", esc(sample.retrieval.caption)));

    var callRow = el("div", "retrieve-call");
    var code = el("code", "rt-code", highlightCall(sample.retrieval.call));
    var btn = el("button", "btn primary retrieve-btn", esc(sample.retrieval.control_label));
    btn.type = "button";
    btn.disabled = true;
    callRow.appendChild(code);
    callRow.appendChild(btn);
    sec.appendChild(callRow);

    var outWrap = el("div", "retrieve-out");
    var outInner = el("div", "retrieve-out-inner");
    var panel = el("div", "rt-panel");
    var badge = sample.retrieval.kind === "select" ? "exact row" : "exact lines";
    panel.appendChild(el("div", "rt-badge", badge));
    panel.appendChild(el("pre", "rt-result", renderResult(sample)));
    panel.appendChild(el("p", "rt-caption", byteExactNote(sample)));
    outInner.appendChild(panel);
    outWrap.appendChild(outInner);
    sec.appendChild(outWrap);

    btn.addEventListener("click", function () {
      if (!getFolded()) requestFold();
      outWrap.classList.add("open");
      btn.textContent = "Retrieved";
      btn.disabled = true;
    });

    return { node: sec, enable: function () { sec.classList.add("enabled"); btn.disabled = false; },
             reset: function () { sec.classList.remove("enabled"); btn.disabled = true;
               outWrap.classList.remove("open"); btn.textContent = sample.retrieval.control_label; } };
  }

  /* ---- one panel ------------------------------------------------------- */
  function buildPanel(sample, index) {
    var panel = el("div", "panel");
    panel.id = "panel-" + sample.id;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", "tab-" + sample.id);
    panel.setAttribute("tabindex", "0");
    if (index !== 0) panel.hidden = true;

    // head
    var head = el("div", "panel-head");
    var title = el("div", "panel-title");
    title.appendChild(el("h3", null, esc(sample.label)));
    title.appendChild(el("p", "panel-kind", esc(sample.kind_label)));
    head.appendChild(title);
    head.appendChild(el("p", "panel-tagline", esc(sample.tagline)));
    var chips = el("div", "chips");
    chips.appendChild(el("span", "chip", "<b>" + commas(sample.tokens_before) + "</b> tokens"));
    chips.appendChild(el("span", "chip arrow", "&rarr;"));
    chips.appendChild(el("span", "chip", "<b>" + commas(sample.tokens_after) + "</b> tokens"));
    chips.appendChild(el("span", "chip pct", "<b>" + pctFmt(sample.percent_saved) + "%</b> fewer"));
    chips.appendChild(el("span", "chip mono-chip", esc(sample.transform.replace(/:[0-9.]+$/, ""))));
    head.appendChild(chips);
    panel.appendChild(head);

    // stage
    var stage = el("div", "stage");
    var labels = el("div", "stage-labels");
    labels.innerHTML = '<span class="lbl-before">original &middot; ' + commas(sample.original_line_count) +
      ' lines</span><span class="lbl-after">after furl</span>';
    stage.appendChild(labels);
    var viewport = el("div", "viewport");
    viewport.setAttribute("data-mode", sample.fold_mode);
    viewport.appendChild(buildCode(sample));
    stage.appendChild(viewport);
    panel.appendChild(stage);

    // controls
    var controls = el("div", "controls");
    var compressBtn = el("button", "btn primary compress-btn", "Compress");
    compressBtn.type = "button";
    var resetBtn = el("button", "btn ghost reset-btn", "Reset");
    resetBtn.type = "button"; resetBtn.hidden = true;
    var meter = el("div", "meter"); var meterFill = el("span", "meter-fill"); meter.appendChild(meterFill);
    var meterPct = el("span", "meter-pct", "0%");
    controls.appendChild(compressBtn);
    controls.appendChild(resetBtn);
    controls.appendChild(meter);
    controls.appendChild(meterPct);
    panel.appendChild(controls);

    var folded = false;
    var retrieve = buildRetrieve(sample, function () { return folded; }, doFold);
    panel.appendChild(retrieve.node);

    function doFold() {
      if (folded) return;
      folded = true;
      viewport.scrollTop = 0;
      stage.classList.add("folded");
      viewport.classList.add("folded");
      meterFill.style.width = pctFmt(sample.percent_saved) + "%";
      animateCount(meterPct, 0, sample.percent_saved, 950, { decimals: true, suffix: "%" });
      compressBtn.disabled = true;
      compressBtn.textContent = "Folded";
      resetBtn.hidden = false;
      retrieve.enable();
    }
    function doReset() {
      folded = false;
      stage.classList.remove("folded");
      viewport.classList.remove("folded");
      viewport.scrollTop = 0;
      meterFill.style.width = "0";
      meterPct.textContent = "0%";
      compressBtn.disabled = false;
      compressBtn.textContent = "Compress";
      resetBtn.hidden = true;
      retrieve.reset();
    }
    compressBtn.addEventListener("click", doFold);
    resetBtn.addEventListener("click", doReset);

    return panel;
  }

  /* ---- tablist + keyboard nav (ARIA APG) ------------------------------- */
  function buildShell() {
    var shell = el("div", "panel-shell");
    var tablist = el("div", "tablist");
    tablist.setAttribute("role", "tablist");
    tablist.setAttribute("aria-label", "Captures");

    var tabs = [];
    var panels = [];

    DATA.samples.forEach(function (sample, i) {
      var tab = el("button", "tab");
      tab.type = "button";
      tab.id = "tab-" + sample.id;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", "panel-" + sample.id);
      tab.setAttribute("aria-selected", i === 0 ? "true" : "false");
      tab.setAttribute("tabindex", i === 0 ? "0" : "-1");
      tab.innerHTML = '<span class="tab-label">' + esc(sample.label) + "</span>" +
        '<span class="tab-pct">' + pctFmt(sample.percent_saved) + "%</span>";
      tablist.appendChild(tab);
      tabs.push(tab);

      var panel = buildPanel(sample, i);
      panels.push(panel);
    });

    function select(idx, focus) {
      tabs.forEach(function (t, i) {
        var on = i === idx;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.setAttribute("tabindex", on ? "0" : "-1");
        panels[i].hidden = !on;
      });
      if (focus) tabs[idx].focus();
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () { select(i, false); });
      tab.addEventListener("keydown", function (e) {
        var last = tabs.length - 1, next = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") next = i === last ? 0 : i + 1;
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = i === 0 ? last : i - 1;
        else if (e.key === "Home") next = 0;
        else if (e.key === "End") next = last;
        if (next != null) { e.preventDefault(); select(next, true); }
      });
    });

    shell.appendChild(tablist);
    panels.forEach(function (p) { shell.appendChild(p); });
    return shell;
  }

  /* ---- boot ------------------------------------------------------------
     The data is inlined, so mount the real panel synchronously. This end-of-
     body script runs before first paint, so the skeleton never swaps and the
     layout never shifts. The skeleton is the pre-script and no-JS fallback. */
  bindNumbers();
  setupReveal();
  var mount = document.getElementById("demo-mount");
  if (mount) {
    mount.textContent = "";
    mount.appendChild(buildShell());
  }
})();
