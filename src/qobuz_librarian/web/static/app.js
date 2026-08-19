// Behaviour is delegated from here so CSP can stay strict and htmx-swapped
// fragments keep working without inline handlers.

(function () {
  var REDUCE = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Identify this browser tab so its own review updates do not reload it.
  var TAB_ID = Math.random().toString(36).slice(2, 10)
    + Date.now().toString(36);
  window.qlTabId = TAB_ID;

  var NAV_STATE_PREFIX = "ql-nav-state:";
  var NAV_RESTORE_KEY = "ql-nav-restore";
  var pendingSearchRestoreY = null;
  var SEARCH_STATE_PREFIX = "ql-search-state:";
  var SEARCH_SNAPSHOT_PREFIX = "ql-search-snapshot:";
  var SEARCH_STATE_TTL = 30 * 60 * 1000;
  var SEARCH_SNAPSHOT_MAX_CHARS = 750000;
  var SEARCH_ASSET_VERSION = document.documentElement.dataset.assetVersion || "";
  var searchPositionRestoring = false;
  var searchRestoreFallback = null;
  var searchSnapshotsInvalidated = false;

  function searchStateId(kind, query, artistId) {
    kind = ["artist", "album", "track"].indexOf(kind) >= 0 ? kind : "artist";
    var state = kind + "|" + String(query || "").trim();
    return artistId ? state + "|artist:" + String(artistId).trim() : state;
  }

  function searchStateFromUrl() {
    if (location.pathname !== "/") return "";
    try {
      var params = new URL(location.href).searchParams;
      var query = (params.get("q") || "").trim();
      if (!query) return "";
      return searchStateId(
        params.get("kind") || "artist",
        query,
        params.get("artist_id") || ""
      );
    } catch (e) {
      return "";
    }
  }

  function searchStorageKey(state) {
    return SEARCH_STATE_PREFIX + state;
  }

  function searchSnapshotStorageKey(state) {
    return SEARCH_SNAPSHOT_PREFIX + state;
  }

  function validSearchRecord(record) {
    return record && record.assetVersion === SEARCH_ASSET_VERSION
      && typeof record.savedAt === "number"
      && Date.now() - record.savedAt <= SEARCH_STATE_TTL;
  }

  function readSearchRecord(state) {
    if (!state) return null;
    try {
      var record = JSON.parse(sessionStorage.getItem(searchStorageKey(state)) || "null");
      if (!validSearchRecord(record)) {
        sessionStorage.removeItem(searchStorageKey(state));
        return null;
      }
      return record;
    } catch (e) {
      return null;
    }
  }

  function readSearchSnapshot(state) {
    if (!state) return "";
    var key = searchSnapshotStorageKey(state);
    try {
      var record = JSON.parse(sessionStorage.getItem(key) || "null");
      if (!validSearchRecord(record) || typeof record.html !== "string") {
        sessionStorage.removeItem(key);
        return "";
      }
      return record.html;
    } catch (e) {
      try { sessionStorage.removeItem(key); } catch (error) {}
      return "";
    }
  }

  function clearSearchSnapshots() {
    try {
      for (var i = sessionStorage.length - 1; i >= 0; i--) {
        var key = sessionStorage.key(i);
        if (key && key.indexOf(SEARCH_SNAPSHOT_PREFIX) === 0) {
          sessionStorage.removeItem(key);
        }
      }
    } catch (e) {}
  }

  function removeSearchSnapshot(state) {
    if (!state) return;
    try { sessionStorage.removeItem(searchSnapshotStorageKey(state)); } catch (e) {}
  }

  function writeSearchSnapshot(state, html) {
    if (!state || !html || html.length > SEARCH_SNAPSHOT_MAX_CHARS) {
      removeSearchSnapshot(state);
      return false;
    }
    clearSearchSnapshots();
    try {
      sessionStorage.setItem(searchSnapshotStorageKey(state), JSON.stringify({
        html: html,
        assetVersion: SEARCH_ASSET_VERSION,
        savedAt: Date.now(),
      }));
      return true;
    } catch (e) {
      removeSearchSnapshot(state);
      return false;
    }
  }

  function updateSearchRecord(state, values, preserveSavedAt) {
    if (!state) return null;
    var record = readSearchRecord(state);
    if (preserveSavedAt && !record) return null;
    record = record || {};
    var savedAt = record.savedAt;
    Object.keys(values || {}).forEach(function (key) {
      record[key] = values[key];
    });
    record.assetVersion = SEARCH_ASSET_VERSION;
    record.savedAt = preserveSavedAt ? savedAt : Date.now();
    var serialized = JSON.stringify(record);
    try {
      sessionStorage.setItem(searchStorageKey(state), serialized);
      return record;
    } catch (e) {
      clearSearchSnapshots();
      try {
        sessionStorage.setItem(searchStorageKey(state), serialized);
        return record;
      } catch (error) {
        return null;
      }
    }
  }

  function invalidateSearchSnapshots() {
    searchSnapshotsInvalidated = true;
    clearSearchSnapshots();
    var now = Date.now();
    try {
      for (var i = sessionStorage.length - 1; i >= 0; i--) {
        var key = sessionStorage.key(i);
        if (!key || key.indexOf(SEARCH_STATE_PREFIX) !== 0) continue;
        try {
          var record = JSON.parse(sessionStorage.getItem(key) || "null");
          if (!record || record.assetVersion !== SEARCH_ASSET_VERSION
              || typeof record.savedAt !== "number"
              || now - record.savedAt > SEARCH_STATE_TTL) {
            sessionStorage.removeItem(key);
          }
        } catch (e) {
          sessionStorage.removeItem(key);
        }
      }
    } catch (e) {}
    if (window.qlRefreshSearchAvailability) {
      window.qlRefreshSearchAvailability();
    }
  }
  window.qlInvalidateSearchSnapshots = invalidateSearchSnapshots;

  var bootSearchState = searchStateFromUrl();
  var bootSearchRecord = readSearchRecord(bootSearchState);
  var bootSearchHtml = readSearchSnapshot(bootSearchState);
  if (bootSearchHtml) {
    searchPositionRestoring = true;
    document.documentElement.classList.add("ql-search-restoring");
    searchRestoreFallback = setTimeout(function () {
      document.documentElement.classList.remove("ql-search-restoring");
      searchPositionRestoring = false;
    }, 2000);
  }
  if (bootSearchState && "scrollRestoration" in history) {
    history.scrollRestoration = "manual";
  }
  var NAV_DEFAULTS = {
    search: "/",
    library: "/library",
    upgrade: "/upgrade",
    downsample: "/downsample",
    repair: "/repair",
    lyrics: "/lyrics",
    queue: "/queue",
    settings: "/settings",
  };

  function activeNavTab() {
    return document.body && NAV_DEFAULTS[document.body.dataset.navTab]
      ? document.body.dataset.navTab : "";
  }

  function currentNavUrl() {
    return location.pathname + location.search + location.hash;
  }

  function pathBelongsToTab(tab, path, owner) {
    if (/^\/jobs\/[^/]+\/?$/.test(path)) return owner === tab;
    if (tab === "search") return path === "/";
    if (tab === "library") return /^\/library(?:\/hidden)?\/?$/.test(path);
    if (tab === "repair") return /^\/repair(?:\/history)?\/?$/.test(path);
    if (tab === "lyrics") return path === "/lyrics" || path === "/lyrics/";
    if (tab === "settings") return /^\/(?:settings|migrate)\/?$/.test(path);
    if (tab === "queue") {
      return /^\/queue(?:\/history)?\/?$/.test(path);
    }
    if (tab === "upgrade" || tab === "downsample") {
      return new RegExp("^/" + tab + "(?:/hidden)?/?$").test(path);
    }
    return false;
  }

  function readNavState(tab) {
    if (!NAV_DEFAULTS[tab]) return null;
    try {
      var state = JSON.parse(sessionStorage.getItem(NAV_STATE_PREFIX + tab) || "null");
      var parsed = state && new URL(state.url, location.origin);
      if (!parsed || parsed.origin !== location.origin
          || !pathBelongsToTab(tab, parsed.pathname, state.owner)
          || typeof state.y !== "number" || !isFinite(state.y)
          || state.y < 0 || state.y > 10000000) return null;
      return {
        url: parsed.pathname + parsed.search + parsed.hash,
        y: Math.round(state.y),
        owner: state.owner || "",
      };
    } catch (e) {
      return null;
    }
  }

  function writeCurrentNavState() {
    var tab = activeNavTab();
    if (!tab || !pathBelongsToTab(tab, location.pathname, tab)) return;
    var url = currentNavUrl();
    var y = Math.max(0, Math.round(window.scrollY || 0));
    var job = /^\/jobs\/[^/]+\/?$/.test(location.pathname)
      ? document.getElementById("job-content") : null;
    if (job && !job.dataset.jobView && job.dataset.navReturn) {
      url = job.dataset.navReturn;
      y = 0;
    }
    try {
      sessionStorage.setItem(NAV_STATE_PREFIX + tab, JSON.stringify({
        url: url,
        y: y,
        owner: tab,
      }));
    } catch (e) {}
  }

  function setNavRestore(tab, state) {
    try {
      if (state) {
        sessionStorage.setItem(NAV_RESTORE_KEY, JSON.stringify({
          tab: tab,
          url: state.url,
          y: state.y,
        }));
      } else {
        sessionStorage.removeItem(NAV_RESTORE_KEY);
      }
    } catch (e) {}
  }

  function rememberedNavUrl(tab) {
    var state = readNavState(tab);
    return state ? state.url : NAV_DEFAULTS[tab];
  }

  function beginNavSwitch(tab, restorePosition) {
    writeCurrentNavState();
    var state = readNavState(tab);
    setNavRestore(tab, restorePosition !== false ? state : null);
    return state;
  }

  function navigateToTab(tab, options) {
    options = options || {};
    if (!NAV_DEFAULTS[tab]) return;
    var url = rememberedNavUrl(tab);
    beginNavSwitch(tab, options.restorePosition);
    if (options.hash) {
      var parsed = new URL(url, location.origin);
      parsed.hash = options.hash;
      url = parsed.pathname + parsed.search + parsed.hash;
    }
    location.href = url;
  }

  function restoreNavPosition(y) {
    if (!(y > 0) || document.getElementById("review-form")) return;
    requestAnimationFrame(function () {
      if (document.getElementById("review-form")) return;
      var height = Math.max(
        document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0
      );
      var limit = Math.max(0, height - window.innerHeight);
      window.scrollTo(0, Math.min(y, limit));
    });
  }

  function initNavState() {
    var active = activeNavTab();
    var links = document.querySelectorAll("[data-nav-tab-link]");
    if (!active && !links.length) return;
    var current = currentNavUrl();
    var saved = active ? readNavState(active) : null;
    var restore = null;
    try {
      restore = JSON.parse(sessionStorage.getItem(NAV_RESTORE_KEY) || "null");
      sessionStorage.removeItem(NAV_RESTORE_KEY);
    } catch (e) {}

    if (active && restore && NAV_DEFAULTS[restore.tab]
        && restore.tab !== active && restore.url === current) {
      try { sessionStorage.removeItem(NAV_STATE_PREFIX + restore.tab); } catch (e) {}
      location.replace(NAV_DEFAULTS[restore.tab]);
      return;
    }
    var restoredJob = /^\/jobs\/[^/]+\/?$/.test(location.pathname)
      ? document.getElementById("job-content") : null;
    if (active && restore && restore.tab === active && restore.url === current
        && restoredJob && !restoredJob.dataset.jobView
        && restoredJob.dataset.navReturn) {
      writeCurrentNavState();
      location.replace(restoredJob.dataset.navReturn);
      return;
    }
    if (active && (!saved || saved.url !== current)) writeCurrentNavState();
    links.forEach(function (link) {
      var tab = link.dataset.navTabLink;
      if (tab === active) return;
      link.setAttribute("href", rememberedNavUrl(tab));
    });
    if (active && restore && restore.tab === active && restore.url === current
        && typeof restore.y === "number") {
      if (active === "search") pendingSearchRestoreY = restore.y;
      else restoreNavPosition(restore.y);
    }
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("[data-nav-tab-link]");
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey
        || event.shiftKey || event.altKey || link.target === "_blank") return;
    var tab = link.dataset.navTabLink;
    if (!NAV_DEFAULTS[tab] || tab === activeNavTab()) return;
    var state = beginNavSwitch(tab, true);
    link.setAttribute("href", state ? state.url : NAV_DEFAULTS[tab]);
  }, true);
  window.addEventListener("pagehide", writeCurrentNavState);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) initNavState();
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initNavState);
  } else {
    initNavState();
  }

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

  function setSearchItemAvailability(item, state) {
    if (!item || item.dataset.owned === "1") return;
    var unavailable = state !== "available";
    item.dataset.queued = state === "queued" ? "1" : "0";
    item.dataset.scanning = state === "scanning" ? "1" : "0";

    var checkbox = item.querySelector("[data-search-select]");
    if (checkbox) {
      checkbox.disabled = unavailable;
      if (unavailable) checkbox.checked = false;
    }

    var form = item.querySelector("[data-search-download-form]");
    var button = form && form.querySelector("button[type=submit]");
    if (!button) return;
    var icon = button.classList.contains("ql-download-icon-button");
    var title = form.dataset.searchTitle || "";
    var artist = form.dataset.searchArtist || "";
    var description = title + (artist ? " by " + artist : "");
    var paused = form.dataset.searchWritesPaused === "1";
    var pending = form.classList.contains("htmx-request");

    button.classList.remove("is-queued", "is-scanning", "is-disabled");
    if (state === "queued") button.classList.add("is-queued", "is-disabled");
    if (state === "scanning") button.classList.add("is-scanning", "is-disabled");
    button.disabled = unavailable || paused || pending;
    if (button.disabled) button.setAttribute("aria-disabled", "true");
    else button.removeAttribute("aria-disabled");

    if (!icon) {
      button.classList.toggle("ql-btn-primary", !unavailable);
      button.classList.toggle("ql-btn-secondary", unavailable);
      button.textContent = state === "queued" ? "Queued"
        : state === "scanning" ? "In current scan" : "Download";
    }
    if (state === "queued") {
      button.setAttribute("aria-label", "Queued: " + description);
      button.setAttribute("title", "Queued");
    } else if (state === "scanning") {
      button.setAttribute("aria-label", "In current scan: " + description);
      button.setAttribute("title", "In current scan");
    } else {
      button.setAttribute("aria-label", "Download " + description);
      button.setAttribute("title", paused
        ? form.dataset.searchWritesPausedReason || "Downloads are paused"
        : "Download");
    }
  }

  function searchResultItems(root) {
    var items = Array.prototype.slice.call(root.querySelectorAll("[data-search-item]"));
    root.querySelectorAll("template[data-search-view-template]").forEach(function (template) {
      items = items.concat(Array.prototype.slice.call(
        template.content.querySelectorAll("[data-search-item]")
      ));
    });
    return items;
  }

  function markSearchDownloadQueued(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var root = item && item.closest("[data-search-results-root]");
    var peers = item ? [item] : [];
    if (root && key) {
      peers = searchResultItems(root).filter(function (peer) {
        return peer.dataset.searchKey === key;
      });
    }
    peers.forEach(function (peer) {
      setSearchItemAvailability(peer, "queued");
    });
    if (root) root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
  }

  var searchAvailabilityRequest = null;
  var refreshSearchAvailabilityAgain = false;

  function refreshSearchAvailability() {
    if (!document.querySelector("[data-search-results-root]")) return;
    if (searchAvailabilityRequest) {
      refreshSearchAvailabilityAgain = true;
      return;
    }
    var request = sessionFetch("/api/search/availability", {
      headers: { "Accept": "application/json" },
    });
    searchAvailabilityRequest = request;
    request
      .then(function (response) { return response.ok ? response.json() : Promise.reject(); })
      .then(function (data) {
        var queued = new Set(data.queued || []);
        var scanning = new Set(data.scanning || []);
        // A download that has finished putting the whole album on disk owns it
        // now; without this the row would fall back to offering that same
        // download again the moment its job left the queue.
        var owned = new Set(data.owned || []);
        document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
          searchResultItems(root).forEach(function (item) {
            var key = item.dataset.searchKey || "";
            if (owned.has(key)) { markSearchItemOwned(item); return; }
            setSearchItemAvailability(item,
              queued.has(key) ? "queued" : scanning.has(key) ? "scanning" : "available");
          });
          root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
        });
        if (!refreshSearchAvailabilityAgain) {
          searchSnapshotsInvalidated = false;
        }
      })
      .catch(function () {})
      .then(function () {
        if (searchAvailabilityRequest === request) searchAvailabilityRequest = null;
        if (refreshSearchAvailabilityAgain) {
          refreshSearchAvailabilityAgain = false;
          refreshSearchAvailability();
        }
      });
  }
  window.qlRefreshSearchAvailability = refreshSearchAvailability;

  function markSearchItemOwned(item) {
    if (!item || item.dataset.owned === "1") return;
    item.dataset.owned = "1";
    // Owned replaces every other state; leaving queued set would let a filter
    // count the same row as both waiting and already yours.
    item.dataset.queued = "0";
    item.dataset.scanning = "0";
    item.classList.add("is-owned");
    var checkbox = item.querySelector("[data-search-select]");
    if (checkbox) {
      checkbox.checked = false;
      var label = checkbox.closest("label");
      if (label) label.remove();
      else checkbox.remove();
    }
    var download = item.querySelector("[data-search-download-form]");
    if (download) {
      var owned = document.createElement("span");
      owned.className = "ql-owned-label";
      owned.textContent = "In library";
      download.replaceWith(owned);
    }
  }

  function markSearchDownloadOwned(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var root = item && item.closest("[data-search-results-root]");
    var peers = item ? [item] : [];
    if (root && key) {
      peers = searchResultItems(root).filter(function (peer) {
        return peer.dataset.searchKey === key;
      });
    }
    peers.forEach(markSearchItemOwned);
    if (root) root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
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

  // Requests the page makes on its own, on a timer. They report nothing to the
  // user when they fail: the status line they feed simply stops moving, where a
  // red toast every few seconds would blame the user for something they never
  // did and bury the flash that matters.
  function fromBackgroundPoll(evt) {
    var elt = evt.detail && evt.detail.elt;
    var trigger = elt && elt.getAttribute && elt.getAttribute("hx-trigger");
    return !!trigger && /(^|[\s,])every\s/.test(trigger);
  }

  // Surface failed htmx requests instead of failing silently. A short
  // plain-text body is the route speaking (e.g. "couldn't save that
  // dismissal"); anything longer or HTML-shaped gets the generic line.
  document.addEventListener("htmx:responseError", function (evt) {
    if (fromBackgroundPoll(evt)) return;
    var xhr = evt.detail && evt.detail.xhr;
    var body = (xhr && xhr.responseText || "").trim();
    var msg = (body && body.length <= 200 && body.indexOf("<") === -1)
      ? body : "That didn't go through. Try again in a moment.";
    showToast(msg, "error");
  });
  document.addEventListener("htmx:sendError", function (evt) {
    if (fromBackgroundPoll(evt)) return;
    showToast("Couldn't reach the server. Check your connection and try again.", "error");
  });

  // Animate a fully hidden artist group before htmx removes it. Dismissals
  // target the group's positioning SHELL (the dismiss button lives beside the
  // <details>, not inside it), so match the shell as well as a bare details;
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

  // Global search, escape, and g-then-key navigation shortcuts.
  var gPending = false;
  var gTimer = null;
  document.addEventListener("keydown", function (evt) {
    if (evt.ctrlKey || evt.metaKey || evt.altKey) return;
    var t = evt.target;
    var inField = !!(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                           t.tagName === "SELECT" || t.isContentEditable));
    if (evt.key === "Escape") {
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (window.qlCloseDropdowns) window.qlCloseDropdowns();
      // A search box has already used Escape to clear itself; blurring on top
      // of that costs a tap to get back in and carry on typing.
      var isSearchBox = !!(t && t.tagName === "INPUT" && t.type === "search");
      if (inField && !isSearchBox && t.blur) t.blur();
      return;
    }
    if (inField) return;
    if (evt.key === "/") {
      evt.preventDefault();
      var box = document.querySelector('input[name="q"]');
      if (!box) {
        navigateToTab("search", { restorePosition: false, hash: "search" });
        return;
      }
      box.focus();
      box.select();
      return;
    }
    if (gPending) {
      var map = { s: "settings", q: "queue", h: "search" };
      gPending = false;
      if (map[evt.key]) {
        evt.preventDefault();
        navigateToTab(map[evt.key]);
      }
      return;
    }
    if (evt.key === "g") {
      gPending = true;
      clearTimeout(gTimer);
      gTimer = setTimeout(function () { gPending = false; }, 800);
    }
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
        '<span class="ql-spinner ql-inline-spinner" aria-hidden="true"></span> Starting…';
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

  // Whether the control being confirmed does something that cannot be undone.
  // The marker sits on the button the user presses; when the confirmation is
  // attached to the form around it, the form asks its own button.
  function isIrreversible(el) {
    if (!el || !el.hasAttribute) return false;
    if (el.hasAttribute("data-irreversible")) return true;
    return !!(el.querySelector && el.querySelector("[data-irreversible]"));
  }

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
      okButton.toggleAttribute("data-irreversible", !!opts.irreversible);
      okButton.classList.toggle("ql-btn-primary", !opts.irreversible);
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
    if (msg.indexOf("{count}") !== -1 || msg.indexOf("{s}") !== -1) {
      var m = (el.textContent || "").match(/[\d,]+/);
      // {s} pluralises from the same live number as {count}; a plural rendered
      // server-side goes stale the moment the selection changes.
      var one = !!m && m[0].replace(/,/g, "") === "1";
      msg = msg.replace("{count}", m ? m[0] : "the").replace(/\{s\}/g, one ? "" : "s");
    }
    window.qlConfirm(msg, {
      action: el.getAttribute("data-confirm-action") || "",
      irreversible: isIrreversible(el),
    }).then(function (ok) {
      if (!ok) return;
      if (el.tagName === "FORM") {
        // A form's click() never submits it. Fire a real submit event so
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
      irreversible: isIrreversible(source),
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

  // Discover artist rows open their albums under the row. htmx fetches the
  // list once; after that the same button is a plain show/hide.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-discover-toggle]");
    if (!btn) return;
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    var next = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", next ? "true" : "false");
    panel.classList.toggle("hidden", !next);
  });

  // Decade chips filter the albums already on screen. The years come from
  // Qobuz, so a chip press never asks for the list again. An album Qobuz has
  // no year for stays visible under every chip rather than disappearing into a
  // decade nobody can name.
  document.addEventListener("click", function (evt) {
    var chip = evt.target.closest && evt.target.closest("[data-decade]");
    if (!chip) return;
    var scope = chip.closest("[data-decade-scope]");
    if (!scope) return;
    evt.preventDefault();
    var decade = parseInt(chip.getAttribute("data-decade"), 10);
    Array.prototype.forEach.call(scope.querySelectorAll("[data-decade]"), function (c) {
      var on = c === chip;
      c.classList.toggle("is-active", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    Array.prototype.forEach.call(scope.querySelectorAll("[data-album-year]"), function (row) {
      var year = parseInt(row.getAttribute("data-album-year"), 10);
      var show = !decade || !year || Math.floor(year / 10) * 10 === decade;
      row.classList.toggle("hidden", !show);
    });
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
  var drawerScrollY = null;
  function lockDrawerPage() {
    if (drawerScrollY !== null || !document.body) return;
    drawerScrollY = Math.max(0, Math.round(window.scrollY || 0));
    document.body.style.setProperty("--ql-drawer-page-top", -drawerScrollY + "px");
    document.body.classList.add("ql-drawer-page-locked");
  }
  function unlockDrawerPage() {
    if (drawerScrollY === null || !document.body) return;
    var y = drawerScrollY;
    drawerScrollY = null;
    document.body.classList.remove("ql-drawer-page-locked");
    document.body.style.removeProperty("--ql-drawer-page-top");
    window.scrollTo(0, y);
  }
  function closeDrawer(dd) {
    var hadFocus = dd.contains(document.activeElement);
    dd.classList.remove("ql-mobile-drawer-open");
    dd.removeAttribute("open");
    var b = dd.querySelector("[aria-expanded]");
    if (b) b.setAttribute("aria-expanded", "false");
    // Hand focus back to the control that opened it. Blurring instead left it
    // on <body>, which costs a dead Tab and then restarts at "Skip to content".
    if (hadFocus) {
      var summary = dd.querySelector("summary");
      if (summary && summary.focus) summary.focus();
      else if (document.activeElement && document.activeElement.blur) {
        document.activeElement.blur();
      }
    }
    if (!document.querySelector(".ql-mobile-drawer.ql-mobile-drawer-open")) {
      unlockDrawerPage();
    }
  }

  // A sheet that covers the viewport keeps Tab inside itself; without this,
  // tabbing out of the drawer walks the page underneath it.
  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "Tab") return;
    var dd = document.querySelector("details.ql-mobile-drawer[open]");
    if (!dd || !dd.contains(document.activeElement)) return;
    var sheet = dd.querySelector(".ql-sheet");
    if (!sheet) return;
    var focusable = Array.prototype.filter.call(
      sheet.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'),
      function (el) { return el.offsetParent !== null; });
    var summary = dd.querySelector("summary");
    if (summary) focusable.unshift(summary);
    if (!focusable.length) return;
    var first = focusable[0];
    var last = focusable[focusable.length - 1];
    if (evt.shiftKey && document.activeElement === first) {
      evt.preventDefault();
      last.focus();
    } else if (!evt.shiftKey && document.activeElement === last) {
      evt.preventDefault();
      first.focus();
    }
  });
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
      lockDrawerPage();
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
  // One-shot flash flags should not stay in the URL after first paint.
  var FLASH_PARAMS = ["approved", "stale", "saved", "queued", "connected",
                      "unverified", "mode", "error", "noselection", "skipped",
                      "notice", "quality_note", "waiting"];
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
    } catch (e) { /* malformed URL; leave it alone */ }
  }
  function fade(el) { collapse(el, function () { if (el.parentNode) el.remove(); }); }
  function autoDismissFlashes() {
    // Keep warnings/errors visible; auto-clear low-risk notices and toasts.
    document.querySelectorAll("[data-flash].ql-notice-success, [data-flash].ql-notice-info, [data-flash].ql-flash-info")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 6000); });
    document.querySelectorAll("#download-toast [data-flash]")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 8000); });
  }
  function normalizeFlashAnnouncements() {
    document.querySelectorAll("[data-flash]").forEach(function (el) {
      if (el.getAttribute("role")) return;
      var isError = el.classList.contains("ql-notice-error")
        || el.classList.contains("ql-flash-error");
      el.setAttribute("role", isError ? "alert" : "status");
      if (!isError) el.setAttribute("aria-live", "polite");
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var copied = false;
    try { copied = document.execCommand("copy"); } catch (e) {}
    area.remove();
    return copied ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy-target]");
    if (!button) return;
    var target = document.querySelector(button.getAttribute("data-copy-target"));
    if (!target) return;
    var original = button.textContent;
    button.disabled = true;
    copyText(target.textContent.trim()).then(function () {
      button.textContent = "Copied";
    }).catch(function () {
      button.textContent = "Copy failed";
    }).then(function () {
      setTimeout(function () {
        button.textContent = original;
        button.disabled = false;
      }, 1600);
    });
  });
  // Escape is the user asking for a clear screen, so it takes everything.
  window.qlDismissAllFlashes = function () {
    document.querySelectorAll("[data-flash]").forEach(fade);
  };
  // What a job's own progress replaces: "Queued", "Saved", and the like.
  // A warning is not replaced by a job starting. "Started the valid choices,
  // 1 selected album changed since this review" is the only place the user is
  // told something was left out, and the first progress tick used to wipe it
  // a second after it appeared.
  window.qlDismissSupersededFlashes = function () {
    document.querySelectorAll(
      "[data-flash].ql-notice-success, [data-flash].ql-notice-info,"
      + " [data-flash].ql-flash-info"
    ).forEach(fade);
  };

  // Programmatic toast for async failures and review actions.
  // `message` is a string (rendered as text, never markup) or a prebuilt
  // node. The node form is what lets a receipt carry a real link, which a
  // textContent-only toast structurally couldn't.
  function showToast(message, kind) {
    var host = document.getElementById("download-toast");
    if (!host) return;
    var el = document.createElement("div");
    var noticeKind = kind || "info";
    el.className = "ql-notice ql-notice-" + noticeKind;
    el.setAttribute("data-flash", "");
    el.setAttribute("data-flash-kind", noticeKind);
    el.setAttribute("role", "status");
    if (message && message.nodeType) {
      el.appendChild(message);
    } else {
      var span = document.createElement("span");
      span.textContent = message;
      el.appendChild(span);
    }
    host.appendChild(el);
    setTimeout(function () { fade(el); }, 8000);
  }
  window.qlShowToast = showToast;

  // Did the request fail because nothing could be reached, rather than because
  // the server answered with an error? fetch() rejects with a TypeError when
  // the connection never completes, and navigator.onLine covers a drop that
  // happened while the answer was in flight.
  function serverUnreachable(why) {
    return why instanceof TypeError || !navigator.onLine;
  }

  // Reloading is how the raw fetch() paths recover from a failed action, and
  // with the connection down it is the worst thing they can do: the service
  // worker answers the navigation with the offline page, so the view the user
  // was working in disappears along with the message explaining why.
  function reloadToRecover() {
    if (!navigator.onLine) return false;
    setTimeout(function () { window.location.reload(); }, 900);
    return true;
  }

  // A stale or missing CSRF token is refused and answered with a fresh cookie,
  // so nothing was written and a retry can't succeed until the page reloads
  // carrying the token that matches it. htmx handles this on its own; the raw
  // fetch() paths have to say so and reload.
  function pageWentStale() {
    if (reloadToRecover()) {
      showToast("That page went stale. Reloading.", "error");
      return;
    }
    showToast("That page went stale, and the server can't be reached to reload it."
      + " Reconnect, then reload the page.", "error");
  }

  // Raw fetch does not understand htmx's navigation headers. Mark these
  // requests as htmx-compatible, then follow a rejected session to Login or
  // reload after the CSRF middleware refreshes a stale token before a caller
  // mistakes either response for JSON/HTML from the requested action.
  function sessionFetch(url, options) {
    var requestOptions = Object.assign({}, options || {});
    requestOptions.headers = Object.assign({}, requestOptions.headers || {}, {
      "HX-Request": "true",
      "HX-Current-URL": window.location.href,
    });
    return fetch(url, requestOptions).then(function (response) {
      if (response.headers.get("HX-Refresh") === "true") {
        pageWentStale();
        return Promise.reject("navigation");
      }
      var target = response.headers.get("HX-Redirect") || "";
      if (response.status === 401 && target.charAt(0) === "/"
          && target.charAt(1) !== "/") {
        window.location.assign(target);
        return Promise.reject("navigation");
      }
      return response;
    });
  }

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

  function searchResponseState(results) {
    var marker = results && results.querySelector("[data-search-response-state]");
    return marker ? marker.dataset.searchResponseState || "" : "";
  }

  function searchSnapshotHtml(results) {
    var copy = results.cloneNode(true);
    copy.querySelectorAll("[data-search-wired]").forEach(function (node) {
      node.removeAttribute("data-search-wired");
    });
    copy.querySelectorAll("[data-cover-fallback-wired]").forEach(function (node) {
      node.removeAttribute("data-cover-fallback-wired");
    });
    return copy.innerHTML;
  }

  function saveSearchSnapshot() {
    if (searchSnapshotsInvalidated) return;
    var results = document.getElementById("search-results");
    var marker = results && results.querySelector("[data-search-response-state]");
    var state = marker ? marker.dataset.searchResponseState || "" : "";
    if (!results || !state) return;
    var values = {};
    if (!searchPositionRestoring) values.scrollY = Math.max(0, Math.round(window.scrollY || 0));
    updateSearchRecord(state, values);
    if (marker.dataset.searchCacheable === "0") {
      removeSearchSnapshot(state);
      return;
    }
    writeSearchSnapshot(state, searchSnapshotHtml(results));
  }

  function finishSearchRestore(record) {
    var savedY = pendingSearchRestoreY;
    if (savedY === null && record && typeof record.scrollY === "number") {
      savedY = record.scrollY;
    }
    savedY = Math.max(0, Math.round(savedY || 0));
    requestAnimationFrame(function () {
      var height = Math.max(
        document.documentElement.scrollHeight,
        document.body ? document.body.scrollHeight : 0
      );
      window.scrollTo(0, Math.min(savedY, Math.max(0, height - window.innerHeight)));
      pendingSearchRestoreY = null;
      searchPositionRestoring = false;
      document.documentElement.classList.remove("ql-search-restoring");
      if (searchRestoreFallback) clearTimeout(searchRestoreFallback);
      searchRestoreFallback = null;
      if (searchExitSave) searchExitSave();
    });
  }

  var searchExitSave = null;
  window.addEventListener("pagehide", function () {
    if (searchExitSave) searchExitSave();
  });

  function initSearchPage() {
    var form = document.querySelector(".ql-search-form");
    var results = document.getElementById("search-results");
    if (!form || !results) {
      document.documentElement.classList.remove("ql-search-restoring");
      return;
    }
    searchExitSave = saveSearchSnapshot;
    if (bootSearchRecord && typeof bootSearchRecord.scrollY === "number"
        && pendingSearchRestoreY === null) {
      pendingSearchRestoreY = bootSearchRecord.scrollY;
    }
    if (bootSearchHtml && bootSearchState) {
      results.innerHTML = bootSearchHtml;
      if (window.htmx) window.htmx.process(results);
      form.querySelectorAll("[data-deep-link]").forEach(function (node) {
        node.remove();
      });
      finishSearchRestore(bootSearchRecord);
      bootSearchRecord = null;
      bootSearchHtml = "";
      return;
    }
    document.documentElement.classList.remove("ql-search-restoring");
    if (form.dataset.searchAuto === "1" && window.htmx) {
      window.htmx.trigger(form, "submit");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSearchPage);
  } else {
    initSearchPage();
  }

  function initSearchResults() {
    document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
      if (root.dataset.searchWired === "1") return;
      root.dataset.searchWired = "1";
      // Ticks and scroll offset survive a reload or a trip to the Queue, keyed
      // by the search that produced them. Picking twenty albums out of five
      // hundred is twenty minutes of scrolling; losing it to a pull-to-refresh
      // was the worst thing this screen did.
      // Keyed by the search itself, not the URL: results arrive by htmx swap,
      // so the URL reads "/" for every query and one search's picks would
      // haunt the next (ghost "3 selected" bars, phantom scroll jumps).
      var state = root.dataset.searchState || location.pathname + location.search;
      function loadSelection() {
        return readSearchRecord(state) || {};
      }
      function saveSelection(preserveSavedAt) {
        var live = {};
        Object.keys(selected).forEach(function (key) {
          if (selected[key]) live[key] = 1;
        });
        root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
          if (!panel.classList.contains("hidden")
              && panel.querySelector("[data-search-item]")) {
            viewScroll[panel.dataset.searchViewPanel] = Math.max(
              0, Math.round(panel.scrollTop || 0)
            );
          }
        });
        var values = {
          picks: live,
          hideOwned: !!(hideOwnedButton
            && hideOwnedButton.getAttribute("aria-pressed") === "true"),
          viewScroll: viewScroll,
        };
        if (!searchPositionRestoring) {
          values.scrollY = Math.max(0, Math.round(window.scrollY || 0));
        }
        updateSearchRecord(
          state,
          values,
          preserveSavedAt || searchSnapshotsInvalidated
        );
      }
      var restored = loadSelection();
      var selected = {};
      Object.keys(restored.picks || {}).forEach(function (k) { selected[k] = true; });
      var viewScroll = {};
      Object.keys(restored.viewScroll || {}).forEach(function (name) {
        var value = Number(restored.viewScroll[name]);
        if (isFinite(value) && value >= 0) viewScroll[name] = Math.round(value);
      });
      var bulkBar = root.querySelector("[data-search-bulk-bar]");
      var selectedCount = root.querySelector("[data-search-selected-count]");
      var selectAll = root.querySelector("[data-search-select-all]");
      var bulkButton = root.querySelector("[data-search-bulk-download]");
      var clearButton = root.querySelector("[data-search-clear-selection]");
      var hideOwnedButton = root.querySelector("[data-search-hide-owned]");
      if (hideOwnedButton && restored.hideOwned) {
        hideOwnedButton.setAttribute("aria-pressed", "true");
        root.classList.add("is-filtering-owned");
      }

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
      // "Select all" means every album listed, one per record. An alternate
      // pressing is a deliberate individual tick: sweeping an open versions
      // fold in selected a record and two more pressings of that same record,
      // and the duplicate guard keys on album id, so all three were accepted as
      // separate downloads into one folder.
      function bulkSelectable(box) {
        return visibleItem(box.closest("[data-search-item]"))
          && !box.closest("[data-version-panel]")
          && !!actionableFormForKey(box.dataset.searchKey);
      }
      function reconcileSelection() {
        Object.keys(selected).forEach(function (key) {
          if (!actionableFormForKey(key)) delete selected[key];
        });
      }
      function syncBoxes() {
        reconcileSelection();
        root.querySelectorAll("[data-search-select]").forEach(function (cb) {
          cb.checked = !!selected[cb.dataset.searchKey];
        });
        var keys = selectedKeys();
        if (bulkBar) bulkBar.classList.toggle("hidden", keys.length === 0);
        if (selectedCount) {
          selectedCount.textContent = keys.length + " selected";
        }
        if (selectAll) {
          var selectable = Array.prototype.filter.call(
            root.querySelectorAll("[data-search-select]"), bulkSelectable);
          var selectableKeys = {};
          selectable.forEach(function (cb) { selectableKeys[cb.dataset.searchKey] = true; });
          var allKeys = Object.keys(selectableKeys);
          var picked = allKeys.filter(function (k) { return selected[k]; }).length;
          selectAll.disabled = allKeys.length === 0;
          selectAll.closest(".ql-search-select-all").classList.toggle(
            "hidden", allKeys.length === 0);
          selectAll.checked = allKeys.length > 0 && picked === allKeys.length;
          selectAll.indeterminate = picked > 0 && picked < allKeys.length;
        }
        saveSelection();
      }
      // "Hide owned" is a CSS filter, so update the visible count here.
      function applyOwnedFilterCount() {
        var meta = root.querySelector("[data-search-count]");
        var empty = root.querySelector("[data-owned-empty]");
        var on = root.classList.contains("is-filtering-owned");
        var activePanel = null;
        root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
          if (!panel.classList.contains("hidden")) activePanel = panel;
        });
        // Count the active view's primary records. Layout visibility is false
        // while the htmx skeleton hides the whole result, even though these are
        // the records that appear when the request settles.
        var shown = activePanel ? Array.prototype.filter.call(
          activePanel.querySelectorAll("[data-search-item]"),
          function (el) {
            return !el.closest("[data-version-panel]")
              && !(on && el.dataset.owned === "1");
          }).length : 0;
        if (meta) {
          if (on) {
            meta.textContent = shown + " album" + (shown === 1 ? "" : "s")
              + " not in your library";
          } else if (meta.dataset.fullLabel) {
            meta.textContent = meta.dataset.fullLabel;
          }
        }
        if (empty) empty.classList.toggle("hidden", !(on && shown === 0));
      }
      function parkSearchView(panel) {
        var template = panel.querySelector("template[data-search-view-template]");
        if (!template) return;
        if (panel.querySelector("[data-search-item]")) {
          viewScroll[panel.dataset.searchViewPanel] = Math.max(
            0, Math.round(panel.scrollTop || 0)
          );
        }
        Array.prototype.slice.call(panel.childNodes).forEach(function (node) {
          if (node !== template) template.content.appendChild(node);
        });
      }
      function materializeSearchView(panel) {
        var template = panel.querySelector("template[data-search-view-template]");
        if (template && template.content.childNodes.length) {
          panel.appendChild(template.content);
        }
        var savedScroll = viewScroll[panel.dataset.searchViewPanel];
        if (typeof savedScroll === "number") {
          panel.scrollTop = savedScroll;
          requestAnimationFrame(function () { panel.scrollTop = savedScroll; });
        }
      }
      function setView(name) {
        var panels = Array.prototype.slice.call(
          root.querySelectorAll("[data-search-view-panel]")
        );
        panels.forEach(function (panel) {
          if (panel.dataset.searchViewPanel !== name) parkSearchView(panel);
        });
        var activePanel = null;
        panels.forEach(function (panel) {
          var active = panel.dataset.searchViewPanel === name;
          if (active) {
            materializeSearchView(panel);
            activePanel = panel;
          }
          panel.classList.toggle("hidden", !active);
        });
        root.querySelectorAll("[data-search-view]").forEach(function (btn) {
          var active = btn.dataset.searchView === name;
          btn.classList.toggle("is-active", active);
          btn.setAttribute("aria-pressed", active ? "true" : "false");
        });
        if (activePanel && window.htmx) window.htmx.process(activePanel);
        initCoverFallbacks();
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
            // Ticking it adds the albums; clearing it leaves nothing behind,
            // including any alternate pressing picked by hand.
            if (on ? !bulkSelectable(box)
                   : !visibleItem(box.closest("[data-search-item]"))) return;
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
          applyOwnedFilterCount();
          return;
        }
        if (hideOwnedButton && evt.target.closest("[data-search-hide-owned]")) {
          evt.preventDefault();
          var next = hideOwnedButton.getAttribute("aria-pressed") !== "true";
          hideOwnedButton.setAttribute("aria-pressed", next ? "true" : "false");
          root.classList.toggle("is-filtering-owned", next);
          applyOwnedFilterCount();
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
      function actionableFormForKey(key) {
        var items = Array.prototype.filter.call(root.querySelectorAll("[data-search-item]"),
          function (item) { return item.dataset.searchKey === key; });
        for (var i = 0; i < items.length; i++) {
          var form = items[i].querySelector("[data-search-download-form]");
          var button = form && form.querySelector("button[type=submit]");
          if (form && button && !button.disabled) return form;
        }
        return null;
      }
      function postForm(form) {
        return sessionFetch(form.action || "/download", {
          method: "POST",
          headers: {
            "X-CSRF-Token": csrf(),
          },
          body: new FormData(form),
        }).then(function (r) {
          return r.text().then(function (text) {
            // A stale token answers an HX request with 200, an empty body and
            // HX-Refresh. htmx reloads on that header; fetch ignores it, so
            // without this check an empty success reads as "queued" and the
            // page reports downloads the server refused.
            return {
              ok: r.ok,
              stale: r.ok && r.headers.get("HX-Refresh") === "true",
              outcome: r.headers.get("X-QL-Download-Outcome") || "",
              text: text,
            };
          });
        });
      }
      function bulkDownload() {
        var keys = selectedKeys();
        if (!keys.length || bulkButton.disabled) return;
        var forms = keys.map(actionableFormForKey).filter(Boolean);
        if (!forms.length) return;
        // The key prefix supplies the correct track or album noun.
        var nTracks = keys.filter(function (k) { return k.indexOf("track-") === 0; }).length;
        var nAlbums = keys.length - nTracks;
        function part(n, one) { return n + " " + (n === 1 ? one : one + "s"); }
        var what = nTracks && nAlbums
          ? part(nAlbums, "album") + " and " + part(nTracks, "track")
          : part(keys.length, nTracks ? "track" : "album");
        if (forms.length <= 3) {
          var names = forms.map(itemLabel).filter(Boolean);
          if (names.length === forms.length) {
            what = names.length === 1
              ? names[0]
              : names.slice(0, -1).join(", ")
                + (names.length === 2 ? " and " : ", and ")
                + names[names.length - 1];
          }
        }
        window.qlConfirm("Download " + what + "? Downloads queue now and import into your library.", { action: "Download" }).then(function (ok) {
          if (ok) runBulkDownload(forms);
        });
      }
      function itemTitle(form) {
        if (form.dataset.searchTitle) return form.dataset.searchTitle;
        var item = form.closest && form.closest("[data-search-item]");
        var el = item && item.querySelector(".ql-table-title, .ql-grid-title, .ql-result-title, .ql-subtitle");
        return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
      }
      function itemLabel(form) {
        var title = itemTitle(form);
        if (!title) return "";
        var artist = form.dataset.searchArtist || "";
        return "“" + title + "”" + (artist ? " by " + artist : "");
      }
      function runBulkDownload(forms) {
        var original = bulkButton.textContent;
        bulkButton.disabled = true;
        bulkButton.textContent = "Queueing…";
        var queued = 0;
        var queuedTracks = 0;
        var duplicates = 0;
        var owned = 0;
        var failed = 0;
        var stale = false;
        var navigating = false;
        var firstTitle = "";
        var chain = Promise.resolve();
        forms.forEach(function (form) {
          chain = chain.then(function () {
            if (navigating) return;
            return postForm(form).then(function (res) {
              if (res.stale) { stale = true; failed += 1; }
              else if (!res.ok || res.text.indexOf("ql-notice-error") >= 0) failed += 1;
              else if (res.outcome === "duplicate") {
                duplicates += 1;
                markSearchDownloadQueued(form);
              } else if (res.outcome === "owned") {
                owned += 1;
                markSearchDownloadOwned(form);
              } else if (res.outcome === "queued"
                         || (!res.outcome && res.text.indexOf("ql-notice-success") >= 0)) {
                queued += 1;
                if (form.querySelector('input[name="track_id"]')) queuedTracks += 1;
                if (!firstTitle) firstTitle = itemTitle(form);
                markSearchDownloadQueued(form);
              } else failed += 1;
            }).catch(function (why) {
              if (why === "navigation") navigating = true;
              else failed += 1;
            });
          });
        });
        chain.then(function () {
          bulkButton.disabled = false;
          bulkButton.textContent = original;
          if (navigating) return;
          if (stale) { pageWentStale(); return; }
          selected = {};
          syncBoxes();
          // The same receipt the single-row path gives: name what was queued
          // and link where it went, instead of a bare count.
          var parts = [];
          if (queued) {
            var queuedAlbums = queued - queuedTracks;
            var what = !queuedAlbums ? cnt(queued, "track", "tracks")
              : !queuedTracks ? cnt(queued, "album", "albums")
              : cnt(queuedAlbums, "album", "albums") + " and " + cnt(queuedTracks, "track", "tracks");
            parts.push(queued === 1 && firstTitle
              ? "“" + firstTitle + "” queued"
              : what + " queued");
          }
          function cnt(n, one, many) { return n + " " + (n === 1 ? one : many); }
          if (duplicates) parts.push(duplicates + " already queued");
          if (owned) parts.push(owned + " already in library");
          if (failed) parts.push(failed + " failed");
          var receipt = document.createElement("span");
          receipt.appendChild(document.createTextNode(
            (parts.length ? parts.join(", ") + ". " : "Nothing queued. ")));
          if (queued || duplicates) {
            var link = document.createElement("a");
            link.href = "/queue";
            link.className = "ql-inline-link";
            link.textContent = "View queue";
            receipt.appendChild(link);
          }
          showToast(receipt, failed ? "error" : (queued || duplicates ? "success" : "info"));
          // Surface the Background-work strip without a reload. It's the
          // page's persistent signal that something is now running.
          if (queued && window.htmx && document.getElementById("dashboard-active")) {
            window.htmx.ajax("GET", "/",
              { target: "#dashboard-active", swap: "outerHTML", select: "#dashboard-active" });
          }
          if (queued && window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
        });
      }
      setView(savedSearchView());
      syncBoxes();
      applyOwnedFilterCount();
      root.addEventListener("qlSearchAvailabilityChanged", function () {
        if (searchSnapshotsInvalidated) {
          var current = loadSelection();
          selected = {};
          Object.keys(current.picks || {}).forEach(function (key) {
            selected[key] = true;
          });
        }
        syncBoxes();
      });
      searchExitSave = function () {
        saveSelection();
        saveSearchSnapshot();
      };
    });
    if (document.querySelector("[data-search-results-root]")) {
      refreshSearchAvailability();
    }
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
    // Same silent-stream problem as the job page: a socket that dies without
    // closing never raises an error, so watch the gap since the last event.
    var SILENT_STREAM_MS = 25000;
    var src = null;
    var finished = false;
    var warnDelay = null;
    var silenceTimer = null;
    var lastEvent = Date.now();
    function shut() {
      if (src) { try { src.close(); } catch (e) {} }
      if (silenceTimer) { clearInterval(silenceTimer); silenceTimer = null; }
      window.removeEventListener("online", onOnline);
      document.removeEventListener("htmx:beforeSwap", onSwap);
    }
    function onSwap(e) {
      if (!e.detail || !e.detail.target) return;
      if (e.detail.target.id === containerId) shut();
    }
    document.addEventListener("htmx:beforeSwap", onSwap);
    function showGap(text, isError) {
      if (!reconnect) return;
      reconnect.textContent = text;
      reconnect.classList.toggle("is-error", !!isError);
      reconnect.classList.toggle("is-warning", !isError);
      reconnect.classList.remove("hidden");
    }
    function streamAlive() {
      lastEvent = Date.now();
      if (warnDelay) { clearTimeout(warnDelay); warnDelay = null; }
      if (reconnect) reconnect.classList.add("hidden");
    }
    function watchStream() {
      if (!document.body.contains(card)) { shut(); return; }
      if (finished || Date.now() - lastEvent < SILENT_STREAM_MS) return;
      showGap("Reconnecting", false);
      openStream();
    }
    function onOnline() {
      openStream();
    }
    // The card is redrawn by a fetch once the run ends, so a connection that
    // is down at that moment leaves a finished job sitting there as running.
    function refreshSurface() {
      if (!window.htmx) { reloadToRecover(); return; }
      if (!navigator.onLine) {
        showGap("Connection lost. Reload to see the latest.", true);
        window.addEventListener("online", refreshSurface, { once: true });
        return;
      }
      if (surface === "dashboard") {
        window.htmx.ajax("GET", "/",
          { target: "#dashboard-active", swap: "outerHTML", select: "#dashboard-active" });
      } else {
        window.htmx.ajax("GET", "/queue",
          { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
      }
    }
    function openStream() {
      if (src) { try { src.close(); } catch (e) {} }
      lastEvent = Date.now();
      src = new EventSource("/api/jobs/" + id + "/stream");
      src.onmessage = streamAlive;
      src.addEventListener("ping", streamAlive);
      src.onopen = streamAlive;
      src.onerror = function () {
        if (src.readyState === EventSource.CLOSED) {
          if (warnDelay) { clearTimeout(warnDelay); warnDelay = null; }
          showGap("Connection lost. Reload to see the latest.", true);
        } else if (reconnect && !warnDelay) {
          // Phones drop the stream the moment the tab backgrounds; only warn
          // when a reconnect still hasn't landed after a real wait.
          warnDelay = setTimeout(function () {
            warnDelay = null;
            showGap("Reconnecting", false);
          }, 5000);
        }
      };
      src.addEventListener("progress", onProgress);
      src.addEventListener("done", onDone);
    }
    function onProgress(e) {
      streamAlive();
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
    }
    function onDone(e) {
      finished = true;
      shut();
      var endStatus = (e && e.data) ? ("" + e.data).trim() : "";
      if (endStatus === "failed") {
        var t = card.querySelector('a[href^="/jobs/"]');
        var label = ((t && t.textContent) || "A job").replace(/\s+/g, " ").trim();
        showToast(label + " failed. Open History for details.", "error");
      }
      refreshSurface();
      if (window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
    }
    openStream();
    silenceTimer = setInterval(watchStream, 5000);
    window.addEventListener("online", onOnline);
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
    function stopElapsed() {
      if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
    }
    var startElapsed = function () {};
    if (elapsedEl && elapsedEl.dataset.start) {
      var serverStart = parseFloat(elapsedEl.dataset.start);
      var serverNow = parseFloat(elapsedEl.dataset.now);
      var elapsedAtLoad = (isFinite(serverNow) ? serverNow : Date.now() / 1000) - serverStart;
      if (!(elapsedAtLoad >= 0)) elapsedAtLoad = 0;
      var clientBase = Date.now();
      var tickElapsed = function () {
        if (!document.body.contains(elapsedEl)) { stopElapsed(); return; }
        var secs = Math.max(0, Math.floor(elapsedAtLoad + (Date.now() - clientBase) / 1000));
        var mm = Math.floor(secs / 60), ss = secs % 60;
        elapsedEl.textContent = "· " + mm + ":" + (ss < 10 ? "0" : "") + ss + " elapsed";
      };
      // The clock reads off the server's start time, so a stream that dropped
      // and came back picks the count straight back up.
      startElapsed = function () {
        if (elapsedTimer) return;
        tickElapsed();
        elapsedTimer = setInterval(tickElapsed, 1000);
      };
      startElapsed();
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
      if (activity && (activity.textContent === "Queued" || activity.textContent === "Waiting to start")) {
        activity.textContent = jc && jc.dataset.jobKind === "repair" ? "Scan in progress" : "Scanning";
      }
    }
    // A stream that dies without closing is invisible to EventSource: no
    // error fires, the last progress line just stays on screen and a finished
    // scan goes on reading as a running one. The server pings a quiet stream,
    // so the time since anything last arrived is what tells us it is gone.
    var SILENT_STREAM_MS = 25000;
    var STALE_NOTE = "Connection lost. This is the last progress the app received.";
    var src = null;
    var opened = false;
    var finished = false;
    var reconnectDelay = null;
    var silenceTimer = null;
    var lastEvent = Date.now();

    function showGap(text, isError) {
      if (!reconnect) return;
      reconnect.textContent = text;
      reconnect.classList.toggle("is-error", !!isError);
      reconnect.classList.toggle("is-warning", !isError);
      reconnect.classList.remove("hidden");
    }
    function hideGap() {
      if (reconnectDelay) { clearTimeout(reconnectDelay); reconnectDelay = null; }
      if (reconnect) reconnect.classList.add("hidden");
    }
    function streamAlive() {
      lastEvent = Date.now();
      hideGap();
      startElapsed();
    }
    function stopWatching() {
      if (silenceTimer) { clearInterval(silenceTimer); silenceTimer = null; }
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    }

    function openStream() {
      if (src) { try { src.close(); } catch (e) {} }
      lastEvent = Date.now();
      src = new EventSource("/api/jobs/" + id + "/stream");
      src.onmessage = function (e) {
        streamAlive();
        if (!titleSet) { document.title = "\u25b6 " + baseTitle; titleSet = true; }
        clearQueuedState();
        if (logEl) { logEl.appendChild(document.createTextNode(e.data + "\n")); logEl.scrollTop = logEl.scrollHeight; }
      };
      src.addEventListener("ping", streamAlive);
      src.onopen = function () {
        streamAlive();
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
          stopElapsed();
          showGap("Connection lost. Reload to see the latest.", true);
        } else if (reconnect && !reconnectDelay) {
          // Backgrounding the tab on a phone kills the stream every time; hold
          // the banner back until a reconnect has genuinely failed to land.
          reconnectDelay = setTimeout(function () {
            reconnectDelay = null;
            showGap(STALE_NOTE, false);
          }, 5000);
        }
      };
      src.addEventListener("progress", function (e) {
        streamAlive();
        var p; try { p = JSON.parse(e.data); } catch (_) { return; }
        clearQueuedState();
        if (window.qlDismissSupersededFlashes) window.qlDismissSupersededFlashes();
        if (activity && p.phase && (!jc || jc.dataset.jobKind !== "repair") && activity.textContent !== p.phase) {
          activity.textContent = p.phase;
        }
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
        // Beets runs to its own end, so withdraw Cancel while an album is going
        // into the library rather than take a stop the import will run past.
        // The chip that replaces it says so; a greyed button explains nothing.
        var cancelForm = document.querySelector("[data-cancel-form]");
        var importChip = document.querySelector("[data-import-chip]");
        if (cancelForm && importChip) {
          cancelForm.classList.toggle("hidden", !!p.importing);
          importChip.classList.toggle("hidden", !p.importing);
        }
      });
      src.addEventListener("done", onDone);
    }

    function watchStream() {
      if (reconnect && !document.body.contains(reconnect)) { stopWatching(); return; }
      if (finished || Date.now() - lastEvent < SILENT_STREAM_MS) return;
      showGap(STALE_NOTE, false);
      openStream();
    }
    function onOffline() {
      if (!finished) showGap(STALE_NOTE, false);
    }
    function onOnline() {
      if (finished) { finishJob(); return; }
      openStream();
    }

    function onDone() {
      finished = true;
      if (src) { try { src.close(); } catch (e) {} }
      stopElapsed();
      document.title = baseTitle;
      if (window.qlDismissSupersededFlashes) window.qlDismissSupersededFlashes();
      finishJob();
    }

    // The finished job body is fetched, not pushed. With nothing reachable at
    // the moment the run ends, the page would sit on a scan that stopped
    // running minutes ago, so say where it stands and ask again on reconnect.
    function finishJob() {
      var body = document.getElementById("job-content");
      if (!body) { stopWatching(); return; }
      if (!window.htmx) { reloadToRecover(); return; }
      var embedded = body.dataset.embedded ? "?embedded=1" : "";
      sessionFetch("/jobs/" + id + "/content" + embedded)
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          var target = document.getElementById("job-content");
          if (!target) return;
          stopWatching();
          hideGap();
          window.htmx.swap(target, html, { swapStyle: "outerHTML" });
        })
        .catch(function (why) {
          if (why === "navigation") return;
          if (navigator.onLine) {
            showGap("This run has finished. Reload to see the result.", true);
            return;
          }
          showGap("This run has finished. It will load when you are back online.", false);
        });
    }

    openStream();
    silenceTimer = setInterval(watchStream, 5000);
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
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
    var reviewBlocked = form.getAttribute("data-review-blocked") === "1";
    var isDownsampleReview = reviewKind === "downsample";
    var reviewItemSingular = cont.dataset.reviewItemSingular || "album";
    var reviewItemPlural = cont.dataset.reviewItemPlural || "albums";
    // Live, not baked: on a library review the dismiss vocabulary follows
    // whichever tab is active now. The data attributes only know the tab the
    // page was first rendered with.
    function dismissItemSingular() {
      if (reviewKind === "library" && curTab()) {
        return curTab() === "gaps" ? "Gap Fill candidate" : "missing album";
      }
      return cont.dataset.reviewDismissSingular || "album";
    }
    function dismissItemPlural() {
      if (reviewKind === "library" && curTab()) {
        return curTab() === "gaps" ? "Gap Fill candidates" : "missing albums";
      }
      return cont.dataset.reviewDismissPlural || "albums";
    }
    function otherTabNote() {
      if (reviewKind !== "library" || !curTab()) {
        return cont.dataset.reviewOtherTab || "";
      }
      return curTab() === "gaps" ? "Your missing albums are untouched."
                                 : "Your Gap Fill list is untouched.";
    }

    var countFormatter = new Intl.NumberFormat();
    function formatCount(n) { return countFormatter.format(n); }
    function plural(n, w) { return formatCount(n) + " " + w + (n === 1 ? "" : "s"); }
    function countLabel(n, one, many) { return formatCount(n) + " " + (n === 1 ? one : many); }
    function dismissRestLabel(rest) {
      return (isDownsampleReview ? "Keep hi-res" : "Dismiss unselected") + " (" + formatCount(rest) + ")";
    }
    function artistDismissLabel(picked) {
      if (isDownsampleReview) return "Keep hi-res";
      return "Dismiss unselected";
    }
    function dismissConfirm(rest) {
      if (isDownsampleReview) {
        return "Keep " + countLabel(rest, "unselected album", "unselected albums") + " hi-res? You can downsample later.";
      }
      var other = otherTabNote();
      return "Dismiss " + countLabel(rest, "unselected " + dismissItemSingular(), "unselected " + dismissItemPlural()) + "?"
        + (other ? " " + other : "") + " You can bring " + (rest === 1 ? "it" : "them") + " back from Dismissed.";
    }
    function dismissBusyLabel() { return isDownsampleReview ? "Keeping…" : "Dismissing…"; }
    function dismissToast(count) {
      return isDownsampleReview
        ? "Kept " + plural(count, "album") + " hi-res."
        : "Dismissed " + countLabel(count, dismissItemSingular(), dismissItemPlural()) + ".";
    }
    function csrf() {
      var m = document.querySelector('meta[name="csrf-token"]');
      return m ? m.content : "";
    }
    function pageBox() { return document.getElementById("review-groups"); }
    function curPage() { var g = pageBox(); return g ? parseInt(g.dataset.page || "1", 10) : 1; }
    function inputQuery() { return (filterInput && filterInput.value || "").trim(); }
    var loadedQuery = inputQuery();
    function curQuery() { return loadedQuery; }
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
    // The active tab's share of the counts. The bulk bar acts on the active
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

    function reviewPageCounts(box) {
      if (!box || box.dataset.reviewTotal === undefined) return null;
      var c = {
        total: parseInt(box.dataset.reviewTotal || "0", 10),
        selected: parseInt(box.dataset.reviewSelected || "0", 10),
        artists: parseInt(box.dataset.reviewArtists || "0", 10),
        reclaimable: parseInt(box.dataset.reviewReclaimable || "0", 10),
        reclaimable_label: box.dataset.reviewReclaimableLabel || "",
        hidden_total: parseInt(box.dataset.reviewHiddenTotal || "0", 10),
        filtered_rest: parseInt(box.dataset.filteredRest || "0", 10),
      };
      if (box.dataset.reviewMissingTotal !== undefined) {
        c.missing_total = parseInt(box.dataset.reviewMissingTotal || "0", 10);
        c.missing_selected = parseInt(box.dataset.reviewMissingSelected || "0", 10);
        c.gap_total = parseInt(box.dataset.reviewGapTotal || "0", 10);
        c.gap_selected = parseInt(box.dataset.reviewGapSelected || "0", 10);
      }
      return c;
    }

    // Counts come from the server because the DOM only holds one page. The
    // bulk bar (submit + dismiss-unselected) is scoped to the active tab.
    function applyCounts(c) {
      if (!c) return;
      lastCounts = c;
      if (typeof c.filtered_rest === "number") {
        var fb = pageBox();
        if (fb) fb.dataset.filteredRest = String(c.filtered_rest);
      }
      var tc = tabCounts(c);
      if (submit) {
        submit.disabled = reviewBlocked || tc.selected === 0;
        submit.textContent = tc.selected
          ? (submit.dataset.reviewVerb || "Download") + " " + formatCount(tc.selected) + " selected"
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
        // "smaller", not "reclaimed": with originals kept, the run costs more
        // disk than it saves until the backups expire.
        dsTotal.textContent = c.reclaimable_label
          ? " · selected: ~" + c.reclaimable_label + " smaller" : "";
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
        if (mc) mc.textContent = formatCount(c.missing_total);
        if (gc) gc.textContent = formatCount(c.gap_total);
      }
      // Only hide/dismiss responses carry hidden_total; a plain tick doesn't
      // change it, so its absence means "leave the link alone".
      if (c.hidden_total !== undefined) {
        var dl = cont.querySelector(".ql-review-dismissed-link");
        if (dl) {
          dl.textContent = (dl.getAttribute("data-hidden-label") || "Dismissed")
            + " (" + formatCount(c.hidden_total) + ")";
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
      return sessionFetch(url, {
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
      // Under a filter the response also recounts what Dismiss unselected
      // would take, so the button can't quote the number from render time.
      var scopeQ = curQuery()
        ? "&q=" + encodeURIComponent(curQuery()) +
          "&tab=" + encodeURIComponent(curTab())
        : "";
      var det = cb.closest("details[data-artist]");
      function revert() {
        cb.checked = previous;
        cb.style.outline = "2px solid #ef4444";
        setTimeout(function () { cb.style.outline = ""; }, 1500);
      }
      // Serialize per-box saves so the server matches the checkbox. NOT by
      // disabling it: disabling the element that has focus hands focus to
      // <body>, and re-enabling never gives it back, so a keyboard user had to
      // Tab from the top of the page again for every single tick. A busy flag
      // does the same job and leaves focus where the user put it.
      if (cb.dataset.saving === "1") {
        // The box already flipped visually; putting it back beats letting the
        // screen contradict the server with nothing to heal it.
        cb.checked = previous;
        return;
      }
      cb.dataset.saving = "1";
      cb.setAttribute("aria-busy", "true");
      function done() {
        delete cb.dataset.saving;
        cb.removeAttribute("aria-busy");
      }
      var body = "cid=" + encodeURIComponent(cb.value) + "&checked=" + (cb.checked ? "1" : "0") + scopeQ;
      post("/jobs/" + id + "/select", body)
        .then(function (r) {
          if (r.status === 403) return Promise.reject("stale");
          return r.ok ? r.json() : Promise.reject();
        })
        .then(function (c) {
          done();
          if (det) delete det.dataset.selectionOverride;
          if (c) applyCounts(c);
          updateHideLabels();
          updateArtistChecks();
        })
        .catch(function (why) {
          done();
          if (why === "navigation") return;
          revert();
          if (why === "stale") { pageWentStale(); return; }
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

    function groupCandidateIds(det) {
      var raw = det.dataset.cids || "";
      return raw ? raw.split(",").filter(function (cid) { return cid; }) : [];
    }

    function bulkSelect(on, scope, artist) {
      if (bulkBusy) {
        showToast("Another selection is still saving. Try again in a moment.", "error");
        applyCounts(lastCounts);
        return Promise.resolve({ ok: false, busy: true });
      }
      bulkBusy = true;
      var requestTab = curTab();
      var requestQuery = curQuery();
      var requestGeneration = loadGen;
      var body = "on=" + (on ? "1" : "0") + "&scope=" + scope +
                 "&tab=" + encodeURIComponent(requestTab) +
                 "&q=" + encodeURIComponent(requestQuery);
      if (scope === "artist") body += "&artist=" + encodeURIComponent(artist || "");
      if (scope === "page") {
        pageBox() && pageBox().querySelectorAll("details[data-artist]").forEach(function (det) {
          groupCandidateIds(det).forEach(function (cid) {
            body += "&cid=" + encodeURIComponent(cid);
          });
        });
      }
      return post("/jobs/" + id + "/select-all", body)
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (c) {
          bulkBusy = false;
          var current = requestTab === curTab() &&
                        requestQuery === curQuery() &&
                        requestGeneration === loadGen;
          var counts = c;
          if (!current && typeof c.filtered_rest === "number") {
            counts = Object.assign({}, c);
            delete counts.filtered_rest;
          }
          applyCounts(counts);
          warnPersistFailed(c);
          var accepted = new Set(c.accepted_cids || []);
          if (current && (scope === "all" || scope === "page")) {
            pageBox() && pageBox().querySelectorAll("details[data-artist]").forEach(function (det) {
              var ids = groupCandidateIds(det);
              if (!ids.length || !ids.every(function (cid) { return accepted.has(cid); })) return;
              det.dataset.selectionOverride = on ? "1" : "0";
              applyGroupChoice(det, on, true);
            });
          }
          updateHideLabels();
          updateArtistChecks();
          return { ok: true, counts: c, current: current, accepted: accepted };
        })
        .catch(function (why) {
          bulkBusy = false;
          if (why === "navigation") {
            return { ok: false, busy: false, navigating: true };
          }
          flashSelectError();
          if (serverUnreachable(why)) {
            // Nothing was saved, and a reload would only reach the offline
            // page, so put the boxes back to the server's last answer.
            applyCounts(lastCounts);
            showToast("Couldn't reach the server, so those choices weren't saved."
              + " Check your connection, then try again.", "error");
            return { ok: false, busy: false, unreachable: true };
          }
          showToast("Couldn't confirm those choices. Reloading the review.", "error");
          reloadToRecover();
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
      // Same reason as saveTick: this box is usually the one with focus.
      var header = det.querySelector("[data-artist-select]");
      if (header) header.setAttribute("aria-busy", "true");
      det.dataset.selectionOverride = on ? "1" : "0";
      applyGroupChoice(det, on, false);
      bulkSelect(on, "artist", det.dataset.artist || "").then(function (result) {
        det._selecting = false;
        if (header) header.removeAttribute("aria-busy");
        if (result.navigating) return;
        if (result.ok && result.current && groupCandidateIds(det).every(function (cid) {
          return result.accepted.has(cid);
        })) {
          applyGroupChoice(det, on, true);
        } else if (!result.ok) {
          delete det.dataset.selectionOverride;
          det.dataset.selectionRecovering = "1";
          det.querySelectorAll(".cb").forEach(function (cb, index) {
            if (index < previousRows.length) cb.checked = previousRows[index];
          });
          restoreGroupHeader(det);
          if (!result.busy && !result.unreachable) {
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

    // "the 1 unselected album" counts at the reader; after "the", one of a
    // thing needs no number.
    function theCount(n) { return n === 1 ? "" : formatCount(n) + " "; }

    // Label each artist action for what it will drop or keep.
    function artistDismissConfirm(who, rest) {
      if (isDownsampleReview) {
        return "Keep the " + theCount(rest) + "unselected " +
          (rest === 1 ? "album" : "albums") + " by " + who +
          " hi-res? You can downsample " + (rest === 1 ? "it" : "them") + " later.";
      }
      return "Dismiss the " + theCount(rest) + "unselected " +
        (rest === 1 ? dismissItemSingular() : dismissItemPlural()) +
        " by " + who + "? You can bring " + (rest === 1 ? "it" : "them") + " back from Dismissed.";
    }
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
        // The rendered confirm counts the moment the page was built; ticks
        // since then change what the button will take. Rewrite it live, and
        // give the button a confirm at all when it reappears on a group that
        // rendered fully selected (the template omits one at rest == 0).
        var rest = total - picked;
        if (rest > 0) {
          btn.setAttribute("data-confirm",
            artistDismissConfirm(det.dataset.artist || "this artist", rest));
          btn.setAttribute("data-confirm-action",
            isDownsampleReview ? "Keep hi-res" : "Dismiss");
        } else {
          btn.removeAttribute("data-confirm");
        }
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
      // One in-flight fetch per box: the restore path and the toggle handler
      // both ask for the same rows in the same tick.
      if (boxEl._loadPromise && !force) return boxEl._loadPromise;
      var generation = (boxEl._loadGeneration || 0) + 1;
      boxEl._loadGeneration = generation;
      boxEl.dataset.loading = "1";
      var p = sessionFetch(boxEl.dataset.itemsUrl)
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
        .catch(function (why) {
          if (boxEl._loadGeneration !== generation) return false;
          delete boxEl.dataset.loading;
          if (why === "navigation") return false;
          showToast("Couldn't load this artist's albums. Try opening it again.", "error");
          return false;
        });
      boxEl._loadPromise = p.then(function (ok) {
        boxEl._loadPromise = null;
        return ok;
      }, function () { boxEl._loadPromise = null; return false; });
      return boxEl._loadPromise;
    }

    var loading = false;
    var loadingPromise = null;
    var pageLoading = false;
    var pendingTab = null;
    var loadGen = 0;

    // Append the next page in place.
    function loadMore(page) {
      if (loading) return loadingPromise || Promise.resolve(false);
      if (pageLoading) return Promise.resolve(false);
      loading = true;
      var gen = ++loadGen;
      var url = "/jobs/" + id + "/review?page=" + (page || 2) +
                "&q=" + encodeURIComponent(curQuery()) +
                "&tab=" + encodeURIComponent(curTab());
      loadingPromise = sessionFetch(url)
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          if (gen !== loadGen) return false;
          if (txt == null) {
            showToast("Couldn't load those results. Try again.", "error");
            return false;
          }
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
          return true;
        })
        .catch(function (why) {
          if (why !== "navigation" && gen === loadGen) {
            showToast("Couldn't load those results. Check your connection.", "error");
          }
          return false;
        });
      var request = loadingPromise;
      return request.then(function (loaded) {
        if (loadingPromise === request) {
          loadingPromise = null;
          loading = false;
        }
        return loaded;
      });
    }

    // The address bar follows the review, so Back has to come back into it
    // rather than leave the page. A tab switch is a history step of its own;
    // the filter is a single step however long the query grows. `mode` picks
    // which: "push" for a tab, "filter" for the filter box, "none" when the
    // browser has already moved us, and "replace" for a reload that did not
    // change where the user is standing.
    function reviewEntry(tab, query, filter) {
      return { ql: { review: id, tab: tab || "", q: query || "", filter: !!filter } };
    }
    function onFilterEntry() {
      var st = window.history.state && window.history.state.ql;
      return !!(st && st.review === id && st.filter);
    }
    function syncUrl(tab, query, mode) {
      if (mode === "none") return;
      if (typeof URL !== "function" || !window.history.replaceState) return;
      var url;
      try { url = new URL(window.location.href); } catch (e) { return; }
      if (tab) url.searchParams.set("tab", tab);
      else url.searchParams.delete("tab");
      if (query) url.searchParams.set("q", query);
      else url.searchParams.delete("q");
      url.searchParams.delete("page");
      var onFilter = onFilterEntry();
      var filterStep = mode === "filter" ? !!query : (mode !== "push" && onFilter);
      var push = mode === "push" || (filterStep && !onFilter);
      if (push && window.history.pushState) {
        window.history.pushState(reviewEntry(tab, query, filterStep), "", url);
      } else {
        window.history.replaceState(reviewEntry(tab, query, filterStep), "", url);
      }
    }

    // Fetch and swap one review page. Requests are generation-tagged and only
    // the newest response may render: a tab click while a fetch is in flight
    // issues its own request instead of being dropped, and the slow old-tab
    // response is discarded on arrival. Otherwise its rows would paint under
    // the newly selected tab while the hidden approval field already points
    // at the new tab.
    function loadPage(page, query, tab, mode) {
      var gen = ++loadGen;
      var requestedQuery = (query || "").trim();
      var requestedTab = tab === undefined ? curTab() : tab;
      loading = false;   // a page swap supersedes any in-flight append
      loadingPromise = null;
      pageLoading = true;
      pendingTab = requestedTab;
      // Pause the outgoing page's Load More until the response lands. A click
      // on it (or the auto-clicking observer) in that gap would bump the
      // generation, discard this page-1 response, and append page 2 beneath
      // the wrong rows. A failed fetch re-enables the still-valid control.
      var staleMore = document.getElementById("review-loadmore");
      var staleMoreButton = staleMore && staleMore.querySelector("button");
      if (staleMoreButton) staleMoreButton.disabled = true;
      var url = "/jobs/" + id + "/review?page=" + (page || 1) +
                "&q=" + encodeURIComponent(requestedQuery) +
                "&tab=" + encodeURIComponent(requestedTab);
      sessionFetch(url)
        .then(function (r) { return r.ok ? r.text() : Promise.reject("response"); })
        .then(function (txt) {
          if (gen !== loadGen) return;
          var host = document.getElementById("review-page");
          if (host) {
            pageLoading = false;
            pendingTab = null;
            var openArtists = {};
            host.querySelectorAll("details[data-artist][open]").forEach(function (d) {
              if (d.dataset.artist) openArtists[d.dataset.artist] = true;
            });
            if (requestedTab) {
              cont.querySelectorAll("[data-review-tab]").forEach(function (b) {
                var active = b.getAttribute("data-review-tab") === requestedTab;
                b.classList.toggle("is-active", active);
                if (active) b.setAttribute("aria-current", "true");
                else b.removeAttribute("aria-current");
              });
              var tabField = document.getElementById("review-tab-field");
              if (tabField) tabField.value = requestedTab;
            }
            syncUrl(requestedTab, requestedQuery, mode);
            loadedQuery = requestedQuery;
            if (filterInput) filterInput.value = requestedQuery;
            host.innerHTML = txt;
            applyCounts(reviewPageCounts(host.querySelector("#review-groups")));
            host.querySelectorAll("details[data-artist]").forEach(function (d) {
              if (d.dataset.artist && openArtists[d.dataset.artist]) d.open = true;
            });
            if (window.htmx) window.htmx.process(host);
            updateHideLabels();
            updateArtistChecks();
            updateDismissRest();
          } else {
            pageLoading = false;
            pendingTab = null;
            if (staleMoreButton) staleMoreButton.disabled = false;
          }
        })
        .catch(function (why) {
          if (why !== "navigation" && gen === loadGen) {
            pageLoading = false;
            pendingTab = null;
            if (staleMoreButton) staleMoreButton.disabled = false;
            if (filterInput) filterInput.value = loadedQuery;
            showToast("Couldn't load those results. Check your connection.", "error");
          }
        });
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
      if (t.closest("[data-artist-select], .ql-review-artist-hit")) {
        e.stopPropagation();
        return;
      }
      var allBtn = t.closest("[data-select-all]");
      if (allBtn) { bulkSelect(allBtn.getAttribute("data-select-all") === "1", "all"); return; }
      var pageBtn = t.closest("[data-select-page]");
      if (pageBtn) { bulkSelect(true, "page"); return; }
      var more = t.closest("[data-load-more]");
      if (more) { loadMore(parseInt(more.getAttribute("data-next-page") || "2", 10)); return; }
      var tabBtn = t.closest("[data-review-tab]");
      if (tabBtn && (
        !tabBtn.classList.contains("is-active") || pageLoading
      )) {
        loadPage(
          1,
          inputQuery(),
          tabBtn.getAttribute("data-review-tab") || "",
          "push"
        );
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
              ? "Keep the " + theCount(rest) + (rest === 1 ? "unselected album" : "unselected albums") + " matching your filter hi-res? You can downsample " + (rest === 1 ? "it" : "them") + " later."
              : "Dismiss the " + theCount(rest) + "unselected " + (rest === 1 ? dismissItemSingular() : dismissItemPlural()) + " matching your filter? You can bring " + (rest === 1 ? "it" : "them") + " back from Dismissed.")
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
            .then(function (r) {
              if (r.status === 403) return Promise.reject("stale");
              return r.json().catch(function () { return {}; }).then(function (body) {
                if (!r.ok) return Promise.reject({ dismissFailure: true, body: body });
                return body;
              });
            })
            .then(function (c) {
              if (c.review_done) { location.reload(); return; }
              dismissRest.disabled = false;
              applyCounts(c);
              loadPage(1, curQuery());
              if (c.finalize_failed) {
                showToast("The albums were dismissed, but the finished review couldn't be saved. Check the data folder and reload.", "error");
              } else {
                showToast(c.hidden ? dismissToast(c.hidden)
                                   : "Nothing to dismiss on this filter.",
                          c.hidden ? "success" : "info");
              }
            })
            .catch(function (why) {
              if (why === "navigation") return;
              if (why === "stale") { pageWentStale(); return; }
              if (serverUnreachable(why)) {
                dismissRest.disabled = false;
                dismissRest.textContent = prev;
                showToast("Couldn't reach the server, so nothing was dismissed."
                  + " Check your connection, then try again.", "error");
                return;
              }
              dismissRest.disabled = true;
              dismissRest.textContent = "Reloading…";
              var hidden = why && why.dismissFailure
                ? parseInt(why.body && why.body.hidden || "0", 10) : 0;
              showToast(
                hidden > 0
                  ? (isDownsampleReview
                      ? "Kept " + plural(hidden, "album") + " hi-res before the rest failed. Reloading the review."
                      : "Dismissed " + countLabel(hidden, dismissItemSingular(), dismissItemPlural()) + " before the rest failed. Reloading the review.")
                  : "Couldn't confirm those dismissals. Reloading the review.",
                "error"
              );
              reloadToRecover();
            });
        });
      });
    }

    // Filter across the full server-side result set.
    var filterTimer = null;
    function cancelFilter() {
      if (filterTimer) clearTimeout(filterTimer);
      filterTimer = null;
    }
    function applyFilter() {
      var query = inputQuery();
      // Emptying the box steps back off the entry the filter pushed, so one
      // Back press undoes the query whatever its length.
      if (!query && onFilterEntry()) { window.history.back(); return; }
      loadPage(1, query, pendingTab === null ? curTab() : pendingTab, "filter");
    }
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        cancelFilter();
        loadGen += 1;
        filterTimer = setTimeout(function () {
          filterTimer = null;
          applyFilter();
        }, 250);
      });
      // A search input's Escape-clear and its native ✕ change the value
      // without firing `input`, which left the box empty and the list filtered.
      filterInput.addEventListener("search", function () {
        cancelFilter();
        loadGen += 1;
        applyFilter();
      });
      // Enter should filter, not submit the review form.
      filterInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
      });
    }

    // Back and Forward move between the entries above. The browser has set
    // the address already, so this only has to make the page match it.
    function onPopState(e) {
      // A queued keystroke is the newer intent; let its load land instead.
      if (filterTimer) return;
      var st = e.state && e.state.ql;
      var tab, query;
      if (st && st.review === id) {
        tab = st.tab || "";
        query = st.q || "";
      } else {
        var params;
        try { params = new URL(window.location.href).searchParams; }
        catch (err) { return; }
        tab = params.get("tab") || "";
        query = (params.get("q") || "").trim();
      }
      var here = pendingTab === null ? curTab() : pendingTab;
      if (tab === here && query === loadedQuery) return;
      if (filterInput) filterInput.value = query;
      loadPage(1, query, tab, "none");
    }
    window.addEventListener("popstate", onPopState);
    // Tag the entry the user arrived on, without touching the address they
    // arrived with, so a Back press onto it can be restored like any other.
    if (window.history.replaceState) {
      window.history.replaceState(
        reviewEntry(curTab(), loadedQuery, false), "", window.location.href
      );
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

    // A refresh or Back rebuilds the page collapsed, so the browser's own
    // scroll restore lands past the end of a list fifteen times shorter and
    // the artist being worked through is closed. Record which groups are open
    // and where the user stood, per job and tab in this browser tab only,
    // reopen the groups, wait for their lazy rows, then put the scroll back.
    function placeKey() { return "ql-review-place:" + id + ":" + curTab(); }
    var placeTimer = null;
    function recordPlace() {
      var open = [];
      cont.querySelectorAll("details[data-artist][open]").forEach(function (d) {
        if (d.dataset.artist) open.push(d.dataset.artist);
      });
      try {
        sessionStorage.setItem(placeKey(),
          JSON.stringify({ open: open, y: window.scrollY, page: curPage() }));
      } catch (e) {}
    }
    function savePlace() {
      if (placeTimer) clearTimeout(placeTimer);
      placeTimer = setTimeout(recordPlace, 250);
    }
    cont.addEventListener("toggle", savePlace, true);
    window.addEventListener("scroll", savePlace, { passive: true });
    window.addEventListener("pagehide", recordPlace);
    // The browser's restore would fight ours with an offset measured on the
    // taller pre-reload page; ours waits for the rows to exist.
    if ("scrollRestoration" in history) history.scrollRestoration = "manual";
    (function restorePlace() {
      var raw = null;
      try { raw = sessionStorage.getItem(placeKey()); } catch (e) {}
      if (!raw) return;
      var place;
      try { place = JSON.parse(raw); } catch (e) { return; }
      if (!place || (!(place.open || []).length && !(place.y > 0))) return;
      var moved = false;
      var moveEvents = ["wheel", "touchmove", "pointerdown", "keydown"];
      function noteMove() { moved = true; }
      moveEvents.forEach(function (name) {
        window.addEventListener(name, noteMove, { passive: true });
      });
      var targetPage = Math.max(1, parseInt(place.page || "1", 10) || 1);
      function loadSavedPages() {
        if (moved || curPage() >= targetPage) return Promise.resolve(!moved);
        var more = document.querySelector("#review-loadmore [data-load-more]");
        var nextPage = more
          ? parseInt(more.getAttribute("data-next-page") || "0", 10) : 0;
        if (!(nextPage > curPage()) || nextPage > targetPage) {
          return Promise.resolve(false);
        }
        return loadMore(nextPage).then(function (loaded) {
          return loaded ? loadSavedPages() : false;
        });
      }
      var pagesReady = loadSavedPages();
      pagesReady.then(function () {
        if (moved) return [];
        var waits = [];
        (place.open || []).forEach(function (name) {
          var esc = window.CSS && CSS.escape ? CSS.escape(name)
                                             : name.replace(/"/g, '\\"');
          var d = cont.querySelector('details[data-artist="' + esc + '"]');
          if (d && !d.open) {
            d.open = true;
            waits.push(loadGroupItems(d, false));
          }
        });
        return Promise.all(waits);
      }).then(function () {
        moveEvents.forEach(function (name) {
          window.removeEventListener(name, noteMove);
        });
        // The user got there first. Don't yank the page out from under them.
        if (moved || !(place.y > 0)) return;
        requestAnimationFrame(function () { window.scrollTo(0, place.y); });
      });
    })();

    updateHideLabels();
    updateArtistChecks();
    syncMaster();

    // Keep review pages in sync across tabs.
    var rsrc = new EventSource("/api/jobs/" + id + "/review-stream");
    var reviewNavigating = false;
    function beginReviewNavigation() { reviewNavigating = true; }
    function shutReview() {
      try { rsrc.close(); } catch (e) {}
      form.removeEventListener("submit", beginReviewNavigation);
      document.body.removeEventListener("qlHidden", onQlHidden);
      document.removeEventListener("htmx:beforeSwap", onReviewSwap);
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("scroll", savePlace);
      window.removeEventListener("pagehide", recordPlace);
      if (placeTimer) clearTimeout(placeTimer);
    }
    form.addEventListener("submit", beginReviewNavigation);
    function onReviewSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === "job-content") shutReview();
    }
    document.addEventListener("htmx:beforeSwap", onReviewSwap);
    rsrc.addEventListener("review", function (e) {
      if ((e.data || "") === "save_failed") {
        showToast("Your latest choice couldn't be saved to disk. It may not survive a restart. Check the data folder.", "error");
        return;
      }
      // Our own change echoing back. The DOM and counts are already current
      // from the action's response, and reloading now would swap the page out
      // from under the user's next click.
      if (reviewNavigating || (e.data || "") === TAB_ID) return;
      loadPage(curPage(), curQuery());
    });
    rsrc.addEventListener("closed", function (e) {
      // An archived/restored review has no live producer, so the server ends
      // the stream with "inactive" right away. Hides and ticks still work
      // there, so keep the count listeners and only stop the dead stream.
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
    normalizeFlashAnnouncements();
    initCoverFallbacks();
    initSearchResults();
    initStreamCards();
    initJobContent();
  }

  var keyboardSearchForm = null;
  var pendingSearchFocus = null;
  var activeSearchRequests = [];
  var latestSearchSubmission = 0;

  function isSearchRequestForm(form) {
    return form && form.matches && form.matches("form")
      && form.getAttribute("hx-target") === "#search-results";
  }

  function searchRequestForm(event) {
    var source = event.detail && event.detail.elt;
    var form = source && (source.matches("form") ? source : source.closest("form"));
    return isSearchRequestForm(form) ? form : null;
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!isSearchRequestForm(form)) return;
    latestSearchSubmission += 1;
    form.dataset.searchSubmission = latestSearchSubmission;
  }, true);

  document.addEventListener("htmx:beforeRequest", function (event) {
    var form = searchRequestForm(event);
    if (!form || !event.detail.xhr) return;
    saveSearchSnapshot();
    var generation = parseInt(form.dataset.searchSubmission || "0", 10);
    if (!generation || activeSearchRequests.some(function (xhr) {
      return xhr.qlSearchSubmission === generation;
    })) {
      latestSearchSubmission += 1;
      generation = latestSearchSubmission;
      form.dataset.searchSubmission = generation;
    }
    event.detail.xhr.qlSearchSubmission = generation;
    var previousRequests = activeSearchRequests.slice();
    if (activeSearchRequests.indexOf(event.detail.xhr) === -1) {
      activeSearchRequests.push(event.detail.xhr);
    }
    previousRequests.forEach(function (xhr) {
      try { xhr.abort(); } catch (error) { /* request already finished */ }
    });
    var results = document.getElementById("search-results");
    var status = document.getElementById("search-status");
    if (results) results.setAttribute("aria-busy", "true");
    if (status) status.textContent = "Searching…";
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "search-results" || !(pendingSearchRestoreY > 0)) return;
    searchPositionRestoring = true;
    document.documentElement.classList.add("ql-search-restoring");
    if (searchRestoreFallback) clearTimeout(searchRestoreFallback);
    searchRestoreFallback = setTimeout(function () {
      document.documentElement.classList.remove("ql-search-restoring");
      searchPositionRestoring = false;
    }, 2000);
  });

  function restoreSearchFormFromUrl() {
    var form = document.querySelector(".ql-search-form");
    if (!form || typeof URL !== "function") return;
    try {
      var url = new URL(location.href);
      var kind = url.searchParams.get("kind");
      if (["artist", "album", "track"].indexOf(kind) === -1) kind = "artist";
      var query = form.querySelector('input[name="q"]');
      if (query) query.value = url.searchParams.get("q") || "";
      form.querySelectorAll('input[name="kind"]').forEach(function (radio) {
        radio.checked = radio.value === kind;
      });
    } catch (error) { /* leave the form alone if the address cannot be read */ }
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    if (!event.detail.xhr) return;
    var failed = event.detail.successful !== true;
    var index = activeSearchRequests.indexOf(event.detail.xhr);
    if (index === -1) return;
    activeSearchRequests.splice(index, 1);
    if (activeSearchRequests.length) return;
    var results = document.getElementById("search-results");
    var status = document.getElementById("search-status");
    if (results) results.setAttribute("aria-busy", "false");
    if (status) status.textContent = "";
    if (failed && event.detail.xhr.qlSearchSubmission === latestSearchSubmission) {
      restoreSearchFormFromUrl();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var button = event.target && event.target.closest && event.target.closest("#search-results button[type=submit]");
    if (!button || !button.form || !button.form.matches(".ql-result-action-form, .ql-search-back-form")) return;
    keyboardSearchForm = button.form;
  }, true);

  document.addEventListener("pointerdown", function () {
    keyboardSearchForm = null;
  }, true);

  document.addEventListener("htmx:beforeRequest", function (event) {
    var source = event.detail && event.detail.elt;
    var form = source && (source.matches("form") ? source : source.closest("form"));
    if (!form || form !== keyboardSearchForm) return;
    var artist = form.querySelector('input[name="artist_id"]');
    pendingSearchFocus = {
      form: form,
      returning: form.classList.contains("ql-search-back-form"),
      artistId: artist ? artist.value : form.dataset.searchArtistId
    };
    keyboardSearchForm = null;
  });

  function restoreSearchFocus(event) {
    var target = event.detail && event.detail.target;
    if (!pendingSearchFocus || !target || target.id !== "search-results") return;
    var pending = pendingSearchFocus;
    pendingSearchFocus = null;
    requestAnimationFrame(function () {
      var control = null;
      if (pending.returning && pending.artistId) {
        Array.prototype.some.call(target.querySelectorAll('input[name="artist_id"]'), function (input) {
          if (input.value !== pending.artistId) return false;
          control = input.form && input.form.querySelector("button[type=submit]");
          return !!control;
        });
      } else {
        control = target.querySelector(".ql-search-back");
      }
      (control || target).focus();
    });
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    if (!pendingSearchFocus || !event.detail || event.detail.successful !== false) return;
    var source = event.detail.elt;
    var form = source && (source.matches("form") ? source : source.closest("form"));
    if (form !== pendingSearchFocus.form) return;
    var control = form.querySelector("button[type=submit]");
    pendingSearchFocus = null;
    requestAnimationFrame(function () {
      if (control && document.contains(control)) control.focus();
    });
  });

  function revealSearchFeedback(event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "search-results") return;
    requestAnimationFrame(function () {
      var empty = target.querySelector(".ql-search-empty");
      var tabbar = document.querySelector(".ql-tabbar");
      if (!empty || !tabbar || getComputedStyle(tabbar).display === "none") return;
      var feedbackBottom = empty.getBoundingClientRect().bottom;
      var navigationTop = tabbar.getBoundingClientRect().top;
      if (feedbackBottom > navigationTop) {
        window.scrollBy(0, feedbackBottom - navigationTop + 12);
      }
    });
  }

  function announceDiagnosticsSwap(event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "diagnostics-list") return;
    // Recheck alone. Restore, Remove and Delete swap the same list, and each
    // already says what it did; a second, generic notice beside a refusal
    // reads like the action went through.
    var req = event.detail && event.detail.requestConfig;
    if (!req || String(req.verb).toLowerCase() !== "get") return;
    var status = document.getElementById("diagnostics-status");
    if (status) status.textContent = "Diagnostics refreshed.";
    // The status line above is sr-only; a sighted user pressing Recheck on a
    // long diagnostics list saw the button change nothing and nothing else
    // say the check ran.
    showToast("Diagnostics refreshed.", "info");
  }

  function rememberSearchSwap(event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "search-results") return;
    if (!target.querySelector("[data-search-results-root]")) {
      searchExitSave = saveSearchSnapshot;
    }
    searchSnapshotsInvalidated = false;
    saveSearchSnapshot();
    if (searchPositionRestoring || pendingSearchRestoreY !== null) {
      finishSearchRestore(readSearchRecord(searchResponseState(target)));
    }
  }

  // Re-scan swapped content for flashes and streams.
  document.addEventListener("htmx:afterSwap", initAll);
  document.addEventListener("htmx:afterSwap", rememberSearchSwap);
  document.addEventListener("htmx:afterSwap", restoreSearchFocus);
  document.addEventListener("htmx:afterSwap", revealSearchFeedback);
  document.addEventListener("htmx:afterSwap", announceDiagnosticsSwap);
  // A restored artist deep-link replays itself once on load; drop the artist it
  // carried afterwards so the next search the user types is its own.
  document.addEventListener("htmx:afterRequest", function (e) {
    var form = e.target && e.target.closest && e.target.closest(".ql-search-form");
    if (!form) return;
    if (e.detail && e.detail.successful === false) return;
    form.querySelectorAll("[data-deep-link]").forEach(function (el) { el.remove(); });
  });
  // One album downloaded from a search row gets the same treatment as a bulk
  // download: bring up the Background-work strip. Its toast clears after a few
  // seconds, and without the strip the page the user is standing on stops
  // mentioning the download at all.
  document.addEventListener("htmx:afterRequest", function (e) {
    var source = e.target;
    if (!source || !source.closest) return;
    if (!source.closest("[data-search-download-form]")) return;
    if (e.detail && e.detail.successful === false) return;
    if (window.htmx && document.getElementById("dashboard-active")) {
      window.htmx.ajax("GET", "/",
        { target: "#dashboard-active", swap: "outerHTML", select: "#dashboard-active" });
    }
    if (window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
  });
  cleanFlashUrl();
  // The sticky chrome (review summary row, review footer, toasts, search bar)
  // offsets itself by the header and tab-bar heights. Those vary with the
  // safe-area insets and the font, so hard-coded rem guesses left see-through
  // bands that sliced rows. Measure the real elements and let the CSS read
  // the result; the stylesheet keeps the old guesses as fallbacks for the
  // moment before this runs.
  (function () {
    function apply() {
      var root = document.documentElement;
      var bar = document.querySelector(".ql-mobilebar");
      var tabs = document.querySelector(".ql-tabbar");
      if (bar && bar.offsetHeight) {
        root.style.setProperty("--ql-mobilebar-h", bar.offsetHeight + "px");
      }
      if (tabs && tabs.offsetHeight) {
        root.style.setProperty("--ql-tabbar-h", tabs.offsetHeight + "px");
      }
    }
    window.addEventListener("resize", apply);
    window.addEventListener("load", apply);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", apply);
    } else {
      apply();
    }
  })();
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
