// Behaviour is delegated from here so CSP can stay strict and htmx-swapped
// fragments keep working without inline handlers.

(function () {
  var REDUCE = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // This tab's id, sent on review mutations so the server's review-changed
  // nudge can name where a change came from. The tab that made the change
  // skips the reload (its DOM is already current from the action's own
  // response) — reloading it too replaced the page mid-interaction and ate
  // the next tick of anyone working quickly down a review list.
  var TAB_ID = Math.random().toString(36).slice(2, 10)
    + Date.now().toString(36);
  window.qlTabId = TAB_ID;

  // Animate removals without layout jumps.
  function collapse(el, done) {
    if (!el || el.dataset.qlCollapsing === "1") return;
    el.dataset.qlCollapsing = "1";
    if (REDUCE) { if (done) done(); return; }
    var h = el.getBoundingClientRect().height;
    el.style.overflow = "hidden";
    el.style.height = h + "px";
    el.style.transition =
      "height 280ms ease 40ms, opacity 200ms ease, " +
      "margin 280ms ease 40ms, padding 280ms ease 40ms";
    void el.offsetHeight;  // force reflow so the start values stick
    el.style.opacity = "0";
    el.style.height = "0px";
    el.style.marginTop = "0";
    el.style.marginBottom = "0";
    el.style.paddingTop = "0";
    el.style.paddingBottom = "0";
    if (done) setTimeout(done, 320);
  }

  function markSearchDownloadQueued(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var root = item && item.closest("[data-search-results-root]");
    var forms = [form];
    if (root && key) {
      forms = Array.prototype.map.call(root.querySelectorAll("[data-search-item]"), function (peer) {
        if (peer.dataset.searchKey !== key) return null;
        return peer.querySelector("[data-search-download-form]");
      }).filter(Boolean);
    }
    forms.forEach(function (f) {
      var b = f.querySelector("button[type=submit]");
      if (!b) return;
      b.disabled = true;
      if (b.classList.contains("ql-download-icon-button")) {
        b.setAttribute("aria-label", "Queued");
        b.setAttribute("title", "Queued");
        b.classList.add("is-queued");
      } else {
        b.textContent = "Queued";
      }
      b.classList.remove("ql-btn-primary");
      b.classList.add("is-disabled");
      if (!b.classList.contains("ql-download-icon-button")) {
        b.classList.add("ql-btn-secondary");
      }
    });
  }

  // Disable a search-result button only after a real queue success.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-queue-button]")) return;
    if (!evt.detail || !evt.detail.successful) return;
    var xhr = evt.detail.xhr;
    if (!xhr || xhr.responseText.indexOf("ql-notice-success") === -1) return;
    markSearchDownloadQueued(form);
  });

  // Surface failed htmx requests instead of failing silently. A short
  // plain-text body is the route speaking (e.g. "couldn't save that
  // dismissal"); anything longer or HTML-shaped gets the generic line.
  document.addEventListener("htmx:responseError", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    var body = (xhr && xhr.responseText || "").trim();
    var msg = (body && body.length <= 200 && body.indexOf("<") === -1)
      ? body : "That didn't go through. Try again in a moment.";
    showToast(msg, "error");
  });
  document.addEventListener("htmx:sendError", function () {
    showToast("Couldn't reach the server. Check your connection and try again.", "error");
  });

  // Animate a fully hidden artist group before htmx removes it. Dismissals
  // target the group's positioning SHELL (the dismiss button lives beside the
  // <details>, not inside it), so match the shell as well as a bare details —
  // matching only the details left the swap unanimated and, worse, skipped
  // the qlHidden recount, so an emptied page kept its stale counts.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var t = evt.detail && evt.detail.target;
    if (!t || !t.matches) return;
    if (!t.matches("details[data-artist]")
        && !(t.matches(".ql-review-group-shell")
             && t.querySelector("details[data-artist]"))) return;
    if ((evt.detail.serverResponse || "").trim() !== "") return;
    collapse(t);
    // Recount after the delayed removal has actually finished.
    setTimeout(function () {
      document.body.dispatchEvent(new CustomEvent("qlHidden"));
    }, 360);
  });

  // "/" jumps to the search box from anywhere, unless you're already typing
  // in a field (so slashes in queries and paths still land). Pages without a
  // search box go to Search, carrying a hash so it lands focused.
  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "/" || evt.ctrlKey || evt.metaKey || evt.altKey) return;
    var t = evt.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
              t.tagName === "SELECT" || t.isContentEditable)) return;
    evt.preventDefault();
    var box = document.querySelector('input[name="q"]');
    if (!box) {
      window.location.href = "/#search";
      return;
    }
    box.focus();
    box.select();
  });
  function focusSearchFromHash() {
    if (window.location.hash !== "#search") return;
    var landBox = document.querySelector('input[name="q"]');
    if (landBox) landBox.focus();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", focusSearchFromHash);
  } else {
    focusSearchFromHash();
  }

  // Plain POST forms need immediate feedback while the redirect starts.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-busy-submit]")) return;
    var b = (evt.submitter && evt.submitter.type === "submit" ? evt.submitter : null)
          || form.querySelector("button[type=submit]");
    if (!b || b.disabled) return;
    setTimeout(function () {
      b.dataset.busyHtml = b.innerHTML;
      b.disabled = true;
      b.classList.add("is-disabled");
      b.innerHTML =
        '<span class="ql-spinner ql-inline-spinner" aria-hidden="true"></span> Starting...';
    }, 0);
  });

  // BFCache can restore a disabled submit button.
  window.addEventListener("pageshow", function (evt) {
    if (!evt.persisted) return;
    document.querySelectorAll("form[data-busy-submit] button[type=submit]").forEach(function (b) {
      if (b.dataset.busyHtml == null) return;
      b.disabled = false;
      b.classList.remove("is-disabled");
      b.innerHTML = b.dataset.busyHtml;
      delete b.dataset.busyHtml;
    });
  });

  // Styled confirm prompt (the app-drawn <dialog>, not the browser's popup),
  // falling back to window.confirm where <dialog> is unsupported.
  window.qlConfirm = function (msg, opts) {
    opts = opts || {};
    var d = document.getElementById("ql-confirm");
    if (!d || typeof d.showModal !== "function") {
      return Promise.resolve(window.confirm(msg));
    }
    return new Promise(function (resolve) {
      d.querySelector("[data-confirm-text]").textContent = msg;
      var okButton = d.querySelector("[data-confirm-ok]");
      okButton.textContent = opts.action || "Continue";
      okButton.classList.toggle("ql-btn-danger", !!opts.danger);
      okButton.classList.toggle("ql-btn-primary", !opts.danger);
      var choice = false;
      okButton.onclick = function () { choice = true; d.close(); };
      d.querySelector("[data-confirm-cancel]").onclick = function () { choice = false; d.close(); };
      d.addEventListener("close", function h() {
        d.removeEventListener("close", h);
        resolve(choice);
      });
      d.showModal();
    });
  };

  // Shared confirm for destructive submits and links. A {count} placeholder
  // picks up the number in the control's own label, so the review submit can
  // say how many albums a tap is really about to queue. Capture phase: the
  // original activation is swallowed, the dialog asks, and on Continue the
  // click is replayed with a bypass mark so forms/htmx fire exactly once.
  document.addEventListener("click", function (evt) {
    var el = evt.target.closest && evt.target.closest("[data-confirm]");
    if (!el) return;
    if (el.dataset.confirmBypass === "1") {
      delete el.dataset.confirmBypass;
      return;
    }
    evt.preventDefault();
    evt.stopPropagation();
    var msg = el.getAttribute("data-confirm");
    if (msg.indexOf("{count}") !== -1) {
      var m = (el.textContent || "").match(/[\d,]+/);
      msg = msg.replace("{count}", m ? m[0] : "the");
    }
    window.qlConfirm(msg, {
      action: el.getAttribute("data-confirm-action") || "",
      danger: el.hasAttribute("data-confirm-danger"),
    }).then(function (ok) {
      if (!ok) return;
      if (el.tagName === "FORM") {
        // A form's click() never submits it — fire a real submit event so
        // htmx (or the browser) takes it from here.
        el.requestSubmit();
        return;
      }
      el.dataset.confirmBypass = "1";
      el.click();
    });
  }, true);

  // hx-confirm attributes go through the same styled dialog instead of the
  // browser's native popup (htmx asks via this event before it would call
  // window.confirm itself).
  document.addEventListener("htmx:confirm", function (evt) {
    var q = evt.detail && evt.detail.question;
    if (!q) return;
    evt.preventDefault();
    var source = evt.detail && evt.detail.elt;
    window.qlConfirm(q, {
      action: (source && source.getAttribute("data-confirm-action")) || "",
      danger: !!(source && source.hasAttribute("data-confirm-danger")),
    }).then(function (ok) {
      if (ok) evt.detail.issueRequest(true);
    });
  });

  // Lock-busy retry button.
  document.addEventListener("click", function (evt) {
    if (evt.target.closest && evt.target.closest("[data-reload]")) {
      evt.preventDefault();
      location.reload();
    }
  });

  // Show/hide password and token fields.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-toggle-password]");
    if (!btn) return;
    var f = document.getElementById(btn.getAttribute("data-toggle-password"));
    if (!f) return;
    f.type = f.type === "password" ? "text" : "password";
    var isVisible = f.type === "text";
    var label = isVisible ? btn.dataset.hideLabel : btn.dataset.showLabel;
    var labelNode = btn.querySelector("[data-toggle-password-label]");
    var showIcon = btn.querySelector("[data-icon-show]");
    var hideIcon = btn.querySelector("[data-icon-hide]");
    btn.setAttribute("aria-pressed", isVisible ? "true" : "false");
    if (label) {
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      if (labelNode) labelNode.textContent = label;
    }
    if (showIcon) showIcon.classList.toggle("hidden", isVisible);
    if (hideIcon) hideIcon.classList.toggle("hidden", !isVisible);
  });

  // Keep the search field metadata in step with the search mode.
  document.addEventListener("change", function (evt) {
    var radio = evt.target;
    if (!radio.matches || !radio.matches('input[name="kind"]')) return;
    var form = radio.closest("form");
    var q = form && form.querySelector('input[name="q"]');
    if (!q) return;
    var placeholder = "Album title";
    if (radio.value === "artist") placeholder = "Artist name";
    if (radio.value === "track") placeholder = "Track title";
    q.setAttribute("placeholder", placeholder);
    q.setAttribute("aria-label", placeholder);
  });

  // Search result version lists use a real button so the row itself does not
  // mix "expand" and "download" tap targets.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-version-toggle]");
    if (!btn) return;
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    var open = btn.getAttribute("aria-expanded") === "true";
    var nextOpen = !open;
    btn.setAttribute("aria-expanded", nextOpen ? "true" : "false");
    panel.classList.toggle("hidden", !nextOpen);
    var label = btn.querySelector("[data-version-label]");
    if (label) {
      label.textContent = nextOpen
        ? (btn.dataset.hideLabel || "Hide versions")
        : (btn.dataset.showLabel || "Show versions");
    }
  });

  // Light/dark toggle.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("#theme-toggle, [data-theme-toggle]");
    if (!btn) return;
    var next = document.documentElement.getAttribute("data-theme") === "winter"
      ? "night" : "winter";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("ql-theme", next); } catch (e) { /* private mode */ }
    var m = document.querySelector('meta[name="theme-color"]');
    // Matches --ql-bg in tokens.css and the boot script in base.html.
    if (m) m.setAttribute("content", next === "winter" ? "#f3ecdf" : "#0f0b07");
  });

  // Keep the mobile drawer aria-expanded in step with the actual open state.
  function drawerIsOpen(dd) {
    return dd.classList.contains("ql-mobile-drawer-open") || dd.hasAttribute("open");
  }
  function syncDrawerExpanded(dd) {
    var btn = dd.querySelector("[aria-expanded]");
    if (btn) btn.setAttribute("aria-expanded", drawerIsOpen(dd).toString());
  }
  document.addEventListener("focusin", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".ql-mobile-drawer");
    if (dd) syncDrawerExpanded(dd);
  });
  document.addEventListener("focusout", function (evt) {
    var dd = evt.target.closest && evt.target.closest(".ql-mobile-drawer");
    if (!dd) return;
    // focusout fires before the new focus settles, so re-check next tick.
    setTimeout(function () { syncDrawerExpanded(dd); }, 0);
  });

  // Tap-driven drawer support for mobile browsers.
  function closeDrawer(dd) {
    dd.classList.remove("ql-mobile-drawer-open");
    dd.removeAttribute("open");
    if (dd.contains(document.activeElement) && document.activeElement.blur) {
      document.activeElement.blur();
    }
    var b = dd.querySelector("[aria-expanded]");
    if (b) b.setAttribute("aria-expanded", "false");
  }
  window.qlCloseDropdowns = function (root) {
    var scope = root || document;
    var nodes = [];
    if (scope.matches && scope.matches(".ql-mobile-drawer")) nodes.push(scope);
    scope.querySelectorAll(".ql-mobile-drawer.ql-mobile-drawer-open, details.ql-mobile-drawer[open]")
      .forEach(function (dd) {
        if (nodes.indexOf(dd) === -1) nodes.push(dd);
      });
    nodes.forEach(closeDrawer);
  };
  document.addEventListener("click", function (evt) {
    // The drawer trigger is its <summary> (a disclosure button, not a menu).
    var btn = evt.target.closest && evt.target.closest(".ql-mobile-drawer > summary");
    var trigger = (btn && btn.closest(".ql-mobile-drawer")) ? btn : null;
    var triggerDd = trigger ? trigger.closest(".ql-mobile-drawer") : null;
    // Close outside taps and other open drawers.
    document.querySelectorAll(".ql-mobile-drawer.ql-mobile-drawer-open").forEach(function (o) {
      if (o !== triggerDd) closeDrawer(o);
    });
    if (!triggerDd) return;
    evt.preventDefault();
    if (drawerIsOpen(triggerDd)) {
      closeDrawer(triggerDd);
    } else {
      triggerDd.classList.add("ql-mobile-drawer-open");
      triggerDd.setAttribute("open", "");
      trigger.setAttribute("aria-expanded", "true");
    }
  });
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-close-mobile-menu]");
    if (!btn) return;
    var dd = btn.closest(".ql-mobile-drawer");
    if (dd) closeDrawer(dd);
  });
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") window.qlCloseDropdowns();
  });

  // One-shot flash flags should not stay in the URL after first paint.
  var FLASH_PARAMS = ["approved", "stale", "saved", "queued", "connected",
                      "unverified", "mode", "error"];
  function cleanFlashUrl() {
    if (typeof URL !== "function" || !history.replaceState) return;
    try {
      var url = new URL(location.href);
      var touched = false;
      FLASH_PARAMS.forEach(function (k) {
        if (url.searchParams.has(k)) { url.searchParams.delete(k); touched = true; }
      });
      if (touched) {
        var qs = url.searchParams.toString();
        history.replaceState(null, "", url.pathname + (qs ? "?" + qs : "") + url.hash);
      }
    } catch (e) { /* malformed URL — leave it alone */ }
  }
  function fade(el) { collapse(el, function () { if (el.parentNode) el.remove(); }); }
  function autoDismissFlashes() {
    // Keep warnings/errors visible; auto-clear low-risk notices and toasts.
    document.querySelectorAll("[data-flash].ql-notice-success, [data-flash].ql-notice-info")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 6000); });
    document.querySelectorAll("#download-toast [data-flash]")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 8000); });
  }
  // Clear stale flashes after a job moves on.
  window.qlDismissAllFlashes = function () {
    document.querySelectorAll("[data-flash]").forEach(fade);
  };

  // Programmatic toast for async failures and review actions.
  function showToast(message, kind) {
    var host = document.getElementById("download-toast");
    if (!host) return;
    var el = document.createElement("div");
    var noticeKind = kind || "info";
    el.className = "ql-notice ql-notice-" + noticeKind;
    el.setAttribute("data-flash", "");
    el.setAttribute("data-flash-kind", noticeKind);
    el.setAttribute("role", "status");
    var span = document.createElement("span");
    span.textContent = message;
    el.appendChild(span);
    host.appendChild(el);
    setTimeout(function () { fade(el); }, 8000);
  }
  window.qlShowToast = showToast;

  function initCoverFallbacks() {
    document.querySelectorAll("img.ql-cover, img.ql-grid-cover, img.ql-result-art").forEach(function (img) {
      if (img.dataset.coverFallbackWired === "1") return;
      img.dataset.coverFallbackWired = "1";
      img.addEventListener("error", function () {
        var fallback = document.createElement("span");
        fallback.className = img.className + " ql-cover-placeholder";
        fallback.setAttribute("aria-hidden", "true");
        img.replaceWith(fallback);
      }, { once: true });
    });
  }

  function initSearchResults() {
    document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
      if (root.dataset.searchWired === "1") return;
      root.dataset.searchWired = "1";
      var selected = {};
      var bulkBar = root.querySelector("[data-search-bulk-bar]");
      var selectedCount = root.querySelector("[data-search-selected-count]");
      var selectAll = root.querySelector("[data-search-select-all]");
      var bulkButton = root.querySelector("[data-search-bulk-download]");
      var clearButton = root.querySelector("[data-search-clear-selection]");
      var hideOwnedButton = root.querySelector("[data-search-hide-owned]");

      function selectedKeys() {
        return Object.keys(selected).filter(function (k) { return selected[k]; });
      }
      function boxesFor(key) {
        return Array.prototype.filter.call(root.querySelectorAll("[data-search-select]"),
          function (cb) { return cb.dataset.searchKey === key; });
      }
      function visibleItem(item) {
        return item && item.offsetParent !== null;
      }
      function syncBoxes() {
        root.querySelectorAll("[data-search-select]").forEach(function (cb) {
          cb.checked = !!selected[cb.dataset.searchKey];
        });
        var keys = selectedKeys();
        if (bulkBar) bulkBar.classList.toggle("hidden", keys.length === 0);
        if (selectedCount) {
          selectedCount.textContent = keys.length + " selected";
        }
        if (selectAll) {
          var selectable = Array.prototype.filter.call(root.querySelectorAll("[data-search-select]"),
            function (cb) { return visibleItem(cb.closest("[data-search-item]")); });
          var selectableKeys = {};
          selectable.forEach(function (cb) { selectableKeys[cb.dataset.searchKey] = true; });
          var allKeys = Object.keys(selectableKeys);
          var picked = allKeys.filter(function (k) { return selected[k]; }).length;
          selectAll.checked = allKeys.length > 0 && picked === allKeys.length;
          selectAll.indeterminate = picked > 0 && picked < allKeys.length;
        }
      }
      function setView(name) {
        root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
          panel.classList.toggle("hidden", panel.dataset.searchViewPanel !== name);
        });
        root.querySelectorAll("[data-search-view]").forEach(function (btn) {
          var active = btn.dataset.searchView === name;
          btn.classList.toggle("is-active", active);
          btn.setAttribute("aria-pressed", active ? "true" : "false");
        });
        try { localStorage.setItem("ql-search-view", name); } catch (e) {}
      }
      function savedSearchView() {
        try { return localStorage.getItem("ql-search-view") || "table"; } catch (e) { return "table"; }
      }
      root.addEventListener("change", function (evt) {
        var cb = evt.target.closest && evt.target.closest("[data-search-select]");
        if (cb) {
          selected[cb.dataset.searchKey] = cb.checked;
          boxesFor(cb.dataset.searchKey).forEach(function (peer) {
            if (peer !== cb) peer.checked = cb.checked;
          });
          syncBoxes();
          return;
        }
        if (evt.target.closest && evt.target.closest("[data-search-select-all]")) {
          var on = evt.target.checked;
          root.querySelectorAll("[data-search-select]").forEach(function (box) {
            if (!visibleItem(box.closest("[data-search-item]"))) return;
            selected[box.dataset.searchKey] = on;
          });
          syncBoxes();
        }
      });
      root.addEventListener("click", function (evt) {
        var view = evt.target.closest && evt.target.closest("[data-search-view]");
        if (view) {
          evt.preventDefault();
          setView(view.dataset.searchView || "table");
          syncBoxes();
          return;
        }
        if (hideOwnedButton && evt.target.closest("[data-search-hide-owned]")) {
          evt.preventDefault();
          var next = hideOwnedButton.getAttribute("aria-pressed") !== "true";
          hideOwnedButton.setAttribute("aria-pressed", next ? "true" : "false");
          root.classList.toggle("is-filtering-owned", next);
          syncBoxes();
          return;
        }
        if (clearButton && evt.target.closest("[data-search-clear-selection]")) {
          evt.preventDefault();
          selected = {};
          syncBoxes();
          return;
        }
        if (bulkButton && evt.target.closest("[data-search-bulk-download]")) {
          evt.preventDefault();
          bulkDownload();
        }
      });
      function csrf() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.content : "";
      }
      function firstFormForKey(key) {
        var items = Array.prototype.filter.call(root.querySelectorAll("[data-search-item]"),
          function (item) { return item.dataset.searchKey === key; });
        for (var i = 0; i < items.length; i++) {
          var form = items[i].querySelector("[data-search-download-form]");
          if (form) return form;
        }
        return null;
      }
      function postForm(form) {
        return fetch(form.action || "/download", {
          method: "POST",
          headers: {
            "HX-Request": "true",
            "X-CSRF-Token": csrf(),
          },
          body: new FormData(form),
        }).then(function (r) {
          return r.text().then(function (text) {
            return { ok: r.ok, text: text };
          });
        });
      }
      function bulkDownload() {
        var keys = selectedKeys();
        if (!keys.length || bulkButton.disabled) return;
        var forms = keys.map(firstFormForKey).filter(Boolean);
        if (!forms.length) return;
        var albumLabel = keys.length === 1 ? "album" : "albums";
        window.qlConfirm("Download " + keys.length + " selected " + albumLabel + "? They queue now and import into your library.", { action: "Download" }).then(function (ok) {
          if (ok) runBulkDownload(forms);
        });
      }
      function runBulkDownload(forms) {
        var original = bulkButton.textContent;
        bulkButton.disabled = true;
        bulkButton.textContent = "Queueing...";
        var queued = 0;
        var skipped = 0;
        var failed = 0;
        var chain = Promise.resolve();
        forms.forEach(function (form) {
          chain = chain.then(function () {
            return postForm(form).then(function (res) {
              if (!res.ok || res.text.indexOf("ql-notice-error") >= 0) failed += 1;
              else if (res.text.indexOf("ql-notice-warning") >= 0) {
                skipped += 1;
                markSearchDownloadQueued(form);
              } else {
                queued += 1;
                markSearchDownloadQueued(form);
              }
            }).catch(function () { failed += 1; });
          });
        });
        chain.then(function () {
          bulkButton.disabled = false;
          bulkButton.textContent = original;
          selected = {};
          syncBoxes();
          var parts = [];
          if (queued) parts.push(queued + " queued");
          if (skipped) parts.push(skipped + " already queued");
          if (failed) parts.push(failed + " failed");
          showToast(parts.length ? parts.join(", ") + "." : "Nothing queued.", failed ? "error" : "success");
        });
      }
      setView(savedSearchView());
      syncBoxes();
    });
  }

  // Live job and queue streams.

  function unitSuffix(p) {
    return p.unit ? " " + p.unit + (p.total === 1 ? "" : "s") : "";
  }

  function fmtProgress(p, verb, withItem) {
    if (withItem === undefined) withItem = true;
    var item = (withItem && p.item) ? " · " + p.item : "";
    if (p.total > 0) {
      return verb + " " + p.current + " / " + p.total + unitSuffix(p) + item;
    }
    if (p.phase) return p.phase + item;
    return "";
  }

  function jobActivityFallback(kind, status) {
    if (status === "scanning") return "Scanning";
    if (kind === "upgrade") return "Upgrading";
    if (kind === "downsample") return "Downsampling";
    if (kind === "repair") return "Repairing";
    if (kind === "lyrics") return "Fetching lyrics";
    if (kind === "migration") return "Migrating";
    return (kind === "download" || ["album", "library", "new_releases"].indexOf(kind) >= 0)
      ? "Downloading" : "Running";
  }

  // Dashboard and queue progress cards.
  function wireStreamCard(card) {
    if (card.dataset.sseWired === "1") return;
    card.dataset.sseWired = "1";
    var id = card.dataset.jobId;
    var surface = card.dataset.jobSurface;   // "dashboard" | "queue"
    var status = card.dataset.jobStatus;
    var kind = card.dataset.jobKind || "";
    var runFallback = jobActivityFallback(kind, status);
    if (!id || !surface) return;
    // Once a queued card starts, refresh it into the live running layout.
    var flippedFromPending = false;
    var progId = (surface === "dashboard" ? "dash-prog-" : "card-prog-") + id;
    var containerId = surface === "dashboard" ? "dashboard-active"
                    : "queue-body";
    var reconnect = surface === "dashboard" ? document.getElementById("dash-reconnect-" + id) : null;
    var src = new EventSource("/api/jobs/" + id + "/stream");
    function shut() {
      try { src.close(); } catch (e) {}
      document.removeEventListener("htmx:beforeSwap", onSwap);
    }
    function onSwap(e) {
      if (!e.detail || !e.detail.target) return;
      if (e.detail.target.id === containerId) shut();
    }
    document.addEventListener("htmx:beforeSwap", onSwap);
    if (reconnect) {
      var warnDelay = null;
      src.onopen = function () {
        if (warnDelay) { clearTimeout(warnDelay); warnDelay = null; }
        reconnect.classList.add("hidden");
      };
      src.onerror = function () {
        if (src.readyState === EventSource.CLOSED) {
          if (warnDelay) { clearTimeout(warnDelay); warnDelay = null; }
          reconnect.textContent = "Connection lost. Reload to see the latest.";
          reconnect.classList.remove("is-warning");
          reconnect.classList.add("is-error");
          reconnect.classList.remove("hidden");
        } else if (!warnDelay) {
          // Phones drop the stream the moment the tab backgrounds; only warn
          // when a reconnect still hasn't landed after a real wait.
          warnDelay = setTimeout(function () {
            warnDelay = null;
            reconnect.classList.remove("is-error");
            reconnect.classList.add("is-warning");
            reconnect.classList.remove("hidden");
          }, 5000);
        }
      };
    }
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      // Re-render once a pending queue card starts.
      if (surface === "queue" && status === "pending" && !flippedFromPending) {
        flippedFromPending = true;
        shut();
        if (window.htmx) {
          window.htmx.ajax("GET", "/queue",
            { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
        }
        return;
      }
      var el = document.getElementById(progId);
      if (!el) return;
      var txt = fmtProgress(p, status === "scanning" ? "Scanning" : (p.phase || runFallback),
                            surface !== "dashboard");
      if (txt) el.textContent = txt;
      var bar = document.getElementById("card-bar-" + id);
      if (bar && p.total > 0) {
        var fill = bar.querySelector("i");
        if (fill) fill.style.width = Math.min(100, (p.current / p.total) * 100) + "%";
      }
    });
    src.addEventListener("done", function (e) {
      shut();
      var endStatus = (e && e.data) ? ("" + e.data).trim() : "";
      if (endStatus === "failed") {
        var t = card.querySelector('a[href^="/jobs/"]');
        var label = ((t && t.textContent) || "A job").replace(/\s+/g, " ").trim();
        showToast(label + " failed. Open History for details.", "error");
      }
      if (surface === "dashboard") {
        if (window.htmx) {
          window.htmx.ajax("GET", "/",
            { target: "#dashboard-active", swap: "outerHTML", select: "#dashboard-active" });
        } else { location.reload(); }
      } else if (window.htmx) {
        window.htmx.ajax("GET", "/queue",
          { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
      } else { location.reload(); }
      if (window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
    });
  }

  function initStreamCards() {
    document.querySelectorAll("[data-job-card]").forEach(wireStreamCard);
  }

  // Single-job progress and review pages.
  function initJobContent() {
    var jc = document.getElementById("job-content");
    if (!jc || jc.dataset.jobWired === "1") return;
    var view = jc.dataset.jobView;
    var id = jc.dataset.jobId;
    if (!view || !id) return;
    jc.dataset.jobWired = "1";
    if (view === "review") wireReview(id);
    else if (view === "progress") wireProgress(id);
  }

  function wireProgress(id) {
    var logEl = document.getElementById("log");
    var card = document.getElementById("progress-card");
    var label = document.getElementById("prog-label");
    var count = document.getElementById("prog-count");
    var bar = document.getElementById("prog-bar");
    var item = document.getElementById("prog-item");
    var activity = document.getElementById("job-activity");
    var foundEl = document.getElementById("scan-found");
    var reconnect = document.getElementById("sse-reconnect");
    var jc = document.getElementById("job-content");
    var foundSingular = (jc && jc.dataset.progressItemSingular) || "album";
    var foundPlural = (jc && jc.dataset.progressItemPlural) || foundSingular + "s";
    // Server-relative elapsed clock for long-running scans.
    var elapsedEl = document.getElementById("scan-elapsed");
    var elapsedTimer = null;
    if (elapsedEl && elapsedEl.dataset.start) {
      var serverStart = parseFloat(elapsedEl.dataset.start);
      var serverNow = parseFloat(elapsedEl.dataset.now);
      var elapsedAtLoad = (isFinite(serverNow) ? serverNow : Date.now() / 1000) - serverStart;
      if (!(elapsedAtLoad >= 0)) elapsedAtLoad = 0;
      var clientBase = Date.now();
      var tickElapsed = function () {
        if (!document.body.contains(elapsedEl)) { if (elapsedTimer) clearInterval(elapsedTimer); return; }
        var secs = Math.max(0, Math.floor(elapsedAtLoad + (Date.now() - clientBase) / 1000));
        var mm = Math.floor(secs / 60), ss = secs % 60;
        elapsedEl.textContent = "· " + mm + ":" + (ss < 10 ? "0" : "") + ss + " elapsed";
      };
      tickElapsed();
      elapsedTimer = setInterval(tickElapsed, 1000);
    }
    var baseTitle = document.title;
    var titleSet = false;
    var foundAlbums = 0, foundArtists = 0;
    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function countLabel(n, one, many) { return n + " " + (n === 1 ? one : many); }
    function showFound() {
      if (!foundEl) return;
      foundEl.textContent = foundAlbums
        ? "Found " + countLabel(foundAlbums, foundSingular, foundPlural) + " across " + plural(foundArtists, "artist") + " so far…"
        : "";
    }
    var waitNote = document.getElementById("queue-wait-note");
    function clearQueuedState() {
      if (waitNote) { waitNote.classList.add("hidden"); waitNote = null; }
      if (activity && activity.textContent === "Queued") activity.textContent = "Scanning";
    }
    var src = new EventSource("/api/jobs/" + id + "/stream");
    var opened = false;
    src.onmessage = function (e) {
      if (!titleSet) { document.title = "▶ " + baseTitle; titleSet = true; }
      clearQueuedState();
      if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
    };
    var reconnectDelay = null;
    src.onopen = function () {
      if (reconnectDelay) { clearTimeout(reconnectDelay); reconnectDelay = null; }
      if (reconnect) reconnect.classList.add("hidden");
      // Reconnect replays recent lines, so reset before appending them again.
      if (opened) {
        if (logEl) logEl.textContent = "";
        foundAlbums = 0;
        foundArtists = 0;
      }
      opened = true;
    };
    src.onerror = function () {
      if (src.readyState === EventSource.CLOSED) {
        if (reconnectDelay) { clearTimeout(reconnectDelay); reconnectDelay = null; }
        if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
        if (reconnect) {
          reconnect.textContent = "Connection lost. Reload to see the latest.";
          reconnect.classList.remove("is-warning");
          reconnect.classList.add("is-error");
          reconnect.classList.remove("hidden");
        }
      } else if (reconnect && !reconnectDelay) {
        // Backgrounding the tab on a phone kills the stream every time; hold
        // the banner back until a reconnect has genuinely failed to land.
        reconnectDelay = setTimeout(function () {
          reconnectDelay = null;
          reconnect.classList.remove("is-error");
          reconnect.classList.add("is-warning");
          reconnect.classList.remove("hidden");
        }, 5000);
      }
    };
    src.addEventListener("progress", function (e) {
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      clearQueuedState();
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (activity && p.phase && activity.textContent !== p.phase) activity.textContent = p.phase;
      if (card) card.classList.remove("hidden");
      if (label) label.textContent = p.phase || (activity && activity.textContent !== "Queued" ? activity.textContent : "Running");
      var ct = p.total > 0 ? p.current + " / " + p.total + unitSuffix(p) : (p.current ? String(p.current) : "");
      if (count) count.textContent = ct;
      if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(p.current / p.total * 100); } else { bar.removeAttribute("value"); } }
      if (item) item.textContent = p.item || "";
      if (typeof p.found === "number" && p.found > foundAlbums) foundAlbums = p.found;
      // Both tallies come cumulative from the server (found_artists rides the
      // reconnect snapshot too), so a dropped-and-reopened stream picks the
      // line back up instead of restarting the artist count at zero.
      if (typeof p.found_artists === "number" && p.found_artists > foundArtists) {
        foundArtists = p.found_artists;
      }
      if (typeof p.found === "number" || p.hit) showFound();
    });
    src.addEventListener("done", function () {
      src.close();
      if (elapsedTimer) clearInterval(elapsedTimer);
      document.title = baseTitle;
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (window.htmx) {
        var jc = document.getElementById("job-content");
        var embedded = jc && jc.dataset.embedded ? "?embedded=1" : "";
        window.htmx.ajax("GET", "/jobs/" + id + "/content" + embedded, { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
  }

  // Paginated, server-backed review. Selection is saved server-side.
  function wireReview(id) {
    var cont = document.getElementById("review-candidates");
    var form = document.getElementById("review-form");
    if (!cont || !form) return;
    var submit = document.getElementById("review-submit");
    var summaryRow = document.getElementById("review-summary-row");
    var summaryCount = document.querySelector("#review-candidates [data-summary-count]");
    var emptyBox = document.getElementById("review-empty");
    var filterRow = document.getElementById("review-filter-row");
    var filterInput = document.getElementById("review-filter");
    var dsTotal = document.querySelector("[data-downsample-total]");
    var dismissRest = document.getElementById("review-dismiss-rest");
    var reviewKind = form.getAttribute("data-review-kind") || "";
    var isDownsampleReview = reviewKind === "downsample";
    var reviewItemSingular = cont.dataset.reviewItemSingular || "album";
    var reviewItemPlural = cont.dataset.reviewItemPlural || "albums";
    var dismissItemSingular = cont.dataset.reviewDismissSingular || "album";
    var dismissItemPlural = cont.dataset.reviewDismissPlural || "albums";

    function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
    function countLabel(n, one, many) { return n + " " + (n === 1 ? one : many); }
    function dismissRestLabel(rest) {
      return (isDownsampleReview ? "Keep hi-res" : "Dismiss unselected") + " (" + rest + ")";
    }
    function artistDismissLabel(picked) {
      if (isDownsampleReview) return "Keep hi-res";
      return "Dismiss unselected";
    }
    function dismissConfirm(rest) {
      if (isDownsampleReview) {
        return "Keep " + countLabel(rest, "unselected album", "unselected albums") + " hi-res? You can downsample later.";
      }
      var other = cont.dataset.reviewOtherTab;
      return "Dismiss " + countLabel(rest, "unselected " + dismissItemSingular, "unselected " + dismissItemPlural) + "?"
        + (other ? " " + other : "") + " You can bring them back from Dismissed.";
    }
    function dismissBusyLabel() { return isDownsampleReview ? "Keeping…" : "Dismissing…"; }
    function dismissToast(count) {
      return isDownsampleReview
        ? "Kept " + plural(count, "album") + " hi-res."
        : "Dismissed " + countLabel(count, dismissItemSingular, dismissItemPlural) + ".";
    }
    function csrf() {
      var m = document.querySelector('meta[name="csrf-token"]');
      return m ? m.content : "";
    }
    function pageBox() { return document.getElementById("review-groups"); }
    function curPage() { var g = pageBox(); return g ? parseInt(g.dataset.page || "1", 10) : 1; }
    function curQuery() { return (filterInput && filterInput.value || "").trim(); }
    function curTab() {
      var b = cont.querySelector("[data-review-tab].is-active");
      return b ? b.getAttribute("data-review-tab") : "";
    }
    // Last server counts payload. Seeded from the initial render's data
    // attributes so tab switches can re-scope the bulk bar without a request.
    var lastCounts = {
      total: parseInt(cont.dataset.reviewTotal || "0", 10),
      selected: parseInt(cont.dataset.reviewSelected || "0", 10),
    };
    if (cont.dataset.reviewMissingTotal !== undefined) {
      lastCounts.missing_total = parseInt(cont.dataset.reviewMissingTotal || "0", 10);
      lastCounts.missing_selected = parseInt(cont.dataset.reviewMissingSelected || "0", 10);
      lastCounts.gap_total = parseInt(cont.dataset.reviewGapTotal || "0", 10);
      lastCounts.gap_selected = parseInt(cont.dataset.reviewGapSelected || "0", 10);
    }
    // The active tab's share of the counts — the bulk bar acts on the active
    // tab only, so it counts the active tab only. Untabbed reviews fall back
    // to the whole set.
    function tabCounts(c) {
      var tab = curTab();
      if (!tab || !c || c.missing_total === undefined) {
        return { total: c ? c.total : 0, selected: c ? c.selected : 0 };
      }
      return tab === "gaps"
        ? { total: c.gap_total, selected: c.gap_selected }
        : { total: c.missing_total, selected: c.missing_selected };
    }

    // Counts come from the server because the DOM only holds one page. The
    // bulk bar (submit + dismiss-unselected) is scoped to the active tab.
    function applyCounts(c) {
      if (!c) return;
      lastCounts = c;
      var tc = tabCounts(c);
      if (submit) {
        submit.disabled = tc.selected === 0;
        submit.textContent = tc.selected
          ? (submit.dataset.reviewVerb || "Download") + " " + tc.selected + " selected"
          : (submit.dataset.emptyLabel || "Select " + reviewItemPlural);
      }
      if (summaryCount) {
        summaryCount.textContent = countLabel(c.total, reviewItemSingular, reviewItemPlural) + " across " + plural(c.artists, "artist");
      }
      if (summaryRow) summaryRow.classList.toggle("hidden", c.total === 0);
      if (emptyBox) emptyBox.classList.toggle("hidden", c.total > 0);
      var tabsNav = cont.querySelector("[data-review-tabs]");
      if (tabsNav) tabsNav.classList.toggle("hidden", c.total === 0);
      if (filterRow && !curQuery()) filterRow.classList.toggle("hidden", c.artists < 4);
      if (dsTotal) {
        dsTotal.textContent = c.reclaimable_label
          ? " · ~" + c.reclaimable_label + " reclaimable" : "";
      }
      cont.dataset.reviewTotal = c.total;
      cont.dataset.reviewSelected = c.selected;
      syncMaster();
      // A library review's tabs carry per-tab totals; keep the tab count
      // chips honest as hides and dismissals shrink the sets.
      if (c.missing_total !== undefined) {
        // Keep the seed attributes current too, or a later re-init reads the
        // counts the page was first rendered with.
        cont.dataset.reviewMissingTotal = c.missing_total;
        cont.dataset.reviewMissingSelected = c.missing_selected;
        cont.dataset.reviewGapTotal = c.gap_total;
        cont.dataset.reviewGapSelected = c.gap_selected;
        var mc = cont.querySelector('[data-tab-count="missing"]');
        var gc = cont.querySelector('[data-tab-count="gaps"]');
        if (mc) mc.textContent = c.missing_total;
        if (gc) gc.textContent = c.gap_total;
      }
      // Only hide/dismiss responses carry hidden_total; a plain tick doesn't
      // change it, so its absence means "leave the link alone".
      if (c.hidden_total !== undefined) {
        var dl = cont.querySelector(".ql-review-dismissed-link");
        if (dl) {
          dl.textContent = (dl.getAttribute("data-hidden-label") || "Dismissed")
            + " (" + c.hidden_total + ")";
        }
      }
      updateDismissRest();
    }

    // What the button will actually take. Under a filter the action is scoped
    // to the rows on screen, so the tab-wide total would name a number it is
    // not going to honour.
    function restCount() {
      if (curQuery()) {
        var box = pageBox();
        var n = box ? parseInt(box.dataset.filteredRest || "0", 10) : 0;
        return isNaN(n) ? 0 : n;
      }
      var tc = tabCounts(lastCounts);
      return tc.total - tc.selected;
    }

    function updateDismissRest() {
      if (!dismissRest) return;
      var rest = restCount();
      dismissRest.classList.toggle("hidden", rest <= 0);
      dismissRest.textContent = dismissRestLabel(rest);
    }

    function syncMaster() {
      var master = cont.querySelector("[data-select-master]");
      if (!master) return;
      var tc = tabCounts(lastCounts);
      master.checked = tc.total > 0 && tc.selected >= tc.total;
      master.indeterminate = tc.selected > 0 && tc.selected < tc.total;
    }

    function post(url, body) {
      return fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf(),
          "X-QL-Origin": TAB_ID,
        },
        body: body,
      });
    }

    function warnPersistFailed(c) {
      if (!c || !c.persist_failed) return;
      showToast("Your choices changed, but couldn't be saved to disk. They may not survive a restart. Check the data folder.", "error");
    }

    // Save one checkbox to the server, then refresh counts from its response.
    function saveTick(cb) {
      var previous = !cb.checked;
      var det = cb.closest("details[data-artist]");
      function revert() {
        cb.checked = previous;
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      }
      // Serialize per-box saves so the server matches the checkbox.
      cb.disabled = true;
      var body = "cid=" + encodeURIComponent(cb.value) + "&checked=" + (cb.checked ? "1" : "0");
      post("/jobs/" + id + "/select", body)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (c) {
          cb.disabled = false;
          if (det) delete det.dataset.selectionOverride;
          if (c) applyCounts(c);
          updateHideLabels();
          updateArtistChecks();
        })
        .catch(function () {
          cb.disabled = false;
          revert();
          showToast("Couldn't save that choice. Try again.", "error");
        });
    }

    // Guard against overlapping whole-set writes.
    var bulkBusy = false;
    function applyGroupChoice(det, on, commit) {
      det.querySelectorAll(".cb").forEach(function (cb) { cb.checked = on; });
      var header = det.querySelector("[data-artist-select]");
      if (header) {
        header.checked = on;
        header.indeterminate = false;
      }
      if (commit) {
        det.dataset.groupSelected = on ? (det.dataset.groupTotal || "0") : "0";
      }
    }

    function restoreGroupHeader(det) {
      var total = parseInt(det.dataset.groupTotal || "0", 10);
      var picked = parseInt(det.dataset.groupSelected || "0", 10);
      var header = det.querySelector("[data-artist-select]");
      if (!header) return;
      header.checked = total > 0 && picked === total;
      header.indeterminate = picked > 0 && picked < total;
    }

    function bulkSelect(on, scope, artist) {
      if (bulkBusy) {
        showToast("Another selection is still saving. Try again in a moment.", "error");
        applyCounts(lastCounts);
        return Promise.resolve({ ok: false, busy: true });
      }
      bulkBusy = true;
      var body = "on=" + (on ? "1" : "0") + "&scope=" + scope +
                 "&tab=" + encodeURIComponent(curTab()) +
                 "&q=" + encodeURIComponent(curQuery());
      if (scope === "artist") body += "&artist=" + encodeURIComponent(artist || "");
      if (scope === "page") {
        pageBox() && pageBox().querySelectorAll("details[data-artist]").forEach(function (det) {
          body += "&artist=" + encodeURIComponent(det.dataset.artist || "");
        });
      }
      return post("/jobs/" + id + "/select-all", body)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (c) {
          bulkBusy = false;
          applyCounts(c);
          warnPersistFailed(c);
          if (scope === "all" || scope === "page") {
            pageBox() && pageBox().querySelectorAll("details[data-artist]").forEach(function (det) {
              det.dataset.selectionOverride = on ? "1" : "0";
              applyGroupChoice(det, on, true);
            });
          }
          updateHideLabels();
          updateArtistChecks();
          return { ok: true, counts: c };
        })
        .catch(function () {
          bulkBusy = false;
          flashSelectError();
          showToast("Couldn't confirm those choices. Reloading the review.", "error");
          setTimeout(function () { location.reload(); }, 900);
          return { ok: false, busy: false };
        });
    }

    function flashSelectError() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll(".cb").forEach(function (cb) {
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      });
    }

    function groupSelect(det, on) {
      if (det._selecting) return;
      var previousRows = Array.prototype.map.call(
        det.querySelectorAll(".cb"), function (cb) { return cb.checked; });
      det._selecting = true;
      var header = det.querySelector("[data-artist-select]");
      if (header) header.disabled = true;
      det.dataset.selectionOverride = on ? "1" : "0";
      applyGroupChoice(det, on, false);
      bulkSelect(on, "artist", det.dataset.artist || "").then(function (result) {
        det._selecting = false;
        if (header) header.disabled = false;
        if (result.ok) {
          applyGroupChoice(det, on, true);
        } else {
          delete det.dataset.selectionOverride;
          det.dataset.selectionRecovering = "1";
          det.querySelectorAll(".cb").forEach(function (cb, index) {
            if (index < previousRows.length) cb.checked = previousRows[index];
          });
          restoreGroupHeader(det);
          if (!result.busy) {
            loadGroupItems(det, true).then(function (loaded) {
              if (loaded) delete det.dataset.selectionRecovering;
              updateHideLabels();
              updateArtistChecks();
            });
          } else {
            delete det.dataset.selectionRecovering;
          }
        }
        updateHideLabels();
        updateArtistChecks();
      });
    }

    // Label each artist action for what it will drop or keep.
    function updateHideLabels() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll("[data-hide]").forEach(function (btn) {
        var shell = btn.closest(".ql-review-group-shell");
        var det = shell ? shell.querySelector("details[data-artist]") : btn.closest("details");
        var lbl = btn.querySelector("[data-hide-label]");
        if (!det || !lbl) return;
        var lazy = det.querySelector("[data-lazy-items]");
        var useSavedCounts = det.dataset.selectionRecovering !== undefined
          || (lazy && !lazy.dataset.loaded);
        var total = useSavedCounts
          ? parseInt(det.dataset.groupTotal || "0", 10)
          : det.querySelectorAll(".cb").length;
        var picked = useSavedCounts
          ? parseInt(det.dataset.groupSelected || "0", 10)
          : det.querySelectorAll(".cb:checked").length;
        btn.classList.toggle("hidden", total > 0 && picked === total);
        lbl.textContent = artistDismissLabel(picked);
      });
    }

    // Keep artist checkboxes in sync with their albums.
    function updateArtistChecks() {
      var box = pageBox();
      if (!box) return;
      box.querySelectorAll("[data-artist-select]").forEach(function (cb) {
        var det = cb.closest("details");
        if (!det) return;
        var lazy = det.querySelector("[data-lazy-items]");
        var useSavedCounts = det.dataset.selectionRecovering !== undefined
          || (lazy && !lazy.dataset.loaded);
        var total = useSavedCounts
          ? parseInt(det.dataset.groupTotal || "0", 10)
          : det.querySelectorAll(".cb").length;
        var picked = useSavedCounts
          ? parseInt(det.dataset.groupSelected || "0", 10)
          : det.querySelectorAll(".cb:checked").length;
        cb.checked = total > 0 && picked === total;
        cb.indeterminate = picked > 0 && picked < total;
        if (!useSavedCounts && !det._selecting) {
          det.dataset.groupTotal = String(total);
          det.dataset.groupSelected = String(picked);
        }
      });
    }

    function loadGroupItems(det, force) {
      var boxEl = det.querySelector("[data-lazy-items]");
      if (!boxEl || (boxEl.dataset.loaded && !force)) return Promise.resolve(true);
      var generation = (boxEl._loadGeneration || 0) + 1;
      boxEl._loadGeneration = generation;
      boxEl.dataset.loading = "1";
      return fetch(boxEl.dataset.itemsUrl, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (txt) {
          if (boxEl._loadGeneration !== generation) return false;
          delete boxEl.dataset.loading;
          boxEl.innerHTML = txt;
          boxEl.dataset.loaded = "1";
          if (det.dataset.selectionOverride !== undefined) {
            applyGroupChoice(det, det.dataset.selectionOverride === "1", false);
          }
          updateHideLabels();
          updateArtistChecks();
          return true;
        })
        .catch(function () {
          if (boxEl._loadGeneration !== generation) return false;
          delete boxEl.dataset.loading;
          showToast("Couldn't load this artist's albums. Try opening it again.", "error");
          return false;
        });
    }

    // Append the next page in place.
    function loadMore(page) {
      if (loading) return;
      loading = true;
      var gen = ++loadGen;
      var url = "/jobs/" + id + "/review?page=" + (page || 2) +
                "&q=" + encodeURIComponent(curQuery()) +
                "&tab=" + encodeURIComponent(curTab());
      fetch(url, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          loading = false;
          if (gen !== loadGen) return;
          if (txt == null) { showToast("Couldn't load those results. Try again.", "error"); return; }
          var tmp = document.createElement("div");
          tmp.innerHTML = txt;
          var liveGroups = document.getElementById("review-groups");
          var newGroups = tmp.querySelector("#review-groups");
          if (liveGroups && newGroups) {
            while (newGroups.firstElementChild) liveGroups.appendChild(newGroups.firstElementChild);
            liveGroups.dataset.page = newGroups.dataset.page || liveGroups.dataset.page;
          }
          var oldMore = document.getElementById("review-loadmore");
          if (oldMore) oldMore.remove();
          var newMore = tmp.querySelector("#review-loadmore");
          if (newMore && liveGroups) liveGroups.parentNode.appendChild(newMore);
          if (window.htmx) window.htmx.process(document.getElementById("review-page"));
          updateHideLabels();
          updateArtistChecks();
        })
        .catch(function () { loading = false; if (gen === loadGen) showToast("Couldn't load those results. Check your connection.", "error"); });
    }

    // Fetch and swap one review page. Requests are generation-tagged and only
    // the newest response may render: a tab click while a fetch is in flight
    // issues its own request instead of being dropped, and the slow old-tab
    // response is discarded on arrival — otherwise its rows would paint under
    // the newly selected tab while the hidden approval field already points
    // at the new tab.
    var loading = false;
    var loadGen = 0;
    function loadPage(page, query) {
      var gen = ++loadGen;
      loading = false;   // a page swap supersedes any in-flight append
      // The outgoing page's Load More dies NOW, not when the response lands:
      // a click on it (or the auto-clicking observer) in that gap would bump
      // the generation, discard this page-1 response, and append the new
      // tab's page 2 beneath the old tab's rows. The swap brings its own
      // control back; on a failed fetch the tab click re-issues the load.
      var staleMore = document.getElementById("review-loadmore");
      if (staleMore) staleMore.remove();
      var url = "/jobs/" + id + "/review?page=" + (page || 1) +
                "&q=" + encodeURIComponent(query || "") +
                "&tab=" + encodeURIComponent(curTab());
      fetch(url, { headers: { "HX-Request": "true" } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          if (gen !== loadGen) return;
          if (txt == null) { showToast("Couldn't load those results. Try again.", "error"); return; }
          var host = document.getElementById("review-page");
          if (host) {
            var openArtists = {};
            host.querySelectorAll("details[data-artist][open]").forEach(function (d) {
              if (d.dataset.artist) openArtists[d.dataset.artist] = true;
            });
            host.innerHTML = txt;
            host.querySelectorAll("details[data-artist]").forEach(function (d) {
              if (d.dataset.artist && openArtists[d.dataset.artist]) d.open = true;
            });
            if (window.htmx) window.htmx.process(host);
            updateHideLabels();
            updateArtistChecks();
            updateDismissRest();
          }
        })
        .catch(function () { if (gen === loadGen) showToast("Couldn't load those results. Check your connection.", "error"); });
    }

    // Delegated interactions keep swapped pages wired.
    cont.addEventListener("change", function (e) {
      if (e.target.classList && e.target.classList.contains("cb")) saveTick(e.target);
      var asel = e.target.closest("[data-artist-select]");
      if (asel) { var ad = asel.closest("details"); if (ad) groupSelect(ad, asel.checked); }
    });
    // Closed artist groups leave their rows out of the DOM. Fetch the current
    // server state on first open; generation tags keep a retry from being
    // overwritten by an older response still in flight.
    cont.addEventListener("toggle", function (e) {
      var det = e.target;
      if (!det || !det.matches || !det.matches("details[data-artist]") || !det.open) return;
      loadGroupItems(det, false);
    }, true);
    cont.addEventListener("click", function (e) {
      var t = e.target;
      if (t.closest("[data-hide]")) return;
      // The artist checkbox sits inside a <summary>: its own activation runs
      // instead of the summary's, but stop the bubble so nothing else fires.
      if (t.closest("[data-artist-select]")) { e.stopPropagation(); return; }
      var allBtn = t.closest("[data-select-all]");
      if (allBtn) { bulkSelect(allBtn.getAttribute("data-select-all") === "1", "all"); return; }
      var pageBtn = t.closest("[data-select-page]");
      if (pageBtn) { bulkSelect(true, "page"); return; }
      var more = t.closest("[data-load-more]");
      if (more) { loadMore(parseInt(more.getAttribute("data-next-page") || "2", 10)); return; }
      var tabBtn = t.closest("[data-review-tab]");
      if (tabBtn && !tabBtn.classList.contains("is-active")) {
        cont.querySelectorAll("[data-review-tab]").forEach(function (b) {
          var on2 = b === tabBtn;
          b.classList.toggle("is-active", on2);
          if (on2) b.setAttribute("aria-current", "true");
          else b.removeAttribute("aria-current");
        });
        var tabField = document.getElementById("review-tab-field");
        if (tabField) tabField.value = curTab();
        applyCounts(lastCounts);  // re-scope the bulk bar to the new tab
        loadPage(1, curQuery());
        return;
      }
      var expBtn = t.closest("[data-expand-all]");
      if (expBtn) {
        var openAll = expBtn.getAttribute("data-expand-all") === "1";
        var box = pageBox();
        if (box) {
          box.querySelectorAll("details[data-artist]").forEach(function (d) { d.open = openAll; });
        }
        // The one control flips between the two actions.
        expBtn.setAttribute("data-expand-all", openAll ? "0" : "1");
        expBtn.textContent = openAll ? "Collapse all" : "Expand all";
        return;
      }
    });
    cont.addEventListener("change", function (evt) {
      var master = evt.target.closest && evt.target.closest("[data-select-master]");
      if (!master) return;
      var btn = cont.querySelector('[data-select-all="' + (master.checked ? "1" : "0") + '"]');
      if (btn) btn.click();
    });
    if (dismissRest) {
      dismissRest.addEventListener("click", function () {
        var rest = restCount();
        if (rest <= 0) return;
        // The number is the filtered one when a filter is on, so the question
        // and the button agree with what will actually happen.
        var confirmMsg = curQuery()
          ? (isDownsampleReview
              ? "Keep the " + countLabel(rest, "unselected album", "unselected albums") + " matching your filter hi-res? You can downsample them later."
              : "Dismiss the " + countLabel(rest, "unselected " + dismissItemSingular, "unselected " + dismissItemPlural) + " matching your filter? You can bring them back from Dismissed.")
          : dismissConfirm(rest);
        window.qlConfirm(confirmMsg, {
          action: isDownsampleReview ? "Keep hi-res" : "Dismiss",
        }).then(function (ok) {
          if (!ok) return;
          var prev = dismissRest.textContent;
          dismissRest.disabled = true;
          dismissRest.textContent = dismissBusyLabel();
          post("/jobs/" + id + "/dismiss-rest",
               "tab=" + encodeURIComponent(curTab()) +
               "&q=" + encodeURIComponent(curQuery()))
            .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
            .then(function (c) {
              if (c.review_done) { location.reload(); return; }
              dismissRest.disabled = false;
              applyCounts(c);
              loadPage(1, curQuery());
              showToast(c.hidden ? dismissToast(c.hidden)
                                 : "Nothing to dismiss on this filter.",
                        c.hidden ? "success" : "info");
            })
            .catch(function () {
              dismissRest.disabled = false;
              dismissRest.textContent = prev;
              flashSelectError();
            });
        });
      });
    }

    // Filter across the full server-side result set.
    var filterTimer = null;
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        if (filterTimer) clearTimeout(filterTimer);
        filterTimer = setTimeout(function () { loadPage(1, curQuery()); }, 250);
      });
      // A search input's Escape-clear and its native ✕ change the value
      // without firing `input`, which left the box empty and the list filtered.
      filterInput.addEventListener("search", function () {
        if (filterTimer) clearTimeout(filterTimer);
        loadPage(1, curQuery());
      });
      // Enter should filter, not submit the review form.
      filterInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
      });
    }

    // Refresh counts and reload a page if hiding empties it.
    function onQlHidden(e) {
      var d = e.detail || {};
      if (d.counts) applyCounts(d.counts);
      var box = pageBox();
      if (box && box.querySelectorAll(":scope > .ql-review-group-shell, :scope > details").length === 0) {
        var p = curPage();
        loadPage(p > 1 ? p - 1 : 1, curQuery());
      } else {
        updateHideLabels();
      }
    }
    document.body.addEventListener("qlHidden", onQlHidden);

    updateHideLabels();
    updateArtistChecks();
    syncMaster();

    // Keep review pages in sync across tabs.
    var rsrc = new EventSource("/api/jobs/" + id + "/review-stream");
    function shutReview() {
      try { rsrc.close(); } catch (e) {}
      document.body.removeEventListener("qlHidden", onQlHidden);
      document.removeEventListener("htmx:beforeSwap", onReviewSwap);
    }
    function onReviewSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === "job-content") shutReview();
    }
    document.addEventListener("htmx:beforeSwap", onReviewSwap);
    rsrc.addEventListener("review", function (e) {
      // Our own change echoing back — the DOM and counts are already current
      // from the action's response, and reloading now would swap the page out
      // from under the user's next click.
      if ((e.data || "") === TAB_ID) return;
      loadPage(curPage(), curQuery());
      post("/jobs/" + id + "/select", "cid=&checked=0")  // no-op tick → returns counts
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (c) { if (c) applyCounts(c); });
    });
    rsrc.addEventListener("closed", function (e) {
      // An archived/restored review has no live producer, so the server ends
      // the stream with "inactive" right away. Hides and ticks still work
      // there, so keep the count listeners — only stop the dead stream.
      if ((e.data || "") === "inactive") {
        try { rsrc.close(); } catch (err) {}
        return;
      }
      shutReview();
      if (window.htmx) {
        var jc = document.getElementById("job-content");
        var embedded = jc && jc.dataset.embedded ? "?embedded=1" : "";
        window.htmx.ajax("GET", "/jobs/" + id + "/content" + embedded, { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
    rsrc.onerror = function () {
      if (rsrc.readyState === EventSource.CLOSED) shutReview();
    };
  }

  function initAll() {
    autoDismissFlashes();
    initCoverFallbacks();
    initSearchResults();
    initStreamCards();
    initJobContent();
  }

  // Re-scan swapped content for flashes and streams.
  document.addEventListener("htmx:afterSwap", initAll);
  cleanFlashUrl();
  // "Load more artists" clicks itself as it approaches the viewport, so a
  // long review reads as one continuous scrolling list. Deferred to DOM-ready:
  // this script loads in <head>, where document.body doesn't exist yet.
  (function () {
    if (!("IntersectionObserver" in window)) return;
    function start() {
      var io = new IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].isIntersecting) entries[i].target.click();
        }
      }, { rootMargin: "700px 0px" });
      var seen = null;
      function arm() {
        var btn = document.querySelector("[data-load-more]");
        if (btn === seen) return;
        if (seen) io.unobserve(seen);
        seen = btn;
        if (btn) io.observe(btn);
      }
      arm();
      new MutationObserver(arm).observe(document.body, { childList: true, subtree: true });
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  })();

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
