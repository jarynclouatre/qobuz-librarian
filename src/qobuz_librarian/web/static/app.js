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
  // Each tab's results for the query in the box, so switching back to a tab
  // shows them again without asking Qobuz.
  var keptSearchViews = {};
  var libraryRevision = document.documentElement.dataset.libraryRevision;
  var failedLibraryReads = new WeakMap();

  function searchStateId(kind, query, artistId, albumId) {
    kind = ["artist", "album", "track"].indexOf(kind) >= 0 ? kind : "artist";
    var state = kind + "|" + String(query || "").trim();
    if (artistId) state += "|artist:" + String(artistId).trim();
    if (kind === "album" && albumId) state += "|album:" + String(albumId).trim();
    return state;
  }

  // htmx restores only entries it marked, and a page opened from its
  // address carries no mark until htmx pushes from it.
  function pushSearchUrl(url) {
    if (!(history.state && history.state.htmx)) {
      history.replaceState({ htmx: true }, "", location.href);
    }
    history.pushState({ htmx: true }, "", url);
  }

  function searchStateFromUrl() {
    if (location.pathname !== "/") return "";
    try {
      var params = new URL(location.href).searchParams;
      var query = (params.get("q") || "").trim();
      if (!query && !params.get("album_id")) return "";
      return searchStateId(
        params.get("kind") || "artist",
        query,
        params.get("artist_id") || "",
        params.get("album_id") || ""
      );
    } catch (e) {
      return "";
    }
  }

  function searchStorageKey(state) {
    return SEARCH_STATE_PREFIX + state;
  }

  function searchSnapshotKey(state) {
    var params = new URLSearchParams(location.search);
    if (!params.has("back_kind")) return state;
    return JSON.stringify([state, params.get("back_kind"), params.get("back_q"),
      params.get("back_album_id"), params.get("back_track_q")]);
  }

  function searchSnapshotStorageKey(state) {
    return SEARCH_SNAPSHOT_PREFIX + searchSnapshotKey(state);
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
      if (!validSearchRecord(record) || typeof record.html !== "string"
          || record.libraryRevision !== libraryRevision) {
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

  function writeSearchSnapshot(state, html, revision) {
    if (!state || !html || html.length > SEARCH_SNAPSHOT_MAX_CHARS) {
      removeSearchSnapshot(state);
      return false;
    }
    clearSearchSnapshots();
    try {
      sessionStorage.setItem(searchSnapshotStorageKey(state), JSON.stringify({
        html: html,
        libraryRevision: revision,
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
    keptSearchViews = {};
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

  var queueBadgePollingStarted = false;
  function initQueueBadgePolling() {
    if (queueBadgePollingStarted
        || !document.querySelector(".js-queue-badge")) return;
    queueBadgePollingStarted = true;
    var inFlight = null;
    var controller = null;
    var queueSignatureKey = "ql-queue-count-signature";

    // The phone tab carries one mark: the count while work is in flight,
    // tinted when a finished job needs attention, and a dot when nothing is.
    function apply(n, attention, reviews) {
      document.querySelectorAll(".js-queue-badge").forEach(function (badge) {
        badge.classList.toggle("is-attention", !!attention);
        if (!n) {
          badge.classList.add("hidden");
          badge.textContent = "";
          return;
        }
        badge.classList.remove("hidden");
        badge.textContent = n > 99 ? "99+" : String(n);
      });
      document.querySelectorAll("[data-attention-badge]").forEach(function (badge) {
        var solo = badge.hasAttribute("data-attention-solo");
        badge.classList.toggle("hidden", !attention || (solo && !!n));
        if (!attention) return;
        var label = attention + " finished job"
          + (attention === 1 ? " needs" : "s need") + " attention";
        badge.setAttribute("aria-label", label);
        if (badge.hasAttribute("title")) badge.setAttribute("title", label);
      });
      // More carries one dot for all the tools in its sheet.
      document.querySelectorAll("[data-review-badge]").forEach(function (badge) {
        var ready = badge.dataset.reviewBadge.split(" ").some(function (name) {
          return reviews[name];
        });
        badge.classList.toggle("hidden", !ready);
      });
    }

    function refresh() {
      if (document.hidden || inFlight) return;
      controller = typeof AbortController === "function"
        ? new AbortController() : null;
      var request = fetch("/api/queue/count", {
        headers: { "Accept": "application/json" },
        signal: controller ? controller.signal : undefined,
      });
      inFlight = request;
      request
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || inFlight !== request || document.hidden) return;
          var signature = data.signature
            || String(data.count) + ":" + (data.running ? "1" : "0");
          try {
            var previous = sessionStorage.getItem(queueSignatureKey);
            if (previous && previous !== signature
                && window.qlInvalidateSearchSnapshots) {
              window.qlInvalidateSearchSnapshots();
            }
            sessionStorage.setItem(queueSignatureKey, signature);
          } catch (e) {}
          apply(data.count, data.attention || 0, data.reviews || {});
          if (data.library_revision !== undefined) refreshLibraryResults(String(data.library_revision));
          // Redraw when the rendered rows no longer match the live queue.
          var body = document.getElementById("queue-body");
          var rows = data.rows || String(data.count);
          var moved = body && body.dataset.queueRows !== rows;
          if (body && window.htmx
              && (moved
                  || body.querySelectorAll("[data-queue-row]").length
                     !== data.count)) {
            window.htmx.ajax("GET", body.dataset.queueSrc || "/queue",
              { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
          }
        })
        .catch(function () {})
        .then(function () {
          if (inFlight === request) {
            inFlight = null;
            controller = null;
          }
        });
    }

    refresh();
    setInterval(refresh, 12000);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        if (controller) controller.abort();
        controller = null;
        inFlight = null;
      } else {
        failedLibraryReads = new WeakMap();
        refresh();
      }
    });
    window.qlRefreshQueueBadge = refresh;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initQueueBadgePolling);
  } else {
    initQueueBadgePolling();
  }

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
    discover: "/discover",
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
    if (tab === "discover") {
      return /^\/discover(?:\/(?:genres|search|favourites|hidden))?\/?$/.test(path);
    }
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
    if (active && restore && NAV_DEFAULTS[restore.tab] && restore.tab !== active) {
      // The remembered page sent us elsewhere (a cleared job, say); the tab
      // starts from its own page next time.
      try { sessionStorage.removeItem(NAV_STATE_PREFIX + restore.tab); } catch (e) {}
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

    // A grid tile holds its other versions' rows; leave their state to them.
    var own = function (el) { return el.closest("[data-search-item]") === item; };
    var mine = function (selector) {
      return Array.prototype.filter.call(item.querySelectorAll(selector), own);
    };
    mine(".ql-search-quality-status, [data-search-download-form]").forEach(function (el) {
      el.hidden = unavailable;
    });
    mine("[data-search-pending]").forEach(function (chip) {
      chip.hidden = !unavailable;
      chip.textContent = state === "scanning" ? "In current scan" : "Queued";
    });

    var form = mine("[data-search-download-form]")[0];
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

  function searchAvailabilityItems() {
    var items = Array.prototype.slice.call(
      document.querySelectorAll("[data-search-item]")
    );
    document.querySelectorAll("template[data-search-view-template]").forEach(
      function (template) {
        items = items.concat(Array.prototype.slice.call(
          template.content.querySelectorAll("[data-search-item]")
        ));
      }
    );
    return items;
  }

  function markSearchDownloadQueued(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var peers = item ? [item] : [];
    if (key) {
      if (queuedDuringAvailabilityCheck) {
        queuedDuringAvailabilityCheck.add(key);
        refreshSearchAvailabilityAgain = true;
      }
      peers = searchAvailabilityItems().filter(function (peer) {
        return peer.dataset.searchKey === key;
      });
    }
    peers.forEach(function (peer) {
      setSearchItemAvailability(peer, "queued");
    });
    var root = item && item.closest("[data-search-results-root]");
    if (root) root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
  }

  var queuedDuringAvailabilityCheck = null;
  var refreshSearchAvailabilityAgain = false;

  function refreshSearchAvailability() {
    if (!searchAvailabilityItems().length) return;
    if (queuedDuringAvailabilityCheck) {
      refreshSearchAvailabilityAgain = true;
      return;
    }
    queuedDuringAvailabilityCheck = new Set();
    sessionFetch("/api/search/availability", {
      headers: { "Accept": "application/json" },
    })
      .then(function (response) { return response.ok ? response.json() : Promise.reject(); })
      .then(function (data) {
        var queued = new Set(data.queued || []);
        var scanning = new Set(data.scanning || []);
        // A download that has finished putting the whole album on disk owns it
        // now, so the row does not offer that download again. Only a row this
        // page showed queued takes it: one drawn after the download finished
        // already says what is on disk, and the files may have gone since.
        var owned = new Set(data.owned || []);
        searchAvailabilityItems().forEach(function (item) {
          var key = item.dataset.searchKey || "";
          if (queuedDuringAvailabilityCheck.has(key)) return;
          if (owned.has(key) && item.dataset.queued === "1") {
            markSearchItemOwned(item);
            return;
          }
          setSearchItemAvailability(item,
            queued.has(key) ? "queued" : scanning.has(key) ? "scanning" : "available");
        });
        document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
          root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
        });
        if (!refreshSearchAvailabilityAgain) {
          searchSnapshotsInvalidated = false;
        }
      })
      .catch(function () {})
      .then(function () {
        queuedDuringAvailabilityCheck = null;
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
      // The label is a table cell; emptying it keeps the columns in place.
      if (label) label.replaceChildren();
      else checkbox.remove();
    }
    // Draw the row as the server draws an owned one, so the track count the
    // row had before the download does not stay beside "In library".
    var own = function (el) { return el.closest("[data-search-item]") === item; };
    Array.prototype.filter.call(item.querySelectorAll("[data-search-pending]"), own)
      .forEach(function (chip) { chip.remove(); });
    var statuses = Array.prototype.filter.call(
      item.querySelectorAll(".ql-search-quality-status"), own);
    statuses.forEach(function (status) {
      var notes = Array.prototype.slice.call(
        status.querySelectorAll("[data-unavailable-note]"));
      status.replaceWith.apply(status, [ownedLabel(), " "].concat(notes));
    });
    var download = Array.prototype.find.call(
      item.querySelectorAll("[data-search-download-form]"), own);
    if (download) {
      if (statuses.length) {
        download.remove();
      } else {
        download.parentElement
          .querySelectorAll(".ql-partial-label, .ql-other-version-label:not([data-unavailable-note])")
          .forEach(function (label) { label.remove(); });
        download.replaceWith(ownedLabel());
      }
    }
    // A tracklist fetched before the download still shows the old marks.
    var refetched = {};
    item.querySelectorAll("[data-tracks-toggle]").forEach(function (toggle) {
      var id = toggle.getAttribute("aria-controls");
      if (!own(toggle) || refetched[id]) return;
      refetched[id] = true;
      var panel = document.querySelector(toggle.getAttribute("hx-target"));
      if (!panel || !panel.firstElementChild) return;
      if (panel.classList.contains("hidden") || panel.closest("dialog:not([open])")) panel.replaceChildren();
      else window.htmx.ajax("GET", toggle.getAttribute("hx-get"), { target: panel, swap: "innerHTML" });
    });
  }

  function ownedLabel() {
    var owned = document.createElement("span");
    owned.className = "ql-owned-label";
    owned.textContent = "In library";
    return owned;
  }

  function markSearchDownloadOwned(form) {
    if (!form || !form.matches || !form.matches("[data-search-download-form]")) return;
    var item = form.closest("[data-search-item]");
    var key = item && item.dataset.searchKey;
    var peers = item ? [item] : [];
    if (key) {
      peers = searchAvailabilityItems().filter(function (peer) {
        return peer.dataset.searchKey === key;
      });
    }
    peers.forEach(markSearchItemOwned);
    var root = item && item.closest("[data-search-results-root]");
    if (root) root.dispatchEvent(new CustomEvent("qlSearchAvailabilityChanged"));
  }

  document.addEventListener("htmx:beforeSend", function (evt) {
    var form = evt.detail.elt;
    if (form.matches("[data-search-download-form]") && form.closest(".ql-version-sheet[open]")) {
      dismissSheet();
    }
  });

  // Disable a search-result button only once the album is queued, by this
  // press or an earlier one. A press refused because the album is on disk
  // already draws the row owned: a download that finished while the tab was
  // hidden left it offering one.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-queue-button]")) return;
    if (!evt.detail || !evt.detail.successful) return;
    var xhr = evt.detail.xhr;
    if (!xhr) return;
    var outcome = xhr.getResponseHeader("X-QL-Download-Outcome");
    if (outcome === "owned") markSearchDownloadOwned(form);
    else if (outcome === "queued" || outcome === "duplicate") markSearchDownloadQueued(form);
  });

  // Requests the page makes on its own, on a timer. They report nothing to the
  // user when they fail: the status line they feed simply stops moving.
  function fromBackgroundPoll(evt) {
    var elt = evt.detail && evt.detail.elt;
    var trigger = elt && elt.getAttribute && elt.getAttribute("hx-trigger");
    return !!trigger && /(^|[\s,])every\s/.test(trigger);
  }

  function showRequestError(event, message) {
    if (fromBackgroundPoll(event)) return;
    if (libraryRefreshRevision(event.detail)) return;
    if (event.detail.target && event.detail.target.matches(".ql-track-sheet-list")) return;
    var form = searchRequestForm(event);
    if (form && form.matches(".ql-search-form")) {
      if (event.detail.xhr.qlSearchSubmission !== latestSearchSubmission) return;
      clearSearchMoreError();
      var error = document.createElement("p");
      error.className = "ql-notice ql-notice-warning ql-search-error";
      error.setAttribute("role", "alert");
      error.textContent = message;
      event.detail.target.replaceChildren(error);
      searchExitSave = saveSearchSnapshot;
      return;
    }
    showToast(message, "error");
  }

  // Surface failed htmx requests instead of failing silently. A short
  // plain-text body is the route speaking (e.g. "couldn't save that
  // dismissal"); anything longer or HTML-shaped gets the generic line.
  document.addEventListener("htmx:responseError", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    var body = (xhr && xhr.responseText || "").trim();
    var msg = (body && body.length <= 200 && body.indexOf("<") === -1)
      ? body : "That didn't go through. Try again in a moment.";
    showRequestError(evt, msg);
  });
  document.addEventListener("htmx:sendError", function (evt) {
    showRequestError(evt, "Couldn't reach the server. Check your connection and try again.");
  });

  // A control htmx disables while its request runs drops keyboard focus to
  // the page body. Hand it back when the request ends, or, when the control
  // stays disabled (a search result that is now queued), to the control
  // before it.
  function previousFocusable(el) {
    var found = null;
    document.querySelectorAll(
      "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), summary"
    ).forEach(function (candidate) {
      if (candidate.getClientRects().length
          && candidate.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
        found = candidate;
      }
    });
    return found;
  }
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    var active = document.activeElement;
    if (!elt || !elt.hasAttribute || !elt.hasAttribute("hx-disabled-elt")) return;
    if (active && active !== document.body && elt.contains(active)) evt.detail.xhr.qlFocus = active;
  });
  document.addEventListener("htmx:afterRequest", function (evt) {
    var lost = evt.detail && evt.detail.xhr && evt.detail.xhr.qlFocus;
    if (!lost) return;
    setTimeout(function () {
      var now = document.activeElement;
      if ((now && now !== document.body) || !lost.isConnected) return;
      var target = lost.disabled ? previousFocusable(lost) : lost;
      if (target) target.focus({ preventScroll: true });
    });
  });

  // Animate a fully hidden artist group before htmx removes it. Dismissals
  // target the group's positioning SHELL (the dismiss button lives beside the
  // <details>, not inside it), so match the shell as well as a bare details.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var t = evt.detail && evt.detail.target;
    if (!t || !t.matches) return;
    if (!t.matches("details[data-artist]")
        && !(t.matches(".ql-review-group-shell")
             && t.querySelector("details[data-artist]"))) return;
    if ((evt.detail.serverResponse || "").trim() !== "") return;
    collapse(t);
  });

  // The x in a search or filter box, and Escape in one with text, empty the
  // box and leave the cursor in it, so a phone keeps its keyboard up. The
  // page then reacts as it does to any emptied box.
  function clearButtonFor(input) {
    var next = input && input.nextElementSibling;
    return next && next.matches("[data-clear-field]") ? next : null;
  }
  function clearField(input) {
    // A filter the server applied clears the way it was set: by asking again.
    var filtered = input.hasAttribute("data-clear-submits") && input.defaultValue;
    input.value = "";
    input.focus();
    input.dispatchEvent(new Event("input", { bubbles: true }));
    if (filtered || isSearchRequestForm(input.form)) input.form.requestSubmit();
  }
  document.addEventListener("mousedown", function (evt) {
    if (evt.target.closest && evt.target.closest("[data-clear-field]")) evt.preventDefault();
  });
  document.addEventListener("click", function (evt) {
    var button = evt.target.closest && evt.target.closest("[data-clear-field]");
    if (button && button.previousElementSibling) clearField(button.previousElementSibling);
  });

  // Global search, escape, and g-then-key navigation shortcuts.
  var gPending = false;
  var gTimer = null;
  document.addEventListener("keydown", function (evt) {
    if (evt.ctrlKey || evt.metaKey || evt.altKey) return;
    var overlay = document.querySelector("dialog[open], .ql-mobile-drawer[open]");
    if (overlay) {
      gPending = false;
      if (evt.key === "Escape" && overlay.matches(".ql-mobile-drawer")) {
        evt.preventDefault();
        dismissSheet();
      }
      return;
    }
    var t = evt.target;
    var inField = !!(t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" ||
                           t.tagName === "SELECT" || t.isContentEditable));
    if (evt.key === "Escape") {
      if (window.qlDismissAllFlashes) window.qlDismissAllFlashes();
      if (inField && t.value && clearButtonFor(t)) {
        evt.preventDefault();
        clearField(t);
        return;
      }
      // A search box has already used Escape to clear itself; blurring on top
      // of that costs a tap to get back in and carry on typing.
      var isSearchBox = !!(t && t.tagName === "INPUT" && t.type === "search");
      if (inField && !isSearchBox && t.blur) t.blur();
      return;
    }
    if (inField) return;
    if (evt.key === "/") {
      evt.preventDefault();
      var box = Array.from(document.querySelectorAll('#review-filter, input[name="q"]:not([type="hidden"])'))
        .find(function (input) { return !input.disabled && input.getClientRects().length; });
      if (!box) {
        if (activeNavTab() !== "search") navigateToTab("search", { restorePosition: false, hash: "search" });
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
  // Settings sections stay shut until they are wanted, so a link or a save
  // that names one has to open it, and the Advanced drawer too when the link
  // names a field inside it. A heading is in its summary, so the browser can
  // land on it either way; this only unfolds what is under it.
  function openSettingsSectionFromHash() {
    var hash = window.location.hash;
    if (hash.length < 2) return;
    var target;
    try { target = document.querySelector(hash); } catch (e) { return; }
    if (!target || !target.closest) return;
    var opened = false;
    for (var d = target.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) {
      if (!d.open) { d.open = true; opened = true; }
    }
    if (opened) target.scrollIntoView();
  }
  window.addEventListener("hashchange", openSettingsSectionFromHash);

  // A form's Referer omits the fragment. If its session ends, sign-in can
  // still return to the Settings section that held the form.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    var section = form.closest(".ql-settings-section");
    if (!section || form.method !== "post" || !form.hasAttribute("action")) return;
    var action = new URL(form.action);
    action.searchParams.set("section", section.querySelector("h2").id);
    form.action = action.href;
  });

  function focusSearchFromHash() {
    if (window.location.hash !== "#search") return;
    var landBox = document.querySelector('input[name="q"]');
    if (landBox) landBox.focus();
  }
  function initFromHash() {
    focusSearchFromHash();
    openSettingsSectionFromHash();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initFromHash);
  } else {
    initFromHash();
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
        '<span class="ql-spinner ql-inline-spinner" aria-hidden="true"></span> ';
      b.appendChild(document.createTextNode(
        b.dataset.busyLabel || form.dataset.busyLabel || "Starting…"));
    }, 0);
  });

  // The Library refresh lands back on the review tab and filter the address
  // bar holds now, which tab switches and the filter keep current.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-review-place]")) return;
    var params = new URLSearchParams(location.search);
    ["tab", "q"].forEach(function (key) {
      var field = form.elements.namedItem(key);
      if (field) field.value = params.get(key) || "";
    });
  });

  // Bring back and Remove reload the list they act on. The reload reopens
  // the same groups and puts the row that was first on screen back where it
  // was, since a notice or a removed row above can shift everything.
  var KEEP_PLACE_KEY = "ql-keep-place";
  function shownPlaces() {
    return Array.prototype.filter.call(
      document.querySelectorAll("[data-place]"),
      function (el) { return el.getClientRects().length > 0; });
  }
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form || !form.matches || !form.matches("form[data-keep-place]")) return;
    var own = form.closest("[data-place]");
    var anchor = shownPlaces().filter(function (el) {
      return !(own && own.contains(el)) && el.getBoundingClientRect().top >= 0;
    })[0];
    var open = [];
    document.querySelectorAll("details[open][data-place]").forEach(function (d) {
      open.push(d.dataset.place);
    });
    try {
      sessionStorage.setItem(KEEP_PLACE_KEY, JSON.stringify({
        path: location.pathname,
        y: Math.round(window.scrollY || 0),
        open: open,
        place: anchor ? anchor.dataset.place : null,
        top: anchor ? anchor.getBoundingClientRect().top : 0,
      }));
    } catch (e) {}
  });

  function restoreKeptPlace() {
    var kept = null;
    try {
      kept = JSON.parse(sessionStorage.getItem(KEEP_PLACE_KEY) || "null");
      sessionStorage.removeItem(KEEP_PLACE_KEY);
    } catch (e) {}
    if (!kept || kept.path !== location.pathname) return;
    document.querySelectorAll("details[data-place]").forEach(function (d) {
      if ((kept.open || []).indexOf(d.dataset.place) !== -1) d.open = true;
    });
    var anchor = shownPlaces().filter(function (el) {
      return el.dataset.place === kept.place;
    })[0];
    if (!anchor) {
      restoreNavPosition(kept.y);
      return;
    }
    requestAnimationFrame(function () {
      window.scrollTo(0, window.scrollY + anchor.getBoundingClientRect().top - kept.top);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreKeptPlace);
  } else {
    restoreKeptPlace();
  }

  function restoreBusySubmitButtons(root) {
    (root || document).querySelectorAll("button[data-busy-html]").forEach(function (b) {
      b.disabled = false;
      b.classList.remove("is-disabled");
      b.innerHTML = b.dataset.busyHtml;
      delete b.dataset.busyHtml;
    });
  }

  window.addEventListener("pageshow", function (evt) {
    if (!evt.persisted) return;
    restoreBusySubmitButtons();
  });

  // A changed setting can be screens away from its form's Save, so a bar with
  // a Save of its own stays up until the form is back as it was loaded.
  function fieldChanged(field) {
    if (field.type === "checkbox") return field.checked !== field.defaultChecked;
    if (field.tagName === "SELECT") {
      var loaded = field.querySelector("option[selected]") || field.options[0];
      return field.value !== loaded.value;
    }
    return field.tagName === "INPUT" && field.value !== field.defaultValue;
  }
  function syncUnsavedBar() {
    var bar = document.querySelector("[data-unsaved-bar]");
    if (!bar) return;
    var form = document.getElementById(bar.dataset.unsavedBar);
    var saved = JSON.parse(form.dataset.savedValues || "{}");
    var changed = Array.prototype.some.call(form.elements, function (field) {
      if (field.name in saved) {
        return (field.type === "checkbox" ? field.checked : field.value) !== saved[field.name];
      }
      return fieldChanged(field);
    });
    bar.classList.toggle("hidden", !changed);
  }
  document.addEventListener("input", syncUnsavedBar);
  document.addEventListener("change", syncUnsavedBar);
  window.addEventListener("pageshow", syncUnsavedBar);

  document.addEventListener("input", function (evt) {
    var field = evt.target;
    if (field.getAttribute("aria-invalid") !== "true") return;
    var note = document.getElementById("refusal-" + field.name);
    if (!note) return;
    field.removeAttribute("aria-invalid");
    field.removeAttribute("aria-describedby");
    note.remove();
  });

  // An htmx form the response left in place (a refusal, an error, no
  // answer) gets its button back.
  document.addEventListener("htmx:afterRequest", function (evt) {
    var form = evt.detail && evt.detail.elt;
    form = form && form.closest ? form.closest("form[data-busy-submit]") : null;
    if (form && form.isConnected) restoreBusySubmitButtons(form);
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", function (evt) {
      if (!evt.data || evt.data.type !== "ql-post-failed") return;
      restoreBusySubmitButtons();
      showToast(
        "Couldn't reach the server. Check your connection and try again.",
        "error"
      );
    });
  }

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
    if (d.open) return Promise.resolve(false);
    // The previous close event can still be queued when the button is pressed again.
    if (d.qlConfirmResult) {
      return d.qlConfirmResult.then(function () { return window.qlConfirm(msg, opts); });
    }
    d.qlConfirmResult = new Promise(function (resolve) {
      d.querySelector("[data-confirm-text]").textContent = msg;
      var okButton = d.querySelector("[data-confirm-ok]");
      okButton.textContent = opts.action || "Continue";
      okButton.toggleAttribute("data-irreversible", !!opts.irreversible);
      okButton.classList.toggle("ql-btn-primary", !opts.irreversible);
      var choice = false;
      okButton.onclick = function () { dismissSheet(function () { choice = true; }); };
      var cancelButton = d.querySelector("[data-confirm-cancel]");
      // Asked whether to cancel a job, a button labelled "Cancel" reads as yes.
      cancelButton.textContent = opts.dismiss || "Cancel";
      cancelButton.onclick = function () { dismissSheet(); };
      // An action in the review's menu closes the menu as the dialog opens,
      // so the dialog has no visible button to hand focus back to.
      var menu = document.activeElement.closest("[data-review-more]");
      function leavePage() {
        d.close();
      }
      window.addEventListener("pagehide", leavePage);
      d.addEventListener("close", function h() {
        d.removeEventListener("close", h);
        delete d.qlConfirmResult;
        window.removeEventListener("pagehide", leavePage);
        if (menu && !document.activeElement.getClientRects().length) {
          menu.querySelector(".ql-review-more-toggle").focus();
        }
        resolve(choice);
      });
      pushSheet(d.id);
      d.showModal();
    });
    return d.qlConfirmResult;
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
    // Below data-confirm-min the count in the label is small enough to act on.
    var min = parseInt(el.getAttribute("data-confirm-min") || "0", 10);
    var shown = (el.textContent || "").match(/\d[\d.,\u00a0\u202f']*/);
    if (min && shown && parseInt(shown[0].replace(/\D/g, ""), 10) < min) return;
    evt.preventDefault();
    evt.stopPropagation();
    var msg = el.getAttribute("data-confirm");
    var dismissalKey = el.matches("[data-hide]") ? confirmControlKey(el) : null;
    // A box that changes what the control does gives it another question.
    var box = document.getElementById(el.getAttribute("data-confirm-checked") || "");
    if (box && box.checked) msg = el.getAttribute("data-confirm-checked-text");
    if (msg.indexOf("{count}") !== -1 || msg.indexOf("{s}") !== -1) {
      var m = (el.textContent || "").match(/[\d,]+/);
      // {s} pluralises from the same live number as {count}; a plural rendered
      // server-side goes stale the moment the selection changes.
      var one = !!m && m[0].replace(/,/g, "") === "1";
      msg = msg.replace("{count}", m ? m[0] : "the").replace(/\{s\}/g, one ? "" : "s");
    }
    window.qlConfirm(msg, {
      action: el.getAttribute("data-confirm-action") || "",
      dismiss: el.getAttribute("data-confirm-dismiss") || "",
      irreversible: isIrreversible(el),
    }).then(function (ok) {
      if (!ok) return;
      // A redraw while the dialog was open (the queue ticking over, a job
      // finishing) can replace the control that asked. Act through its
      // redrawn copy, or say nothing happened rather than click a button
      // that is no longer on the page.
      var target = el.isConnected ? el : redrawnConfirmControl(el);
      if (!target || (dismissalKey && confirmControlKey(target) !== dismissalKey)) {
        var main = document.getElementById("main-content");
        if (main) main.focus({ preventScroll: true });
        showToast("The page changed before you confirmed, so nothing was done.", "warning");
        return;
      }
      if (target.tagName === "FORM") {
        // A form's click() never submits it. Fire a real submit event so
        // htmx (or the browser) takes it from here.
        target.requestSubmit();
        return;
      }
      if (target !== el) target.focus();
      target.dataset.confirmBypass = "1";
      target.click();
    });
  }, true);

  // Everything a confirmed control would send, so its redrawn copy can be
  // told apart from the same button on another row.
  function confirmControlKey(el) {
    var form = el.tagName === "FORM" ? el : el.form;
    var key = [el.tagName, el.getAttribute("data-confirm"), el.getAttribute("formaction"),
               el.getAttribute("name"), el.getAttribute("value"), el.getAttribute("href"),
               el.getAttribute("hx-post"), el.getAttribute("hx-get")];
    if (form) {
      key.push(form.getAttribute("action"));
      form.querySelectorAll("input[type=hidden]").forEach(function (input) {
        if (input.name !== "_csrf_token") key.push(input.name + "=" + input.value);
      });
    }
    return key.join("\n");
  }

  function redrawnConfirmControl(el) {
    var key = confirmControlKey(el);
    var found = null;
    document.querySelectorAll("[data-confirm]").forEach(function (candidate) {
      if (!found && !candidate.disabled && confirmControlKey(candidate) === key) found = candidate;
    });
    return found;
  }

  // hx-confirm attributes go through the same styled dialog instead of the
  // browser's native popup (htmx asks via this event before it would call
  // window.confirm itself).
  function redrawnHtmxConfirmControl(el) {
    var key = confirmControlKey(el);
    var found = null;
    document.querySelectorAll("[hx-confirm]").forEach(function (candidate) {
      if (found || confirmControlKey(candidate) !== key) return;
      var submit = candidate.tagName === "FORM"
        ? candidate.querySelector("[type=submit]") : candidate;
      if (submit && !submit.disabled) found = candidate;
    });
    return found;
  }

  document.addEventListener("htmx:confirm", function (evt) {
    var q = evt.detail && evt.detail.question;
    if (!q) return;
    evt.preventDefault();
    var source = evt.detail && evt.detail.elt;
    if (source && source.dataset && source.dataset.confirmBypass === "1") {
      delete source.dataset.confirmBypass;
      evt.detail.issueRequest(true);
      return;
    }
    window.qlConfirm(q, {
      action: (source && source.getAttribute("data-confirm-action")) || "",
      dismiss: (source && source.getAttribute("data-confirm-dismiss")) || "",
      irreversible: isIrreversible(source),
    }).then(function (ok) {
      if (!ok) return;
      if (!source || source.isConnected) {
        evt.detail.issueRequest(true);
        return;
      }
      // A poll can redraw the control while the dialog is open; send the
      // answer through its redrawn copy.
      var target = redrawnHtmxConfirmControl(source);
      if (!target) {
        showToast("The page changed before you confirmed, so nothing was done.", "warning");
        return;
      }
      target.dataset.confirmBypass = "1";
      if (target.tagName === "FORM") target.requestSubmit();
      else target.click();
    });
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

  document.addEventListener("input", function (evt) {
    var field = evt.target;
    if (field.autocomplete !== "new-password" || !field.form) return;
    var confirm = field.form.querySelector("[data-matches-password]");
    if (!confirm) return;
    var password = field.form.elements.namedItem(confirm.dataset.matchesPassword);
    confirm.setCustomValidity(confirm.value && confirm.value !== password.value
      ? "The two passwords don't match." : "");
  });

  // A hidden file input says nothing about what was picked, so write the name
  // beside the button that opened it.
  document.addEventListener("change", function (evt) {
    var input = evt.target;
    if (input.form && input.form.getAttribute("hx-target") === "#collection-restore-result") {
      document.getElementById("collection-restore-result").textContent = "";
    }
    if (!input.matches || !input.matches("input[type=file][data-file-name]")) return;
    var out = document.getElementById(input.getAttribute("data-file-name"));
    if (!out) return;
    var file = input.files && input.files[0];
    out.textContent = file ? file.name : out.getAttribute("data-empty");
    out.classList.toggle("is-chosen", !!file);
  });

  function syncSearchLabel(form) {
    var q = form && form.querySelector('input[name="q"]');
    var mode = form && form.querySelector('input[name="kind"]:checked');
    if (!q || !mode) return;
    var label = mode.value === "artist" ? "Artist name"
      : mode.value === "track" ? "Track title" : "Album title";
    q.setAttribute("placeholder", label);
    q.setAttribute("aria-label", label);
  }

  // A tab is another view of the query in the box: switching runs it there
  // straight away, or shows what that tab already found for it.
  document.addEventListener("change", function (evt) {
    var radio = evt.target;
    if (!radio.matches || !radio.matches('input[name="kind"]')) return;
    showSearchTab(radio);
  });

  // Enter on an empty box clears the search and its address as a tab does.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    var q = isSearchRequestForm(form) && form.querySelector('input[name="q"]');
    if (!q || q.value.trim()) return;
    evt.preventDefault();
    evt.stopImmediatePropagation();
    showSearchTab(form.querySelector('input[name="kind"]:checked'));
    q.focus();
  }, true);

  function showSearchTab(radio) {
    var form = radio.closest("form");
    var q = form && form.querySelector('input[name="q"]');
    if (!q) return;
    syncSearchLabel(form);
    var results = document.getElementById("search-results");
    var shown = searchResponseState(results);
    (searchExitSave || saveSearchSnapshot)();
    var marker = results.querySelector('[data-search-cacheable="1"]');
    if (shown && !searchSnapshotsInvalidated && marker && marker.dataset.libraryRevision === libraryRevision) {
      keptSearchViews[searchSnapshotKey(shown)] = searchSnapshotHtml(results);
    }
    cancelSearchRequests();
    form.querySelectorAll("[data-deep-link]").forEach(function (node) { node.remove(); });
    var query = q.value.trim();
    if (!query) {
      if (shown || results.querySelector(".ql-search-error")) results.replaceChildren();
      var empty = "/?kind=" + encodeURIComponent(radio.value);
      if (location.pathname + location.search !== empty) {
        // Back still returns to a search the box was cleared from.
        if (searchStateFromUrl()) pushSearchUrl(empty);
        else history.replaceState(history.state, "", empty);
        try { sessionStorage.setItem("htmx-current-path-for-history", empty); } catch (e) {}
      }
      return;
    }
    var state = searchStateId(radio.value, query);
    if (state === shown) return;
    if (!keptSearchViews[state]) {
      form.requestSubmit();
      return;
    }
    var url = "/?" + new URLSearchParams({ kind: radio.value, q: query }).toString();
    if (location.pathname + location.search !== url) {
      pushSearchUrl(url);
      // htmx files the page it saves on the next Back under this path.
      try { sessionStorage.setItem("htmx-current-path-for-history", url); } catch (e) {}
    }
    restoreSearchHistory();
  }

  document.addEventListener("input", function (evt) {
    if (evt.target.matches && evt.target.matches('.ql-search-form input[name="q"]')) {
      keptSearchViews = {};
      if (activeSearchRequests.some(function (xhr) { return xhr.qlSearchResultLink; })) {
        pendingSearchFocus = null;
        pendingSearchRestoreY = null;
        cancelSearchRequests();
      }
    }
  });

  document.addEventListener("click", function (evt) {
    var button = evt.target.closest && evt.target.closest("[data-search-by-artist]");
    var form = button && document.querySelector(".ql-search-form");
    if (!form) return;
    form.querySelector('input[name="q"]').value = button.dataset.searchByArtist;
    var radio = form.querySelector('input[name="kind"][value="artist"]');
    radio.checked = true;
    // The search takes the pressed button away with the old results.
    radio.focus({ preventScroll: true });
    radio.dispatchEvent(new Event("change", { bubbles: true }));
  });

  // Search result version lists and tracklists use a real button so the row
  // itself does not mix "expand" and "download" tap targets. Every press
  // asks htmx for the list, and the request is dropped here once the panel
  // holds one, so a press after a failed fetch (empty panel, or a notice)
  // asks again. Discover's album lists work the same way.
  function panelHoldsNotice(panel) {
    var first = panel && panel.firstElementChild;
    return !!first && first.hasAttribute("data-flash");
  }
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var btn = evt.detail && evt.detail.elt;
    if (!btn || !btn.matches("[data-tracks-toggle],[data-discover-toggle]")) return;
    var panel = evt.detail.target;
    if (panel && panel.firstElementChild && !panelHoldsNotice(panel)) evt.preventDefault();
  });
  document.addEventListener("htmx:afterRequest", function (evt) {
    var btn = evt.detail && evt.detail.elt;
    if (!btn || !btn.matches || !btn.matches("[data-tracks-toggle],[data-discover-toggle]")) return;
    var panel = evt.detail.target;
    if (!panel) return;
    if (!panel.classList.contains("ql-track-sheet-list")) {
      if (!evt.detail.successful && !panel.firstElementChild) setPanelOpen(panel, false);
      return;
    }
    var notice = panel.firstElementChild;
    if (!notice) {
      notice = document.createElement("div");
      notice.className = "ql-notice ql-notice-warning";
      notice.setAttribute("data-flash", "");
      notice.setAttribute("role", "alert");
      notice.textContent = "Couldn't load the track list.";
      panel.appendChild(notice);
    }
    if (!panelHoldsNotice(panel) || notice.querySelector("button")) return;
    var retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ql-inline-link ql-inline-link-hit";
    retry.textContent = "Retry";
    retry.onclick = function () {
      panel.focus({ preventScroll: true });
      panel.replaceChildren();
      window.htmx.trigger(btn, "retry-tracks");
    };
    notice.appendChild(retry);
  });
  var discoverFocus = null;
  var discoverWrites = 0;
  var lastDiscoverWrite = Promise.resolve();
  document.addEventListener("htmx:beforeRequest", function (evt) {
    if (evt.detail.target && evt.detail.target.id === "discover-body") {
      evt.detail.xhr.qlDiscoverWrite = discoverWrites ? null : lastDiscoverWrite;
    }
  });
  document.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail.target;
    if (!target || target.id !== "discover-body") return;
    if (target.querySelector(".ql-release-sheet[open]")
        || evt.detail.xhr.qlDiscoverWrite !== lastDiscoverWrite
        || document.querySelector("[data-discover-undo]")) {
      evt.detail.shouldSwap = false;
      return;
    }
    var active = document.activeElement;
    discoverFocus = target.contains(active) ? active : null;
  });
  document.addEventListener("htmx:afterSwap", function (evt) {
    if (!evt.detail.target || evt.detail.target.id !== "discover-body") return;
    document.querySelectorAll("[data-discover-toggle]").forEach(function (button) {
      var panel = document.getElementById(button.getAttribute("aria-controls"));
      if (panel) button.setAttribute("aria-expanded", panel.classList.contains("hidden") ? "false" : "true");
    });
    var previous = discoverFocus;
    discoverFocus = null;
    if (!previous || document.activeElement !== document.body) return;
    var target = previous.isConnected ? previous : previous.id && document.getElementById(previous.id);
    if (target) target.focus({ preventScroll: true });
  });
  var sheetUrl = "";
  var closingSheet = false;
  var afterSheetClose = null;
  var sheetOnLoad = history.state && history.state.qlSheet;
  var loadingSheetArtist = false;

  function pushSheet(id, artistPanel) {
    sheetUrl = location.href;
    if (!history.state || history.state.qlSheet !== id) {
      if (!history.state && document.querySelector(".ql-search-form")) {
        history.replaceState({ htmx: true }, "", sheetUrl);
      }
      history.pushState(Object.assign({}, history.state, {
        qlSheet: id, qlSheetArtist: artistPanel ? artistPanel.id : ""
      }), "", sheetUrl);
    }
  }

  function sheetToggle(id) {
    return document.querySelector('[aria-controls="' + CSS.escape(id) + '"]');
  }

  function showReleaseSheet(panel, button) {
    button.focus({ preventScroll: true });
    panel.showModal();
    panel.querySelector(".ql-release-sheet-list").scrollTop = 0;
  }

  function reopenSheetControl(button) {
    // A feed can deliver this button before htmx wires its request.
    if (window.htmx) window.htmx.process(button);
    button.click();
  }

  function restorePageSheet() {
    if (!sheetOnLoad || !history.state || history.state.qlSheet !== sheetOnLoad) return;
    sheetUrl = location.href;
    var panel = document.getElementById(sheetOnLoad);
    if (panel && panel.matches(".ql-release-sheet, .ql-mobile-drawer .ql-sheet")) {
      var parent = panel.parentElement.closest(".ql-version-sheet");
      if (parent && !parent.open) showReleaseSheet(parent, sheetToggle(parent.id));
      var toggle = sheetToggle(panel.id);
      if (toggle && toggle.getClientRects().length) {
        sheetOnLoad = null;
        reopenSheetControl(toggle);
        return;
      }
    }
    var artist = history.state.qlSheetArtist && sheetToggle(history.state.qlSheetArtist);
    if (!panel && artist && artist.matches("[data-discover-toggle]")) {
      if (artist.classList.contains("htmx-request")) return;
      if (!loadingSheetArtist) {
        loadingSheetArtist = true;
        reopenSheetControl(artist);
        return;
      }
    }
    if (!panel && !artist && document.querySelector("#discover-body[hx-get]")) return;
    sheetOnLoad = null;
    dismissSheet();
  }

  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      if (history.state && history.state.qlSheet === "ql-confirm") dismissSheet();
      return;
    }
    if (!sheetOnLoad) return;
    Promise.resolve(bootSearchRequest).then(function () {
      var root = document.querySelector("[data-search-results-root]");
      return root && root.qlSearchRestored;
    }).then(restorePageSheet);
  });
  document.addEventListener("htmx:afterRequest", function (event) {
    if (!sheetOnLoad || !history.state) return;
    var target = event.detail.target;
    if (target && ((history.state.qlSheetArtist && target.id === history.state.qlSheetArtist)
        || (target.id === "discover-body" && !loadingSheetArtist))) restorePageSheet();
  });

  function closeSheetsOutside(target) {
    var sheets = document.querySelectorAll(".ql-release-sheet[open], .ql-mobile-drawer[open], #ql-confirm[open]");
    var closed = false;
    for (var i = sheets.length - 1; i >= 0; i--) {
      if (target && sheets[i].contains(target)) continue;
      if (sheets[i].matches("dialog")) sheets[i].close();
      else closeDrawer(sheets[i]);
      closed = true;
    }
    return closed;
  }

  function dismissSheet(then) {
    if (!history.state || !history.state.qlSheet) {
      closeSheetsOutside(null);
      if (then) then();
      return;
    }
    if (closingSheet) return;
    closingSheet = true;
    afterSheetClose = then;
    history.back();
  }

  document.addEventListener("click", function (evt) {
    if (evt.target.matches(".ql-release-sheet[open]") || evt.target.closest("[data-close-sheet]")) {
      dismissSheet();
    }
  });
  document.addEventListener("cancel", function (evt) {
    if (evt.target.matches(".ql-release-sheet, #ql-confirm")) {
      evt.preventDefault();
      dismissSheet();
    }
  }, true);

  // A step within the same page belongs to the sheet before it belongs to
  // htmx or a review tab.
  window.addEventListener("popstate", function (evt) {
    sheetOnLoad = null;
    var target = evt.state && evt.state.qlSheet && document.getElementById(evt.state.qlSheet);
    if (evt.state && evt.state.qlSheet && location.href === sheetUrl
        && (!target || (target.id === "ql-confirm" && !target.open))) {
      // A missing release or an old confirmation has nothing to reopen.
      evt.stopImmediatePropagation();
      closingSheet = false;
      dismissSheet();
      return;
    }
    var handled = closeSheetsOutside(target) || closingSheet;
    if (target && !target.matches("dialog[open], .ql-mobile-drawer[open] .ql-sheet")) {
      var toggle = sheetToggle(target.id);
      if (toggle && toggle.getClientRects().length) {
        toggle.click();
        handled = true;
      }
    }
    closingSheet = false;
    var then = afterSheetClose;
    afterSheetClose = null;
    if (handled && location.href === sheetUrl) {
      evt.stopImmediatePropagation();
      // A bookmarked section would otherwise take focus and scroll after popstate.
      if (location.hash && history.scrollRestoration === "auto") {
        var entry = history.state;
        history.scrollRestoration = "manual";
        requestAnimationFrame(function () {
          if (history.state === entry) history.scrollRestoration = "auto";
        });
      }
      if (then) then();
    }
  }, true);

  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest
      && evt.target.closest("[data-version-toggle],[data-tracks-toggle]");
    if (!btn) return;
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    if (panel.matches(".ql-release-sheet")) {
      pushSheet(panel.id, btn.closest("[hx-preserve][id]"));
      showReleaseSheet(panel, btn);
      return;
    }
    var open = btn.getAttribute("aria-expanded") === "true";
    // A press on an open panel that holds a notice asks again in place.
    if (open && panelHoldsNotice(panel)) return;
    setPanelOpen(panel, !open);
  });
  // A desktop search row and its phone layout each have a Tracks button for
  // the same list, so both follow it.
  function setPanelOpen(panel, open) {
    document.querySelectorAll('[aria-controls="' + panel.id + '"]').forEach(function (btn) {
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    panel.classList.toggle("hidden", !open);
  }

  // The line under an opened artist's albums presses that artist's
  // dismiss button, so both ask the same question and send the same request.
  document.addEventListener("click", function (evt) {
    var line = evt.target.closest && evt.target.closest("[data-hide-rest]");
    if (!line) return;
    var shell = line.closest(".ql-review-group-shell");
    var btn = shell && shell.querySelector("[data-hide]");
    if (btn) btn.click();
  });

  // The menu of review actions. Any press outside the toggle closes
  // it, a press on one of its actions included. It listens on the way
  // down because an action's confirm stops the click there.
  function setReviewMore(menu, open) {
    var toggle = menu.querySelector(".ql-review-more-toggle");
    menu.toggleAttribute("data-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("click", function (evt) {
    var toggle = evt.target.closest && evt.target.closest(".ql-review-more-toggle");
    document.querySelectorAll("[data-review-more][data-open]").forEach(function (menu) {
      if (!toggle || !menu.contains(toggle)) setReviewMore(menu, false);
    });
    if (!toggle) return;
    var menu = toggle.closest("[data-review-more]");
    if (menu) setReviewMore(menu, !menu.hasAttribute("data-open"));
  }, true);
  // On a phone the release-type switches open from Include. A press on a
  // switch leaves them open, so several can be set in one go.
  function setReviewInclude(host, open) {
    var toggle = host.querySelector("[data-review-include]");
    host.toggleAttribute("data-include-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("click", function (evt) {
    if (!evt.target.closest) return;
    var toggle = evt.target.closest("[data-review-include]");
    var openHost = document.querySelector(".ql-review-sticky[data-include-open]");
    if (toggle) {
      setReviewInclude(toggle.closest(".ql-review-sticky"), !openHost);
    } else if (openHost && !evt.target.closest(".ql-review-switches")) {
      setReviewInclude(openHost, false);
    }
  });
  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "Escape") return;
    document.querySelectorAll("[data-review-more][data-open]").forEach(function (menu) {
      setReviewMore(menu, false);
      var toggle = menu.querySelector(".ql-review-more-toggle");
      if (toggle && menu.contains(document.activeElement)) toggle.focus();
    });
    var openHost = document.querySelector(".ql-review-sticky[data-include-open]");
    if (openHost) setReviewInclude(openHost, false);
  });

  // Discover artist rows open their albums under the row. Once the list is
  // there the request above is dropped and the button is a plain show/hide.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-discover-toggle]");
    if (!btn) return;
    var panel = document.getElementById(btn.getAttribute("aria-controls"));
    if (!panel) return;
    var next = btn.getAttribute("aria-expanded") !== "true";
    if (!next && panelHoldsNotice(panel)) return;
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

  // Dismiss: the card leaves at once, and the notice offers it back.
  // The albums opened under a card go with it.
  function discoverHiddenCount(n) {
    document.querySelectorAll("[data-discover-hidden-link]").forEach(function (link) {
      var count = link.querySelector("[data-discover-hidden-count]");
      if (count) count.textContent = String(n);
      link.classList.toggle("hidden", !n);
    });
  }
  function discoverShownCount(parent) {
    var n = parent.querySelectorAll("[data-discover-card]").length;
    document.querySelectorAll("[data-discover-count]").forEach(function (el) {
      el.textContent = n + (n === 1 ? " artist" : " artists");
    });
  }
  function discoverHiddenPost(url, form) {
    var body = new FormData(form);
    discoverWrites++;
    var write = lastDiscoverWrite.then(function () {
      return sessionFetch(url, {
        method: "POST",
        headers: { "X-CSRF-Token": body.get("_csrf_token") || "", "Accept": "application/json" },
        body: body,
      });
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data.error || "That didn't go through. Try again in a moment.");
        return data;
      });
    }).finally(function () {
      discoverWrites--;
    });
    lastDiscoverWrite = write.catch(function () {});
    return write;
  }
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    if (!form.matches || !form.matches("[data-discover-hide]")) return;
    var card = form.closest("[data-discover-card]");
    if (!card || !card.parentNode) return;
    evt.preventDefault();
    var id = form.querySelector('[name="artist_id"]').value;
    var name = form.querySelector('[name="name"]').value;
    var panel = document.getElementById("discover-albums-" + id);
    var parent = card.parentNode;
    var after = (panel || card).nextSibling;
    var cards = Array.prototype.slice.call(parent.querySelectorAll("[data-discover-card]"));
    var at = cards.indexOf(card);
    var next = cards[at + 1] || cards[at - 1];
    card.remove();
    if (panel) panel.remove();
    discoverShownCount(parent);
    var focusTo = next && next.querySelector(".ql-result-title a");
    if (focusTo) focusTo.focus({ preventScroll: true });
    function putBack() {
      if (!parent.isConnected) return false;
      parent.insertBefore(card, after && after.parentNode === parent ? after : null);
      if (panel) parent.insertBefore(panel, card.nextSibling);
      discoverShownCount(parent);
      return true;
    }
    discoverHiddenPost(form.action, form).then(function (data) {
      discoverHiddenCount(data.hidden || 0);
      var note = document.createElement("span");
      note.appendChild(document.createTextNode("Dismissed " + name + ". "));
      var undo = document.createElement("button");
      undo.type = "button";
      undo.className = "ql-inline-link";
      undo.setAttribute("data-discover-undo", "");
      undo.textContent = "Undo";
      undo.setAttribute("aria-label", "Bring back " + name);
      note.appendChild(undo);
      var toast = showToast(note, "info");
      undo.addEventListener("click", function () {
        undo.disabled = true;
        discoverHiddenPost("/discover/unhide", form).then(function (back) {
          discoverHiddenCount(back.hidden || 0);
          if (toast) toast.remove();
          if (putBack()) {
            var link = card.querySelector(".ql-result-title a");
            if (link) link.focus({ preventScroll: true });
          }
        }).catch(function (err) {
          undo.disabled = false;
          if (err !== "navigation") showToast(err.message || String(err), "error");
        });
      });
    }).catch(function (err) {
      putBack();
      if (err !== "navigation") showToast(err.message || String(err), "error");
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
    // Matches --ql-bg in tokens.css and the boot script in _app_head.html.
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
  function setDrawerBackground(inert) {
    document.querySelectorAll('.ql-app-shell, .ql-tabbar > a, .ql-tab-more > summary, a[href="#main-content"], .ql-toast-host')
      .forEach(function (el) { el.inert = inert; });
  }
  function lockDrawerPage() {
    if (drawerScrollY !== null || !document.body) return;
    drawerScrollY = Math.max(0, Math.round(window.scrollY || 0));
    document.body.style.setProperty("--ql-drawer-page-top", -drawerScrollY + "px");
    document.body.classList.add("ql-drawer-page-locked");
    setDrawerBackground(true);
  }
  function unlockDrawerPage() {
    if (drawerScrollY === null || !document.body) return;
    var y = drawerScrollY;
    drawerScrollY = null;
    document.body.classList.remove("ql-drawer-page-locked");
    setDrawerBackground(false);
    document.body.style.removeProperty("--ql-drawer-page-top");
    window.scrollTo(0, y);
  }
  function closeDrawer(dd) {
    dd.classList.remove("ql-mobile-drawer-open");
    dd.removeAttribute("open");
    var b = dd.querySelector("[aria-expanded]");
    if (b) b.setAttribute("aria-expanded", "false");
    if (!document.querySelector(".ql-mobile-drawer.ql-mobile-drawer-open")) {
      unlockDrawerPage();
    }
    var summary = dd.querySelector("summary");
    var target = summary.getClientRects().length ? summary : document.getElementById("main-content");
    if (target) target.focus({ preventScroll: true });
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
  window.qlCloseDropdowns = function () {
    if (document.querySelector(".ql-mobile-drawer[open]")) dismissSheet();
  };

  // A search can finish while More is open. Save the closed page and
  // give its pending navigation the entry More was using.
  document.addEventListener("htmx:beforeSwap", function (evt) {
    if (history.state && history.state.qlSheet && evt.detail.shouldSwap &&
        evt.detail.xhr.getResponseHeader("HX-Push-Url")) {
      evt.detail.xhr.qlReplaceSheet = closeSheetsOutside(null);
    }
  });
  document.addEventListener("htmx:beforeHistoryUpdate", function (evt) {
    if (evt.detail.xhr.qlReplaceSheet) evt.detail.history.type = "replace";
  });
  document.addEventListener("click", function (evt) {
    var trigger = evt.target.closest(".ql-mobile-drawer > summary");
    var drawer = document.querySelector(".ql-mobile-drawer[open]");
    if (drawer) {
      var link = evt.target.closest("a[href]");
      if (link) {
        if (evt.defaultPrevented || evt.button !== 0 || evt.ctrlKey || evt.metaKey ||
            evt.altKey || evt.shiftKey || link.target || link.origin !== location.origin) return;
        // Follow the link from the page under More, so Back lands there.
        evt.preventDefault();
        dismissSheet(function () { link.click(); });
      } else if (evt.target.closest('button[type="submit"][form]')) {
        var submitter = evt.target.closest("button");
        evt.preventDefault();
        dismissSheet(function () { submitter.form.requestSubmit(submitter); });
      } else if (trigger || !drawer.contains(evt.target) ||
                 evt.target.closest("[data-close-mobile-menu], [data-theme-toggle]")) {
        evt.preventDefault();
        dismissSheet();
      }
    } else if (trigger) {
      evt.preventDefault();
      drawer = trigger.closest(".ql-mobile-drawer");
      pushSheet(trigger.getAttribute("aria-controls"));
      lockDrawerPage();
      drawer.classList.add("ql-mobile-drawer-open");
      drawer.setAttribute("open", "");
      trigger.setAttribute("aria-expanded", "true");
      drawer.querySelector("[data-close-mobile-menu]").focus({ preventScroll: true });
    }
  });
  // The dimmed backdrop is the drawer's own ::before. On iOS Safari a tap
  // there fires no click unless the drawer itself listens for one.
  function listenOnDrawerBackdrops() {
    document.querySelectorAll(".ql-mobile-drawer").forEach(function (dd) {
      dd.addEventListener("click", function (evt) {
        if (evt.target === dd && drawerIsOpen(dd)) dismissSheet();
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", listenOnDrawerBackdrops);
  } else {
    listenOnDrawerBackdrops();
  }
  // One-shot flash flags should not stay in the URL after first paint.
  var FLASH_PARAMS = ["approved", "stale", "saved", "queued", "connected",
                      "unverified", "mode", "error", "noselection", "skipped",
                      "notice", "quality_note", "waiting", "refreshed", "started",
                      "offer_refresh"];
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
    document.querySelectorAll("#download-toast [data-flash]:not([data-flash-persistent])")
      .forEach(function (el) { setTimeout(function () { fade(el); }, 8000); });
  }
  function normalizeFlashAnnouncements() {
    document.querySelectorAll("[data-flash]").forEach(function (el) {
      if (el.getAttribute("role")) return;
      var isError = el.classList.contains("ql-notice-error");
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
  // told something was left out.
  window.qlDismissSupersededFlashes = function () {
    document.querySelectorAll(
      "[data-flash].ql-notice-success, [data-flash].ql-notice-info,"
      + " [data-flash].ql-flash-info"
    ).forEach(fade);
  };

  // Programmatic toast for async failures and review actions.
  // `message` is a string (rendered as text, never markup) or a prebuilt
  // node. The node form is what lets a receipt carry a real link.
  function showToast(message, kind, persistentKey) {
    var host = document.getElementById("download-toast");
    if (!host && document.body) {
      host = document.createElement("div");
      host.id = "download-toast";
      host.className = "ql-toast-host";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    if (!host) return;
    if (persistentKey) {
      host.querySelectorAll("[data-flash-persistent]").forEach(function (notice) {
        if (notice.dataset.flashPersistent === persistentKey) notice.remove();
      });
    }
    var noticeKind = kind || "info";
    host.querySelectorAll("[data-flash]:not([data-flash-persistent])").forEach(function (notice) {
      if (notice.dataset.flashKind === noticeKind
          && notice.textContent.trim() === message) notice.remove();
    });
    var el = document.createElement("div");
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
    if (persistentKey) {
      el.dataset.flashPersistent = persistentKey;
      addToastClose(el);
    } else {
      setTimeout(function () { fade(el); }, 8000);
    }
    return el;
  }
  window.qlShowToast = showToast;

  function addToastClose(el) {
    var close = document.createElement("button");
    close.type = "button";
    close.className = "ql-icon-button min-h-11 min-w-11 shrink-0";
    close.setAttribute("aria-label", "Dismiss notice");
    close.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-width="1.8" d="m6 6 12 12M6 18 18 6"/></svg>';
    close.addEventListener("click", function () { fade(el); });
    el.appendChild(close);
  }

  // A toast the server sent replaces the earlier one of its group, and one
  // that stays until closed gets its close button.
  function settleServerToasts() {
    var host = document.getElementById("download-toast");
    if (!host) return;
    var latest = {};
    host.querySelectorAll("[data-flash-group]").forEach(function (el) {
      var group = el.dataset.flashGroup;
      if (latest[group]) latest[group].remove();
      latest[group] = el;
    });
    host.querySelectorAll("[data-flash-group][data-flash-persistent]").forEach(function (el) {
      if (!el.querySelector(":scope > .ql-icon-button")) addToastClose(el);
    });
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

  function returnToLogin() {
    var next = window.location.pathname + window.location.search;
    window.location.assign("/login?next=" + encodeURIComponent(next));
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

  // A review row whose cover fails to load gets the record a row with no
  // artwork shows. Listening on the document catches a row however it was
  // added and however early its image failed.
  document.addEventListener("error", function (evt) {
    var img = evt.target;
    if (!img || img.tagName !== "IMG" || !img.classList.contains("ql-review-cover")) return;
    var fallback = document.createElement("span");
    fallback.className = "ql-review-cover ql-review-cover--empty";
    fallback.setAttribute("aria-hidden", "true");
    fallback.innerHTML = '<svg viewBox="0 0 24 24" class="ql-review-cover-mark"><circle cx="12" cy="12" r="7.5" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="12" cy="12" r="1.9" fill="currentColor"/></svg>';
    img.replaceWith(fallback);
  }, true);

  function searchResponseState(results) {
    var marker = results && results.querySelector("[data-search-response-state]");
    return marker ? marker.dataset.searchResponseState || "" : "";
  }

  function searchSnapshotHtml(results) {
    var copy = results.cloneNode(true);
    copy.querySelectorAll(".ql-release-sheet[open]").forEach(function (sheet) {
      sheet.removeAttribute("open");
    });
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
    writeSearchSnapshot(state, searchSnapshotHtml(results), marker.dataset.libraryRevision);
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

  var bootSearchRequest = null;
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
      bootSearchRequest = window.htmx.ajax("POST", "/search", {
        source: form, target: "#search-results", swap: "innerHTML",
        headers: { "HX-History-Restore-Request": "true" }
      }).catch(function () {});
    } else if (window.matchMedia && window.matchMedia("(pointer: fine)").matches) {
      // Not on a touch screen, where focus would open the keyboard.
      form.querySelector('input[name="q"]').focus();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initSearchPage);
  } else {
    initSearchPage();
  }

  function showSearchMoreError(message) {
    var notice = showToast(message, "error");
    if (notice) notice.id = "search-more-error";
  }
  function clearSearchMoreError() {
    var notice = document.getElementById("search-more-error");
    if (notice) notice.remove();
  }

  function initSearchResults() {
    document.querySelectorAll("[data-search-results-root]").forEach(function (root) {
      if (root.dataset.searchWired === "1") return;
      root.dataset.searchWired = "1";
      // Ticks and scroll offset survive a reload or a trip to the Queue, keyed
      // by the search that produced them, so a pull-to-refresh does not lose
      // twenty minutes of picking.
      // Keyed by the search itself, not the URL: results arrive by htmx swap,
      // so the URL reads "/" for every query and one search's picks would
      // carry into the next.
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
          loadedThrough: loadedThrough(),
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
      // The last record of the batches loaded so far, so coming back to the
      // search loads them again.
      function loadedThrough() {
        var button = root.querySelector("[data-search-more-url]");
        var found = button && /[?&]start=(\d+)/.exec(button.dataset.searchMoreUrl);
        var end = found ? Number(found[1]) : Infinity;
        var last = null;
        root.querySelectorAll("[data-search-key][data-search-order]").forEach(function (item) {
          var order = Number(item.dataset.searchOrder);
          if (order < end && (!last || order > Number(last.dataset.searchOrder))) last = item;
        });
        return last ? last.dataset.searchKey : "";
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
      var queueingSelection = false;
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
      // pressing is a deliberate individual tick: the duplicate guard keys on
      // album id, so sweeping an open versions fold in would queue several
      // pressings of one record into one folder.
      function bulkSelectable(box) {
        return visibleItem(box.closest("[data-search-item]"))
          && !box.closest("[data-version-panel]")
          && !!actionableFormForKey(box.dataset.searchKey);
      }
      function reconcileSelection() {
        Object.keys(selected).forEach(function (key) {
          if (!actionableFormForKey(key) && !inUnloadedBatch(key)) delete selected[key];
        });
      }
      // A pick kept from before a reload can sit in a batch not loaded yet.
      function inUnloadedBatch(key) {
        return !!root.querySelector("[data-search-more]")
          && !root.querySelector('[data-search-item][data-search-key="' + CSS.escape(key) + '"]');
      }
      function updateBulkButton() {
        if (!bulkButton) return;
        var missing = selectedKeys().some(inUnloadedBatch);
        bulkButton.disabled = queueingSelection || (missing && !!moreRequest);
        bulkButton.textContent = queueingSelection ? "Queueing…"
          : missing ? (moreRequest ? "Loading selection…" : "Retry selection")
          : "Download selected";
      }
      function syncBoxes() {
        reconcileSelection();
        root.querySelectorAll("[data-search-select]").forEach(function (cb) {
          cb.checked = !!selected[cb.dataset.searchKey];
        });
        var keys = selectedKeys();
        if (bulkBar) bulkBar.classList.toggle("hidden", keys.length === 0);
        updateBulkButton();
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
        // Batches not loaded yet hold no record already in the library.
        var more = root.querySelector("[data-search-more]");
        if (more) shown += parseInt(more.dataset.searchMoreLeft || "0", 10);
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
      // A long list comes a batch at a time. Each batch carries rows for
      // both views, and a parked view takes its rows into its template.
      // Records already in the library came with the first batch, so a batch
      // goes in around them, by each row's place in the list.
      function insertInOrder(target, rows) {
        var placed = Array.prototype.filter.call(target.children, function (el) {
          return el.dataset.searchOrder;
        });
        Array.prototype.slice.call(rows.children).forEach(function (row) {
          var order = Number(row.dataset.searchOrder);
          var next = null;
          for (var i = 0; i < placed.length; i++) {
            if (Number(placed[i].dataset.searchOrder) > order) {
              next = placed[i];
              break;
            }
          }
          target.insertBefore(row, next);
        });
      }
      var moreRequest = null;
      function loadMoreAlbums(through, keyboard) {
        var more = root.querySelector("[data-search-more]");
        var button = more && more.querySelector("[data-search-more-url]");
        if (!button) return Promise.resolve(false);
        if (moreRequest) return moreRequest;
        clearSearchMoreError();
        var url = button.dataset.searchMoreUrl;
        var missingPicks = selectedKeys().filter(inUnloadedBatch);
        new Set(missingPicks.concat(through || [])).forEach(function (key) {
          url += "&through=" + encodeURIComponent(key.replace(/^album-/, ""));
        });
        button.disabled = true;
        moreRequest = sessionFetch(url)
          .then(function (r) { return r.ok ? r.text() : ""; })
          .then(function (text) {
            if (!root.isConnected) return false;
            var reply = document.createElement("template");
            reply.innerHTML = text;
            var batches = reply.content.querySelectorAll("template[data-search-batch]");
            if (!batches.length) {
              button.disabled = false;
              showSearchMoreError(reply.content.textContent.trim()
                || "Couldn't load more results. Try again.");
              return false;
            }
            var firstNew = null;
            batches.forEach(function (batch) {
              var panel = root.querySelector(
                '[data-search-view-panel="' + batch.dataset.searchBatch + '"]');
              if (!panel) return;
              var parked = panel.querySelector("template[data-search-view-template]");
              var host = parked && parked.content.childNodes.length ? parked.content : panel;
              var target = batch.dataset.searchBatch === "table"
                ? host.querySelector(".ql-table-body") : host;
              if (keyboard && !panel.classList.contains("hidden")) {
                firstNew = batch.content.querySelector("[data-search-item]");
              }
              if (target) insertInOrder(target, batch.content);
            });
            var next = reply.content.querySelector("[data-search-more]");
            if (next) more.replaceWith(next);
            else more.remove();
            root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
              if (!panel.classList.contains("hidden") && window.htmx) window.htmx.process(panel);
            });
            initCoverFallbacks();
            missingPicks.forEach(function (key) {
              if (!actionableFormForKey(key)) delete selected[key];
            });
            syncBoxes();
            applyOwnedFilterCount();
            if (firstNew && document.activeElement === document.body) {
              var control = Array.prototype.find.call(
                firstNew.querySelectorAll("input[data-search-select]:enabled, button:enabled"),
                function (el) { return el.getClientRects().length > 0; });
              if (!control) firstNew.tabIndex = -1;
              (control || firstNew).focus();
            }
            return true;
          })
          .catch(function (why) {
            button.disabled = false;
            if (root.isConnected && why !== "navigation") {
              showSearchMoreError("Couldn't load more results. Check your connection.");
            }
            return false;
          })
          .then(function (loaded) {
            moreRequest = null;
            updateBulkButton();
            if (!loaded && keyboard && button.isConnected && document.activeElement === document.body) {
              button.focus({ preventScroll: true });
            }
            return loaded;
          });
        updateBulkButton();
        return moreRequest;
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
            // including an alternate pressing picked by hand and folded away since.
            if (on && !bulkSelectable(box)) return;
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
        if (evt.target.closest("[data-search-more-url]")) {
          evt.preventDefault();
          loadMoreAlbums(null, evt.detail === 0);
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
        var forms = root.querySelectorAll(
          '[data-search-item][data-search-key="' + CSS.escape(key) + '"] [data-search-download-form]');
        return Array.prototype.find.call(forms, function (form) {
          return form.closest("[data-search-item]").dataset.searchKey === key
            && !form.querySelector("button[type=submit]").disabled;
        });
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
        if (keys.some(inUnloadedBatch)) {
          loadMoreAlbums();
          return;
        }
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
        var warning = "";
        if (forms.some(function (form) { return form.dataset.searchDownloadWarning === "unknown"; })) {
          warning = " Could not check existing tracks for every selected album. Downloading may replace existing tracks.";
        }
        window.qlConfirm("Download " + what + "?" + warning, { action: "Download" }).then(function (ok) {
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
        queueingSelection = true;
        updateBulkButton();
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
          queueingSelection = false;
          updateBulkButton();
          if (navigating) return;
          if (stale) { pageWentStale(); return; }
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
          if (owned) parts.push(owned + " already owned");
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
          if (queued && window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
        });
      }
      // A snapshot saved mid-load keeps the button it disabled.
      var moreButton = root.querySelector("[data-search-more-url]");
      if (moreButton) moreButton.disabled = false;
      setView(savedSearchView());
      syncBoxes();
      applyOwnedFilterCount();
      var lastUnloaded = restored.loadedThrough && inUnloadedBatch(restored.loadedThrough);
      if (lastUnloaded || selectedKeys().some(inUnloadedBatch)) {
        // The page could only scroll as far as the first batch reached.
        var moved = false;
        var noteMove = function () { moved = true; };
        ["wheel", "touchmove", "keydown"].forEach(function (name) {
          window.addEventListener(name, noteMove, { once: true, passive: true });
        });
        root.qlSearchRestored = loadMoreAlbums(lastUnloaded ? [restored.loadedThrough] : []).then(function (loaded) {
          if (!loaded) return;
          if (!moved) {
            root.querySelectorAll("[data-search-view-panel]").forEach(function (panel) {
              var y = (restored.viewScroll || {})[panel.dataset.searchViewPanel];
              if (!panel.classList.contains("hidden") && typeof y === "number") {
                panel.scrollTop = y;
              }
            });
            if (typeof restored.scrollY === "number") window.scrollTo(0, restored.scrollY);
          }
          syncBoxes();
        });
      }
      root.addEventListener("qlSearchAvailabilityChanged", function () {
        if (searchSnapshotsInvalidated) {
          var current = loadSelection();
          selected = {};
          Object.keys(current.picks || {}).forEach(function (key) {
            selected[key] = true;
          });
        }
        syncBoxes();
        applyOwnedFilterCount();
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

  var progressNumber = new Intl.NumberFormat();
  function progressFigures(p) {
    return progressNumber.format(p.current) + " / " + progressNumber.format(p.total);
  }

  function progressCount(p) {
    return progressFigures(p) + unitSuffix(p);
  }

  // A full-album download reports its own title as the item, which the row or
  // the page heading already carries.
  function withoutOwnTitle(item, title) {
    if (!title || !item) return item;
    if (item === title) return "";
    return item.indexOf(title + " · ") === 0 ? item.slice(title.length + 3) : item;
  }

  function unitSuffix(p) {
    return p.unit ? " " + p.unit + (p.total === 1 ? "" : "s") : "";
  }

  function fmtProgress(p, verb, withItem) {
    if (withItem === undefined) withItem = true;
    var item = (withItem && p.item) ? " · " + p.item : "";
    if (p.total > 0) {
      return (verb ? verb + " " : "") + progressCount(p) + item;
    }
    if (p.phase) return p.phase + item;
    return "";
  }

  function jobActivityFallback(kind, status) {
    if (status === "scanning") return kind === "new_releases" ? "Checking" : "Scanning";
    if (kind === "upgrade") return "Upgrading";
    if (kind === "downsample") return "Downsampling";
    if (kind === "repair") return "Repairing";
    if (kind === "lyrics") return "Fetching lyrics";
    if (kind === "migration") return "Migrating";
    return (kind === "download" || ["album", "library", "new_releases"].indexOf(kind) >= 0)
      ? "Downloading" : "Running";
  }

  // Queue progress cards.
  function wireStreamCard(card) {
    if (card.dataset.sseWired === "1") return;
    card.dataset.sseWired = "1";
    var id = card.dataset.jobId;
    var status = card.dataset.jobStatus;
    var kind = card.dataset.jobKind || "";
    var runFallback = jobActivityFallback(kind, status);
    if (!id) return;
    var importing = card.dataset.jobImporting === "1";
    var ownTitle = card.dataset.jobTitle || "";
    var progId = "card-prog-" + id;
    var containerId = "queue-body";
    var reconnect = document.getElementById("card-reconnect-" + id);
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
      var body = document.getElementById(containerId);
      window.htmx.ajax("GET", (body && body.dataset.queueSrc) || "/queue",
        { target: "#queue-body", swap: "outerHTML", select: "#queue-body" });
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
      src.addEventListener("auth", function () {
        shut();
        returnToLogin();
      });
    }
    function onProgress(e) {
      streamAlive();
      var p; try { p = JSON.parse(e.data); } catch (_) { return; }
      // Beets runs to its own end, so the server draws Finishing in place of
      // Cancel while an import runs; redraw the card when that starts or ends.
      // The redrawn card carries the state it was drawn with, so the snapshot
      // its own stream opens with does not redraw it again.
      if (!!p.importing !== importing) {
        importing = !!p.importing;
        shut();
        refreshSurface();
        return;
      }
      var el = document.getElementById(progId);
      if (!el) return;
      var verb = status === "scanning" ? runFallback : (p.phase || runFallback);
      var txt = fmtProgress(
        { current: p.current, total: p.total, unit: p.unit, phase: p.phase,
          item: withoutOwnTitle(p.item, ownTitle) }, verb);
      if (txt) {
        el.textContent = txt;
        if (p.track) {
          var track = document.createElement("span");
          track.className = "ql-progress-track";
          track.textContent = p.track;
          el.appendChild(track);
        }
      }
      var bar = document.getElementById("card-bar-" + id);
      if (bar && p.total > 0) {
        var fill = bar.querySelector("i");
        var share = typeof p.fraction === "number" ? p.fraction : p.current / p.total;
        bar.classList.remove("is-uncounted");
        if (fill) fill.style.width = Math.min(100, share * 100) + "%";
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
    var logDetails = logEl && logEl.closest("details");
    var replayLines = 0;
    var replayGap = 0;
    if (logDetails) logDetails.addEventListener("toggle", function () {
      if (logDetails.open) logEl.scrollTop = logEl.scrollHeight;
    }, { once: true });
    var card = document.getElementById("progress-card");
    var count = document.getElementById("prog-count");
    var unit = document.getElementById("prog-unit");
    var bar = document.getElementById("prog-bar");
    var item = document.getElementById("prog-item");
    var track = document.getElementById("prog-track");
    var activity = document.getElementById("job-activity");
    var foundEl = document.getElementById("scan-found");
    var reconnect = document.getElementById("sse-reconnect");
    var jc = document.getElementById("job-content");
    var foundSingular = (jc && jc.dataset.progressItemSingular) || "album";
    var foundPlural = (jc && jc.dataset.progressItemPlural) || foundSingular + "s";
    var ownTitle = (jc && jc.dataset.jobTitle) || "";
    function distanceFromEnd(log) {
      return Math.max(0, log.scrollHeight - log.clientHeight - log.scrollTop);
    }
    function swapJob(target, html, select) {
      var options = { select: select };
      if (logDetails && (logDetails.open || logDetails.contains(document.activeElement))) {
        var open = logDetails.open;
        var gap = distanceFromEnd(logEl);
        var focus = logDetails.contains(document.activeElement);
        var reading = document.activeElement === logEl;
        options.afterSwapCallback = function () {
          var job = document.getElementById("job-content");
          var log = job && job.dataset.jobId === id && job.querySelector(".log-console");
          if (!log) {
            var link = document.querySelector("main a[href='/jobs/" + id + "']");
            if (focus && link) link.focus({ preventScroll: true });
            return;
          }
          var details = log.closest("details");
          details.open = open;
          if (focus) {
            var control = reading ? log : details.querySelector("summary");
            control.focus({ preventScroll: true });
          }
          if (open) log.scrollTop = log.scrollHeight - log.clientHeight - gap;
        };
      }
      window.htmx.swap(target, html, { swapStyle: "outerHTML" }, options);
    }
    // The line names a scan by its kind, and a tool page names its repair.
    var fixedVerb = !!jc && (jc.dataset.embedded ? jc.dataset.jobKind === "repair"
      : jc.dataset.jobStatus === "scanning");
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
        elapsedEl.textContent = mm + ":" + (ss < 10 ? "0" : "") + ss;
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
        ? "Found " + countLabel(foundAlbums, foundSingular, foundPlural)
          + (foundArtists ? " across " + plural(foundArtists, "artist") : "") + " so far…"
        : "";
    }
    function clearQueuedState() {
      if (activity && activity.hasAttribute("data-queued")) {
        activity.removeAttribute("data-queued");
        activity.textContent = jc && jc.dataset.jobType === "download" && !jc.dataset.jobKind ? "Downloading"
          : (jc && jc.dataset.jobKind === "repair" ? "Scan in progress" : "Scanning");
      }
    }
    // A page drawn while the job waited has no clock or progress card, so
    // the first progress fetches the running page in its place.
    var renderedQueued = !!(activity && activity.hasAttribute("data-queued"));
    var leavingQueued = false;
    function leaveQueued() {
      if (leavingQueued || !window.htmx) return;
      leavingQueued = true;
      var embedded = jc && jc.dataset.embedded ? "?embedded=1" : "";
      sessionFetch("/jobs/" + id + "/content" + embedded)
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          var target = document.getElementById("job-content");
          if (!target || finished) return;
          // Progress replayed for a job that still waits: keep waiting.
          if (/id="job-activity"[^>]*data-queued/.test(html)) {
            leavingQueued = false;
            return;
          }
          finished = true;
          if (src) { try { src.close(); } catch (e) {} }
          stopWatching();
          stopElapsed();
          swapJob(target, html);
        })
        .catch(function () { leavingQueued = false; });
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
        if (logEl) {
          var following = distanceFromEnd(logEl) <= 1;
          logEl.appendChild(document.createTextNode(e.data + "\n"));
          if (replayLines > 0) {
            replayLines--;
            logEl.scrollTop = logEl.scrollHeight - logEl.clientHeight - replayGap;
          } else if (following) {
            logEl.scrollTop = logEl.scrollHeight;
          }
        }
      };
      src.addEventListener("ping", function () {
        replayLines = 0;
        streamAlive();
      });
      src.onopen = function () {
        streamAlive();
        // Reconnect replays recent lines, so reset before appending them again.
        if (opened) {
          if (logEl) {
            replayGap = logDetails.open ? distanceFromEnd(logEl) : 0;
            replayLines = replayGap > 1
              ? Math.min(logEl.childNodes.length, Number(logEl.dataset.replayLimit)) : 0;
            logEl.textContent = "";
          }
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
        replayLines = 0;
        var p; try { p = JSON.parse(e.data); } catch (_) { return; }
        if (renderedQueued) { leaveQueued(); return; }
        clearQueuedState();
        if (window.qlDismissSupersededFlashes) window.qlDismissSupersededFlashes();
        if (activity && p.phase && !fixedVerb && activity.textContent !== p.phase) {
          activity.textContent = p.phase;
        }
        if (card) card.classList.remove("hidden");
        if (count) {
          count.textContent = p.total > 0 ? progressFigures(p)
            : (p.current ? progressNumber.format(p.current) : "");
        }
        if (unit) unit.textContent = p.total > 0 ? unitSuffix(p).trim() : "";
        var share = typeof p.fraction === "number" ? p.fraction : p.current / p.total;
        if (bar) { if (p.total > 0) { bar.max = 100; bar.value = Math.round(share * 100); } else { bar.removeAttribute("value"); } }
        if (item) item.textContent = withoutOwnTitle(p.item, ownTitle) || "";
        if (track) track.textContent = p.track || "";
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
        var pendingCancel = document.querySelector("[data-pending-cancel-form]");
        if (pendingCancel) pendingCancel.remove();
        var importChip = document.querySelector("[data-import-chip]");
        if (cancelForm && importChip) {
          cancelForm.classList.toggle("hidden", !!p.importing);
          importChip.classList.toggle("hidden", !p.importing);
        }
      });
      src.addEventListener("done", onDone);
      src.addEventListener("auth", function () {
        if (src) { try { src.close(); } catch (e) {} }
        stopWatching();
        returnToLogin();
      });
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
      if (window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
      finishJob();
    }

    // The finished job body is fetched, not pushed. With nothing reachable at
    // the moment the run ends, the page would sit on a scan that stopped
    // running minutes ago, so say where it stands and ask again on reconnect.
    function finishJob() {
      var body = document.getElementById("job-content");
      if (!body) { stopWatching(); return; }
      if (!window.htmx) { reloadToRecover(); return; }
      // A tool page draws more than the job around it, such as the Library
      // header's refresh controls, and hides that while a job runs, so the
      // whole page is fetched again.
      var surface = body.dataset.embedded && body.closest("[data-job-surface]");
      var targetId = surface ? surface.id : body.id;
      var href = surface
        ? surface.dataset.jobSurface + "?finished=" + encodeURIComponent(id)
        : "/jobs/" + id + "/content" + (body.dataset.embedded ? "?embedded=1" : "");
      sessionFetch(href)
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (html) {
          var target = document.getElementById(targetId);
          if (!target) return;
          stopWatching();
          hideGap();
          swapJob(target, html, surface ? "#" + targetId : undefined);
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
    var emptyBox = document.getElementById("review-empty");
    var footerDiscard = document.getElementById("review-footer-discard");
    var footer = document.getElementById("review-footer");
    var selectTools = document.getElementById("review-select-tools");
    var filterRow = document.getElementById("review-filter-row");
    var filterInput = document.getElementById("review-filter");
    var dsTotal = document.querySelector("[data-downsample-total]");
    var dismissRest = document.getElementById("review-dismiss-rest");
    var reviewKind = form.getAttribute("data-review-kind") || "";
    var reviewBlocked = form.getAttribute("data-review-blocked") === "1";
    var isDownsampleReview = reviewKind === "downsample";
    // Live, not baked: on a library review the dismiss vocabulary follows
    // whichever tab is active now. The data attributes only know the tab the
    // page was first rendered with.
    function dismissItemSingular() {
      if (reviewKind === "library" && curTab()) {
        return curTab() === "gaps" ? "album to fill" : "missing album";
      }
      return cont.dataset.reviewDismissSingular || "album";
    }
    function dismissItemPlural() {
      if (reviewKind === "library" && curTab()) {
        return curTab() === "gaps" ? "albums to fill" : "missing albums";
      }
      return cont.dataset.reviewDismissPlural || "albums";
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
        return "Keep " + countLabel(rest, "unselected album", "unselected albums") + " hi-res?";
      }
      return "Dismiss " + countLabel(rest, "unselected " + dismissItemSingular(), "unselected " + dismissItemPlural())
        + "? You can bring " + (rest === 1 ? "it" : "them") + " back from Dismissed.";
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

    // A ticked album stays listed whatever its release type, so unticking one
    // whose switch is off takes it off the list.
    function listMayHaveChanged(c) {
      if (c && (c.missing_total !== lastCounts.missing_total ||
                c.gap_total !== lastCounts.gap_total)) refreshNeeded = true;
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
        filtered_total: parseInt(box.dataset.filteredTotal || "0", 10),
        filtered_selected: parseInt(box.dataset.filteredSelected || "0", 10),
        filtered_rest: parseInt(box.dataset.filteredRest || "0", 10),
      };
      c.selection_tokens = {};
      c.selection_tokens[curTab()] = box.dataset.selectionToken;
      if (box.dataset.reviewMissingTotal !== undefined) {
        c.missing_total = parseInt(box.dataset.reviewMissingTotal || "0", 10);
        c.missing_selected = parseInt(box.dataset.reviewMissingSelected || "0", 10);
        c.gap_total = parseInt(box.dataset.reviewGapTotal || "0", 10);
        c.gap_selected = parseInt(box.dataset.reviewGapSelected || "0", 10);
        c.missing_types_on = (box.dataset.reviewTypesOn || "").split(",").filter(Boolean);
      }
      return c;
    }

    var pendingWrites = 0;
    var choiceQueue = Promise.resolve();
    var pendingChoices = new Map();
    var choicesUnknown = false;
    var refreshNeeded = false;
    var recovering = null;
    var dismissPending = false;
    var choiceVersion = 0;

    function actionsBlocked() {
      return pendingWrites > 0 || choicesUnknown || dismissPending || pageLoading;
    }

    function submitBlocked() {
      return reviewBlocked || actionsBlocked() || submit.dataset.busyHtml !== undefined
        || tabCounts(lastCounts).selected === 0;
    }

    function syncActions() {
      var blocked = actionsBlocked();
      if (submit) submit.disabled = submitBlocked();
      cont.querySelectorAll("[data-select-master], [data-select-page]").forEach(function (button) {
        button.disabled = !!pageLoading;
      });
      form.querySelectorAll("[data-hide], #review-dismiss-rest, [formaction$='/cancel']").forEach(function (button) {
        if (blocked && !button.disabled) {
          button.disabled = true;
          button.dataset.selectionLocked = "1";
        } else if (!blocked && button.dataset.selectionLocked) {
          button.disabled = false;
          delete button.dataset.selectionLocked;
        }
      });
    }

    function applyPendingChoices() {
      cont.querySelectorAll("input.cb").forEach(function (cb) {
        if (pendingChoices.has(cb.value)) cb.checked = pendingChoices.get(cb.value);
      });
    }

    function recoverChoices() {
      if (pendingWrites || dismissPending || recovering || !cont.isConnected) return;
      var message = document.createElement("span");
      message.textContent = "Couldn't confirm the saved choices. Check them before continuing. ";
      var retry = document.createElement("button");
      retry.type = "button";
      retry.className = "ql-inline-link";
      retry.textContent = "Retry";
      retry.addEventListener("click", recoverChoices);
      message.appendChild(retry);
      var notice = showToast(message, "error", "review-connection-" + id);
      pendingChoices.clear();
      recovering = refreshReview().then(function (loaded) {
        recovering = null;
        if (loaded) {
          choicesUnknown = false;
          if (notice) notice.remove();
        }
        syncActions();
      });
    }

    function queueChoice(url, body, accept) {
      pendingWrites += 1;
      choiceVersion += 1;
      // A changed selection is a different download from the one sent.
      restoreBusySubmitButtons(form);
      syncActions();
      var request = choiceQueue.then(function () {
        if (choicesUnknown) return Promise.reject("uncertain");
        return post(url, body).then(function (r) {
          return r.ok ? r.json() : Promise.reject();
        }).then(accept);
      });
      choiceQueue = request.catch(function (why) {
        if (why !== "navigation") choicesUnknown = true;
      }).then(function () {
        pendingWrites -= 1;
        syncActions();
        if (!pendingWrites) {
          if (choicesUnknown) recoverChoices();
          else if (refreshNeeded) refreshReview();
        }
      });
      return request;
    }

    function scopedCounts(c, tab, query, generation) {
      if (tab === curTab() && query === curQuery() && generation === loadGen) return c;
      var counts = Object.assign({}, c);
      delete counts.filtered_total;
      delete counts.filtered_selected;
      delete counts.filtered_rest;
      return counts;
    }

    // Counts come from the server because the DOM only holds one page. The
    // bulk bar (submit + dismiss-unselected) is scoped to the active tab.
    function applyCounts(c) {
      if (!c) return;
      lastCounts = c;
      var token = form.elements.namedItem("selection_token");
      if (token && c.selection_tokens && c.selection_tokens[curTab()]) {
        token.value = c.selection_tokens[curTab()];
      }
      if (typeof c.filtered_total === "number") {
        var fb = pageBox();
        if (fb) {
          fb.dataset.filteredTotal = String(c.filtered_total);
          fb.dataset.filteredSelected = String(c.filtered_selected);
          fb.dataset.filteredRest = String(c.filtered_rest);
        }
      }
      var tc = tabCounts(c);
      if (submit) {
        submit.disabled = submitBlocked();
        var label = (submit.dataset.reviewVerb || "Download") + " " +
          formatCount(tc.selected) + " selected";
        if (submit.dataset.busyHtml === undefined) submit.textContent = label;
        else submit.dataset.busyHtml = label;
      }
      if (summaryRow) summaryRow.classList.toggle("hidden", c.total === 0);
      if (emptyBox) emptyBox.classList.toggle("hidden", c.total > 0);
      if (submit) submit.classList.toggle("hidden", c.total === 0);
      if (footerDiscard) footerDiscard.classList.toggle("hidden", c.total > 0);
      // The bar shows once something on the active tab is ticked. A spent
      // review keeps it for Discard.
      if (footer) footer.classList.toggle("hidden", c.total > 0 && tc.selected === 0);
      if (selectTools) selectTools.classList.toggle("hidden", tc.total === 0 || shownTotal() === 0);
      var tabsNav = cont.querySelector("[data-review-tabs]");
      if (tabsNav) tabsNav.classList.toggle("hidden", c.total === 0);
      if (filterRow && !curQuery()) filterRow.classList.toggle("hidden", c.artists < 4 || tc.total === 0);
      if (dsTotal) {
        // "smaller", not "reclaimed": with originals kept, the run costs more
        // disk than it saves until the backups expire.
        dsTotal.textContent = c.reclaimable_label
          ? "~" + c.reclaimable_label + " smaller" : "";
      }
      var countLine = document.querySelector("[data-review-count]");
      if (countLine) countLine.textContent = plural(c.total, "album");
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
      // Another tab may have flipped a release-type switch.
      if (c.missing_types_on) {
        cont.querySelectorAll("[data-review-switch]").forEach(function (b) {
          var on = c.missing_types_on.indexOf(b.getAttribute("data-review-switch")) >= 0;
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }
      syncInclude();
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
      syncActions();
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

    // A tab change carries open artists over and Back reopens them, so the
    // one control reads the groups rather than its own last press.
    function syncExpandAll() {
      var btn = cont.querySelector("[data-expand-all]");
      var box = pageBox();
      if (!btn || !box) return;
      var groups = Array.from(box.querySelectorAll("details[data-artist]"));
      var allOpen = groups.length > 0 && groups.every(function (d) { return d.open; });
      btn.setAttribute("data-expand-all", allOpen ? "0" : "1");
      btn.textContent = allOpen ? "Collapse all" : "Expand all";
    }
    cont.addEventListener("toggle", syncExpandAll, true);
    syncExpandAll();

    // The rows the list can show: the tab's, or under a filter the ones it
    // matches.
    function shownTotal() {
      var box = pageBox();
      if (!curQuery() || !box) return tabCounts(lastCounts).total;
      return parseInt(box.dataset.filteredTotal || "0", 10) || 0;
    }

    function syncSelectPage() {
      var btn = cont.querySelector("[data-select-page]");
      var box = pageBox();
      if (!btn || !box) return;
      btn.classList.toggle("hidden", !(parseInt(box.dataset.pages || "1", 10) > 1));
    }

    function updateDismissRest() {
      if (!dismissRest) return;
      var rest = restCount();
      dismissRest.classList.toggle("hidden", rest <= 0);
      dismissRest.textContent = dismissRestLabel(rest);
    }

    // Include names how many of the active tab's switches are on, and goes
    // away on a tab that has none.
    function syncInclude() {
      var toggle = cont.querySelector("[data-review-include]");
      if (!toggle) return;
      var group = cont.querySelector("[data-review-switches]:not(.hidden)");
      var on = group ? group.querySelectorAll('[aria-pressed="true"]').length : 0;
      toggle.classList.toggle("hidden", !group);
      toggle.textContent = on ? "Include (" + on + ")" : "Include";
    }

    function syncMaster() {
      var master = cont.querySelector("[data-select-master]");
      if (!master) return;
      var tc = tabCounts(lastCounts);
      if (curQuery()) {
        var box = pageBox();
        if (box) {
          tc = {
            total: parseInt(box.dataset.filteredTotal || "0", 10),
            selected: parseInt(box.dataset.filteredSelected || "0", 10),
          };
        }
      }
      master.checked = tc.total > 0 && tc.selected >= tc.total;
      master.indeterminate = tc.selected > 0 && tc.selected < tc.total;
      // The button takes every pick on the tab, filter or not.
      var offscreen = form.querySelector("[data-review-offscreen]");
      if (offscreen) {
        var outside = curQuery() ? tabCounts(lastCounts).selected - tc.selected : 0;
        offscreen.textContent = outside > 0
          ? formatCount(outside) + " of them outside this filter"
          : "";
      }
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
      showToast("Your choices changed, but couldn't be saved to disk. They may not survive a restart. Check the data folder.", "error", "review-save-" + id);
    }

    // Save one checkbox to the server, then refresh counts from its response.
    function saveTick(cb) {
      var previous = !cb.checked;
      if (choicesUnknown || dismissPending || pageLoading) {
        cb.checked = previous;
        return;
      }
      var on = cb.checked;
      var requestTab = curTab();
      var requestQuery = curQuery();
      var requestGeneration = loadGen;
      // Dismiss counts follow the current filter.
      var scopeQ = curQuery()
        ? "&q=" + encodeURIComponent(curQuery()) +
          "&tab=" + encodeURIComponent(curTab())
        : "";
      var det = cb.closest("details[data-artist]");
      // A second tap while the first is saving queues behind it; only the
      // newest one sets the box when its answer comes back.
      var seq = (cb._tickSeq || 0) + 1;
      cb._tickSeq = seq;
      pendingChoices.set(cb.value, on);
      cb.setAttribute("aria-busy", "true");
      function done() {
        if (cb._tickSeq === seq) cb.removeAttribute("aria-busy");
      }
      var body = "cid=" + encodeURIComponent(cb.value) + "&checked=" + (cb.checked ? "1" : "0") + scopeQ;
      queueChoice("/jobs/" + id + "/select", body, function (c) {
          done();
          if (cb._tickSeq !== seq) return;
          pendingChoices.delete(cb.value);
          cb.checked = on;
          if (det) delete det.dataset.selectionOverride;
          listMayHaveChanged(c);
          if (c) applyCounts(scopedCounts(c, requestTab, requestQuery, requestGeneration));
          updateHideLabels();
          updateArtistChecks();
          syncExpandAll();
        })
        .catch(function () {
          done();
        });
    }

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
      // Earlier choices still saving go first: queueChoice sends in order.
      if (choicesUnknown || dismissPending || pageLoading) {
        applyCounts(lastCounts);
        return Promise.resolve({ ok: false, busy: true });
      }
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
      return queueChoice("/jobs/" + id + "/select-all", body, function (c) {
          var current = requestTab === curTab() &&
                        requestQuery === curQuery() &&
                        requestGeneration === loadGen;
          listMayHaveChanged(c);
          applyCounts(scopedCounts(c, requestTab, requestQuery, requestGeneration));
          warnPersistFailed(c);
          var accepted = new Set(c.accepted_cids || []);
          if (current && (scope === "all" || scope === "page")) {
            pageBox() && pageBox().querySelectorAll("details[data-artist]").forEach(function (det) {
              var ids = groupCandidateIds(det);
              if (det._selecting || !ids.length || !ids.every(function (cid) { return accepted.has(cid); })) return;
              det.dataset.selectionOverride = on ? "1" : "0";
              applyGroupChoice(det, on, true);
            });
          }
          applyPendingChoices();
          updateHideLabels();
          updateArtistChecks();
          syncExpandAll();
          return { ok: true, counts: c, current: current, accepted: accepted };
        })
        .catch(function (why) {
          if (why === "navigation") {
            return { ok: false, busy: false, navigating: true };
          }
          return { ok: false, uncertain: true };
        });
    }

    // The switch only changes which saved rows a tab lists, so the list
    // reloads where the user stands once the choice is saved.
    function switchReleaseType(btn) {
      if (actionsBlocked()) return;
      var on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      syncInclude();
      var body = "type=" + encodeURIComponent(btn.getAttribute("data-review-switch")) +
                 "&on=" + (on ? "1" : "0");
      queueChoice("/jobs/" + id + "/missing-types", body, function (c) {
        refreshNeeded = true;
        applyCounts(c);
      }).catch(function () {
        btn.setAttribute("aria-pressed", on ? "false" : "true");
        syncInclude();
      });
    }

    function groupSelect(det, on) {
      var previousRows = Array.prototype.map.call(
        det.querySelectorAll(".cb"), function (cb) { return cb.checked; });
      // As in saveTick, a newer tap on the same artist decides its boxes.
      det._selecting = (det._selecting || 0) + 1;
      var seq = (det._selectSeq || 0) + 1;
      det._selectSeq = seq;
      var header = det.querySelector("[data-artist-select]");
      if (header) header.setAttribute("aria-busy", "true");
      det.dataset.selectionOverride = on ? "1" : "0";
      applyGroupChoice(det, on, false);
      bulkSelect(on, "artist", det.dataset.artist || "").then(function (result) {
        det._selecting -= 1;
        if (det._selectSeq !== seq) return;
        if (header) header.removeAttribute("aria-busy");
        if (result.navigating) return;
        if (result.ok && result.current && groupCandidateIds(det).every(function (cid) {
          return result.accepted.has(cid);
        })) {
          applyGroupChoice(det, on, true);
        } else if (!result.ok && !result.uncertain) {
          delete det.dataset.selectionOverride;
          det.querySelectorAll(".cb").forEach(function (cb, index) {
            if (index < previousRows.length) cb.checked = previousRows[index];
          });
          restoreGroupHeader(det);
        }
        applyPendingChoices();
        updateHideLabels();
        updateArtistChecks();
      });
    }

    // "the 1 unselected album" counts at the reader; after "the", one of a
    // thing needs no number.
    function theCount(n) { return n === 1 ? "" : formatCount(n) + " "; }

    // Label each artist action for what it will drop or keep.
    function artistDismissConfirm(who, rest, total) {
      var all = rest === total;
      var lead = all && rest > 1 ? "all " + formatCount(rest) + " "
        : "the " + theCount(rest) + (all ? "" : "unselected ");
      if (isDownsampleReview) {
        return "Keep " + lead + (rest === 1 ? "album" : "albums") + " by " + who +
          " hi-res? You can downsample " + (rest === 1 ? "it" : "them") + " later.";
      }
      return "Dismiss " + lead +
        (rest === 1 ? dismissItemSingular() : dismissItemPlural()) +
        " by " + who + "? You can bring " + (rest === 1 ? "it" : "them") + " back from Dismissed.";
    }
    // The same action as a line under an opened artist's albums.
    function restLineLabel(rest, total) {
      if (rest === total) {
        if (total === 1) return isDownsampleReview ? "Keep hi-res" : "Dismiss";
        return isDownsampleReview ? "Keep all " + formatCount(total) + " hi-res"
          : "Dismiss all " + formatCount(total);
      }
      var n = rest === 1 ? "one" : formatCount(rest);
      return isDownsampleReview ? "Keep the other " + n + " hi-res" : "Dismiss the " + n + " not ticked";
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
        var useSavedCounts = lazy && !lazy.dataset.loaded;
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
            artistDismissConfirm(det.dataset.artist || "this artist", rest, total));
          btn.setAttribute("data-confirm-action",
            isDownsampleReview ? "Keep hi-res" : "Dismiss");
        } else {
          btn.removeAttribute("data-confirm");
        }
        var line = det.querySelector("[data-hide-rest]");
        if (line) {
          line.textContent = restLineLabel(rest, total);
          line.parentElement.classList.toggle("hidden", rest <= 0);
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
        var useSavedCounts = lazy && !lazy.dataset.loaded;
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
      var pageBtn = cont.querySelector("[data-select-page]");
      if (pageBtn) {
        var checks = Array.from(box.querySelectorAll("[data-artist-select]"));
        var allPicked = checks.length > 0 && checks.every(function (cb) { return cb.checked; });
        pageBtn.setAttribute("data-select-page", allPicked ? "0" : "1");
        pageBtn.textContent = allPicked ? "Deselect page" : "Select page";
        pageBtn.setAttribute("aria-label", (allPicked ? "Deselect" : "Select") + " everything on this page only");
      }
    }

    function loadGroupItems(det, force) {
      if (pendingWrites) return choiceQueue.then(function () { return loadGroupItems(det, force); });
      var boxEl = det.querySelector("[data-lazy-items]");
      if (!boxEl || (boxEl.dataset.loaded && !force)) return Promise.resolve(true);
      // One in-flight fetch per box: the restore path and the toggle handler
      // both ask for the same rows in the same tick.
      if (boxEl._loadPromise && !force) return boxEl._loadPromise;
      var generation = (boxEl._loadGeneration || 0) + 1;
      var choicesAtStart = choiceVersion;
      boxEl._loadGeneration = generation;
      boxEl.dataset.loading = "1";
      var status = boxEl.querySelector(".ql-review-items-loading");
      if (status) status.textContent = "Loading…";
      var p = sessionFetch(boxEl.dataset.itemsUrl)
        .then(function (r) { return r.ok ? r.text() : Promise.reject(); })
        .then(function (txt) {
          if (!det.isConnected) return false;
          if (choicesAtStart !== choiceVersion) return loadGroupItems(det, true);
          if (boxEl._loadGeneration !== generation) return false;
          delete boxEl.dataset.loading;
          boxEl.innerHTML = txt;
          boxEl.dataset.loaded = "1";
          if (det.dataset.selectionOverride !== undefined) {
            applyGroupChoice(det, det.dataset.selectionOverride === "1", false);
          }
          applyPendingChoices();
          updateHideLabels();
          updateArtistChecks();
          syncExpandAll();
          return true;
        })
        .catch(function (why) {
          if (boxEl._loadGeneration !== generation) return false;
          delete boxEl.dataset.loading;
          if (why === "navigation") return false;
          if (status) {
            var retry = document.createElement("button");
            retry.type = "button";
            retry.className = "ql-inline-link ql-inline-link-hit";
            retry.textContent = "Retry";
            retry.onclick = function () {
              det.querySelector("summary").focus({ preventScroll: true });
              loadGroupItems(det, false);
            };
            status.replaceChildren("Couldn't load this artist's results. ", retry);
          }
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
    var loadError = null;
    var pendingTab = null;
    var loadGen = 0;

    // Append the next page in place.
    function loadMore(page) {
      if (pendingWrites) return choiceQueue.then(function () { return loadMore(page); });
      if (choicesUnknown || dismissPending) return Promise.resolve(false);
      if (loading) return loadingPromise || Promise.resolve(false);
      if (pageLoading) return Promise.resolve(false);
      if (loadError) loadError.remove();
      loading = true;
      var gen = ++loadGen;
      var choicesAtStart = choiceVersion;
      var url = "/jobs/" + id + "/review?page=" + (page || 2) +
                "&q=" + encodeURIComponent(curQuery()) +
                "&tab=" + encodeURIComponent(curTab());
      loadingPromise = sessionFetch(url)
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (txt) {
          if (gen !== loadGen) return false;
          if (choicesAtStart !== choiceVersion) {
            loading = false;
            loadingPromise = null;
            return loadMore(page);
          }
          if (txt == null) {
            loadError = showToast("Couldn't load those results. Try again.", "error");
            return false;
          }
          var tmp = document.createElement("div");
          tmp.innerHTML = txt;
          var liveGroups = document.getElementById("review-groups");
          var newGroups = tmp.querySelector("#review-groups");
          var oldMore = document.getElementById("review-loadmore");
          var nextFocus = oldMore && oldMore.querySelector("[data-load-more]:focus-visible") && newGroups
            ? newGroups.querySelector("details[data-artist] > summary") : null;
          if (liveGroups && newGroups) {
            while (newGroups.firstElementChild) liveGroups.appendChild(newGroups.firstElementChild);
            liveGroups.dataset.page = newGroups.dataset.page || liveGroups.dataset.page;
          }
          if (oldMore) oldMore.remove();
          var newMore = tmp.querySelector("#review-loadmore");
          if (newMore && liveGroups) liveGroups.parentNode.appendChild(newMore);
          if (window.htmx) window.htmx.process(document.getElementById("review-page"));
          updateHideLabels();
          updateArtistChecks();
          syncExpandAll();
          if (nextFocus && nextFocus.isConnected) nextFocus.focus();
          return true;
        })
        .catch(function (why) {
          if (why !== "navigation" && gen === loadGen) {
            loadError = showToast("Couldn't load those results. Check your connection.", "error");
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
    function reviewEntry(tab, query, filter, from) {
      return { ql: { review: id, tab: tab || "", q: query || "", filter: !!filter,
                     from: from || "" } };
    }
    function onFilterEntry() {
      var st = window.history.state && window.history.state.ql;
      return !!(st && st.review === id && st.filter);
    }
    // The query of the entry the filter stepped off; Back from the filter
    // lands on it.
    function filterFrom() {
      var st = window.history.state && window.history.state.ql;
      return st && st.review === id ? st.from || "" : "";
    }
    function setHiddenReturn(tab, query) {
      var link = cont.querySelector(".ql-review-dismissed-link");
      if (!link || link.pathname !== location.pathname + "/hidden") return;
      var params = new URLSearchParams();
      if (tab) params.set("tab", tab);
      if (query) params.set("q", query);
      link.search = tab || query
        ? new URLSearchParams({ return_to: location.pathname + "?" + params }).toString() : "";
    }
    function syncUrl(tab, query, mode) {
      setHiddenReturn(tab, query);
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
      var st = window.history.state && window.history.state.ql;
      var filterStep = mode === "filter" ? !!query : (mode !== "push" && onFilter);
      var push = mode === "push" || (filterStep && !onFilter);
      var from = !filterStep ? ""
        : onFilter ? filterFrom()
        : (st && st.review === id ? st.q || "" : "");
      if (history.state && history.state.qlSheet) {
        if (url.href === location.href) return;
        if (closeSheetsOutside(null)) push = false;
      }
      if (push && window.history.pushState) {
        window.history.pushState(reviewEntry(tab, query, filterStep, from), "", url);
      } else {
        window.history.replaceState(reviewEntry(tab, query, filterStep, from), "", url);
      }
    }

    // A redraw of the list replaces the control that had keyboard focus. Note
    // what it acted on, and after the redraw focus the same control, the same
    // one on the next or previous artist when its group has gone, or the
    // Select all box.
    var pendingReviewFocus = null;
    function reviewFocusSpot(el) {
      var host = document.getElementById("review-page");
      if (!el || !host || !host.contains(el)) return null;
      var shell = el.closest(".ql-review-group-shell");
      var group = shell && shell.querySelector("details[data-artist]");
      function artistOf(s) {
        var d = s && s.querySelector && s.querySelector("details[data-artist]");
        return d ? d.getAttribute("data-artist") : null;
      }
      return {
        cid: el.matches("input.cb") ? el.value : "",
        kind: el.matches("[data-hide]") ? "[data-hide]"
          : el.matches("[data-hide-rest]") ? "[data-hide-rest]"
          : el.matches("[data-artist-select]") ? "[data-artist-select]" : "summary",
        artists: [group ? group.getAttribute("data-artist") : null,
                  artistOf(shell && shell.nextElementSibling),
                  artistOf(shell && shell.previousElementSibling)]
      };
    }
    function restoreReviewFocus(spot) {
      var now = document.activeElement;
      if (!spot || (now && now !== document.body)) return;
      function shown(el) { return el && el.getClientRects().length ? el : null; }
      var target = spot.cid
        ? cont.querySelector('input.cb[value="' + CSS.escape(spot.cid) + '"]') : null;
      for (var i = 0; !target && i < spot.artists.length; i++) {
        if (spot.artists[i] === null) continue;
        var group = cont.querySelector('details[data-artist="' + CSS.escape(spot.artists[i]) + '"]');
        var shell = group && group.closest(".ql-review-group-shell");
        if (!shell) continue;
        // The group's rows arrive after the redraw; wait for the box.
        if (spot.cid && i === 0 && group.open
            && group.querySelector("[data-lazy-items]:not([data-loaded])")) {
          loadGroupItems(group, false).then(function (loaded) {
            if (loaded) restoreReviewFocus(spot);
            else if (document.activeElement === document.body) {
              group.querySelector("summary").focus({ preventScroll: true });
            }
          });
          return;
        }
        target = shown(shell.querySelector(spot.kind)) || group.querySelector("summary");
      }
      target = target || cont.querySelector("[data-select-master]");
      if (target) target.focus({ preventScroll: true });
    }

    function refreshReview() {
      if (pendingWrites || dismissPending || pageLoading) {
        refreshNeeded = true;
        return Promise.resolve(false);
      }
      refreshNeeded = false;
      return loadPage(curPage(), curQuery(), curTab(), "replace", true);
    }

    // Only the newest request may replace the page.
    function loadPage(page, query, tab, mode, preservePlace) {
      var gen = ++loadGen;
      var requestedQuery = (query || "").trim();
      var requestedTab = tab === undefined ? curTab() : tab;
      if (pendingWrites) {
        pageLoading = true;
        syncActions();
        return choiceQueue.then(function () {
          if (gen !== loadGen) return false;
          return loadPage(page, requestedQuery, requestedTab, mode, preservePlace);
        });
      }
      loading = false;   // a page swap supersedes any in-flight append
      loadingPromise = null;
      if (loadError) loadError.remove();
      pageLoading = true;
      pendingTab = requestedTab;
      syncActions();
      var anchors = [];
      var moved = false;
      function noteMove() { moved = true; }
      var moveEvents = ["wheel", "touchmove", "pointerdown", "keydown"];
      if (preservePlace) {
        var sticky = cont.querySelector(".ql-review-sticky");
        var bar = document.querySelector(".ql-mobilebar");
        var top = Math.max(sticky ? sticky.getBoundingClientRect().bottom : 0,
                           bar ? bar.getBoundingClientRect().bottom : 0);
        var groups = Array.from(cont.querySelectorAll("details[data-artist]"));
        var index = groups.findIndex(function (d) { return d.getBoundingClientRect().bottom > top; });
        if (index >= 0) {
          anchors = groups.slice(index).concat(groups.slice(0, index).reverse()).map(function (d) {
            return { artist: d.dataset.artist, top: d.getBoundingClientRect().top };
          });
        }
        moveEvents.forEach(function (name) { window.addEventListener(name, noteMove, { passive: true }); });
      }
      // An old Load More request must not supersede this page change.
      var staleMore = document.getElementById("review-loadmore");
      var staleMoreButton = staleMore && staleMore.querySelector("button");
      if (staleMoreButton) staleMoreButton.disabled = true;
      var url = "/jobs/" + id + "/review?page=" + (page || 1) +
                "&q=" + encodeURIComponent(requestedQuery) +
                "&tab=" + encodeURIComponent(requestedTab);
      if (preservePlace) url += "&through=1&anchor=" + encodeURIComponent(anchors.length ? anchors[0].artist : "");
      return sessionFetch(url)
        .then(function (r) { return r.ok ? r.text() : Promise.reject("response"); })
        .then(function (txt) {
          if (gen !== loadGen) return false;
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
              var tabConfirm = submit && submit.getAttribute("data-confirm-" + requestedTab);
              if (tabConfirm) {
                submit.setAttribute("data-confirm", tabConfirm);
                var tabMin = submit.getAttribute("data-confirm-min-" + requestedTab);
                if (tabMin) submit.setAttribute("data-confirm-min", tabMin);
                else submit.removeAttribute("data-confirm-min");
              }
              cont.querySelectorAll("[data-review-switches]").forEach(function (s) {
                s.classList.toggle("hidden", s.getAttribute("data-review-switches") !== requestedTab);
              });
              syncInclude();
            }
            syncUrl(requestedTab, requestedQuery, mode);
            loadedQuery = requestedQuery;
            if (filterInput) filterInput.value = requestedQuery;
            var queryField = document.getElementById("review-q-field");
            if (queryField) queryField.value = requestedQuery;
            var focusSpot = pendingReviewFocus || reviewFocusSpot(document.activeElement);
            pendingReviewFocus = null;
            host.innerHTML = txt;
            applyCounts(reviewPageCounts(host.querySelector("#review-groups")));
            host.querySelectorAll("details[data-artist]").forEach(function (d) {
              if (d.dataset.artist && openArtists[d.dataset.artist]) d.open = true;
            });
            if (window.htmx) window.htmx.process(host);
            updateHideLabels();
            updateArtistChecks();
            syncExpandAll();
            syncSelectPage();
            updateDismissRest();
            var waits = [];
            host.querySelectorAll("details[data-artist][open]").forEach(function (d) {
              waits.push(loadGroupItems(d, false));
            });
            return Promise.all(waits).then(function () {
              if (gen !== loadGen) return false;
              if (!moved) {
                restoreReviewFocus(focusSpot);
                for (var i = 0; i < anchors.length; i++) {
                  var anchor = host.querySelector('details[data-artist="' + CSS.escape(anchors[i].artist) + '"]');
                  if (anchor) {
                    window.scrollBy(0, anchor.getBoundingClientRect().top - anchors[i].top);
                    break;
                  }
                }
              }
              return true;
            });
          } else {
            pageLoading = false;
            pendingTab = null;
            if (staleMoreButton) staleMoreButton.disabled = false;
          }
          return false;
        })
        .catch(function (why) {
          if (why !== "navigation" && gen === loadGen) {
            pageLoading = false;
            pendingTab = null;
            if (staleMoreButton) staleMoreButton.disabled = false;
            if (filterInput) filterInput.value = loadedQuery;
            loadError = showToast("Couldn't load those results. Check your connection.", "error");
          }
          return false;
        }).then(function (loaded) {
          moveEvents.forEach(function (name) { window.removeEventListener(name, noteMove); });
          syncActions();
          if (loaded && refreshNeeded && !pendingWrites) refreshReview();
          return loaded;
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
      if (t.closest(".ql-review-dismissed-link")) recordPlace();
      if (t.closest("[data-hide]")) return;
      // The artist checkbox sits inside a <summary>: its own activation runs
      // instead of the summary's, but stop the bubble so nothing else fires.
      if (t.closest("[data-artist-select], .ql-review-artist-hit")) {
        e.stopPropagation();
        return;
      }
      var pageBtn = t.closest("[data-select-page]");
      if (pageBtn) { bulkSelect(pageBtn.getAttribute("data-select-page") === "1", "page"); return; }
      var more = t.closest("[data-load-more]");
      if (more) {
        loadMore(parseInt(more.getAttribute("data-next-page") || "2", 10));
        return;
      }
      var switchBtn = t.closest("[data-review-switch]");
      if (switchBtn) { switchReleaseType(switchBtn); return; }
      var tabBtn = t.closest("[data-review-tab]");
      if (tabBtn && (
        !tabBtn.classList.contains("is-active") || pageLoading
      )) {
        cancelFilter();
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
        syncExpandAll();
        return;
      }
    });
    cont.addEventListener("change", function (evt) {
      var master = evt.target.closest && evt.target.closest("[data-select-master]");
      if (master) bulkSelect(master.checked, "all");
    });
    if (dismissRest) {
      dismissRest.addEventListener("click", function () {
        if (actionsBlocked()) return;
        var requestTab = curTab();
        var requestQuery = curQuery();
        var selectionToken = form.elements.namedItem("selection_token").value;
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
          if (!ok || actionsBlocked() || requestTab !== curTab() || requestQuery !== curQuery()) return;
          dismissPending = true;
          choiceVersion += 1;
          syncActions();
          dismissRest.textContent = dismissBusyLabel();
          post("/jobs/" + id + "/dismiss-rest",
               "tab=" + encodeURIComponent(requestTab) +
               "&selection_token=" + encodeURIComponent(selectionToken) +
               "&q=" + encodeURIComponent(requestQuery))
            .then(function (r) {
              if (r.status === 403) return Promise.reject("stale");
              if (r.status === 409) return Promise.reject("changed");
              return r.ok ? r.json() : Promise.reject();
            })
            .then(function (c) {
              if (c.review_done) { location.reload(); return; }
              dismissPending = false;
              applyCounts(c);
              refreshReview();
              if (c.finalize_failed) {
                showToast("The albums were dismissed, but the finished review couldn't be saved. Check the data folder and reload.", "error", "review-save-" + id);
              } else {
                showToast(c.hidden ? dismissToast(c.hidden)
                                   : "Nothing to dismiss on this filter.",
                          c.hidden ? "success" : "info");
              }
            })
            .catch(function (why) {
              dismissPending = false;
              choicesUnknown = true;
              syncActions();
              if (why === "navigation") return;
              if (why === "stale") { pageWentStale(); return; }
              if (why === "changed") showToast("The review changed. Check your selection and try again.", "warning");
              recoverChoices();
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
      // Back press undoes the query whatever its length, unless that entry
      // held another filter (a tab switch since); then it is replaced.
      if (!query && onFilterEntry() && !filterFrom()) { window.history.back(); return; }
      loadPage(1, query, pendingTab === null ? curTab() : pendingTab, "filter");
    }
    if (filterInput) {
      filterInput.addEventListener("input", function () {
        cancelFilter();
        loadGen += 1;
        pageLoading = true;
        syncActions();
        filterTimer = setTimeout(function () {
          filterTimer = null;
          applyFilter();
        }, 250);
      });
    }
    // Enter in the filter or on a checkbox would submit the form through its
    // first submit button, which is Discard.
    form.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault();
    });

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
    setHiddenReturn(curTab(), loadedQuery);
    if (window.history.replaceState) {
      window.history.replaceState(
        reviewEntry(curTab(), loadedQuery, false), "", window.location.href
      );
    }

    // Refresh counts and reload a page if hiding empties it.
    function onQlHidden(e) {
      var d = e.detail || {};
      dismissPending = false;
      dismissRequest = null;
      if (d.counts) applyCounts(d.counts);
      var box = pageBox();
      var empty = box && box.querySelectorAll(
        ":scope > .ql-review-group-shell, :scope > details").length === 0;
      var spot = dismissFocus;
      dismissFocus = null;
      if (empty || document.getElementById("review-loadmore")) {
        pendingReviewFocus = spot;
        refreshReview();
      } else {
        updateHideLabels();
        syncExpandAll();
        restoreReviewFocus(spot);
      }
    }
    document.body.addEventListener("qlHidden", onQlHidden);
    // A group's Dismiss redraws the group, or removes it, once its request
    // is done; note where focus was before the button is disabled.
    var dismissFocus = null;
    var dismissRequest = null;
    function onDismissRequest(e) {
      var btn = e.detail && e.detail.elt;
      if (!btn || !btn.matches || !btn.matches("[data-hide]") || !cont.contains(btn)) return;
      if (actionsBlocked()) { e.preventDefault(); return; }
      dismissPending = true;
      dismissRequest = e.detail.xhr;
      choiceVersion += 1;
      var active = document.activeElement;
      dismissFocus = active === btn || active.matches("[data-hide-rest]")
        ? reviewFocusSpot(active) : null;
      syncActions();
    }
    function afterDismissRequest(e) {
      if (!e.detail || e.detail.xhr !== dismissRequest) return;
      if (e.type === "htmx:afterRequest" && e.detail.successful) return;
      dismissRequest = null;
      dismissPending = false;
      if (e.type === "htmx:afterRequest") {
        choicesUnknown = true;
        recoverChoices();
      } else if (refreshNeeded) {
        refreshReview();
      }
      syncActions();
    }
    document.addEventListener("htmx:beforeRequest", onDismissRequest);
    document.addEventListener("htmx:afterRequest", afterDismissRequest);
    document.addEventListener("htmx:afterSwap", afterDismissRequest);

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
    syncInclude();
    updateDismissRest();

    // Keep review pages in sync across tabs.
    var streamTab = curTab();
    var rsrc = new EventSource("/api/jobs/" + id + "/review-stream" +
                               (streamTab ? "?tab=" + encodeURIComponent(streamTab) : ""));
    var streamInterrupted = false;
    var reviewNavigating = false;
    function beginReviewNavigation(e) {
      if (actionsBlocked()) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      reviewNavigating = true;
    }
    function recoverOnConnection() {
      if (choicesUnknown) recoverChoices();
    }
    window.addEventListener("online", recoverOnConnection);
    function shutReview() {
      try { rsrc.close(); } catch (e) {}
      form.removeEventListener("submit", beginReviewNavigation, true);
      document.body.removeEventListener("qlHidden", onQlHidden);
      document.removeEventListener("htmx:beforeRequest", onDismissRequest);
      document.removeEventListener("htmx:afterRequest", afterDismissRequest);
      document.removeEventListener("htmx:afterSwap", afterDismissRequest);
      document.removeEventListener("htmx:beforeSwap", onReviewSwap);
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("scroll", savePlace);
      window.removeEventListener("pagehide", recordPlace);
      window.removeEventListener("online", recoverOnConnection);
      if (placeTimer) clearTimeout(placeTimer);
    }
    form.addEventListener("submit", beginReviewNavigation, true);
    function onReviewSwap(e) {
      if (e.detail && e.detail.target && e.detail.target.id === "job-content") shutReview();
    }
    document.addEventListener("htmx:beforeSwap", onReviewSwap);
    rsrc.addEventListener("review", function (e) {
      if ((e.data || "") === "save_failed") {
        showToast("Your latest choice couldn't be saved to disk. It may not survive a restart. Check the data folder.", "error", "review-save-" + id);
        return;
      }
      // Our own change echoing back. The DOM and counts are already current
      // from the action's response, and reloading now would swap the page out
      // from under the user's next click.
      if (reviewNavigating || (e.data || "") === TAB_ID) return;
      refreshReview();
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
      if (reviewNavigating) return;
      // A page that hosts a review (Library, Repair) shows whichever job owns
      // it now, which after a split download is the rest of the review, not
      // this job.
      var jc = document.getElementById("job-content");
      if (window.htmx && !(jc && jc.dataset.embedded)) {
        window.htmx.ajax("GET", "/jobs/" + id + "/content", { target: "#job-content", swap: "outerHTML" });
      } else { location.reload(); }
    });
    rsrc.addEventListener("auth", function () {
      shutReview();
      returnToLogin();
    });
    rsrc.addEventListener("snapshot", function (e) {
      var snapshot = JSON.parse(e.data);
      var changed = streamInterrupted || streamTab !== curTab() ||
        snapshot.total !== lastCounts.total ||
        snapshot.selection_token !== form.elements.namedItem("selection_token").value;
      streamInterrupted = false;
      if (changed) refreshReview();
    });
    rsrc.onerror = function () {
      streamInterrupted = true;
      if (rsrc.readyState === EventSource.CLOSED) shutReview();
    };
  }

  function initAll() {
    settleServerToasts();
    autoDismissFlashes();
    normalizeFlashAnnouncements();
    initCoverFallbacks();
    initSearchResults();
    if (!document.querySelector("[data-search-results-root]")
        && document.querySelector("[data-search-item]")) {
      refreshSearchAvailability();
    }
    initStreamCards();
    initJobContent();
  }

  var keyboardSearchForm = null;
  var pendingSearchFocus = null;
  var activeSearchRequests = [];
  var activeHistoryRequest = null;
  var latestSearchSubmission = 0;

  function cancelHistoryRequest() {
    if (activeHistoryRequest) activeHistoryRequest.abort();
    activeHistoryRequest = null;
  }

  function cancelSearchRequests() {
    clearSearchMoreError();
    latestSearchSubmission += 1;
    cancelHistoryRequest();
    activeSearchRequests.slice().forEach(function (xhr) { xhr.abort(); });
  }

  window.addEventListener("popstate", cancelSearchRequests, true);

  document.addEventListener("htmx:historyCacheMiss", function (event) {
    cancelHistoryRequest();
    var xhr = event.detail.xhr;
    activeHistoryRequest = xhr;
    xhr.addEventListener("loadend", function () {
      if (activeHistoryRequest === xhr) activeHistoryRequest = null;
    });
  });

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
    cancelHistoryRequest();
    latestSearchSubmission += 1;
    form.dataset.searchSubmission = latestSearchSubmission;
  }, true);

  document.addEventListener("htmx:beforeRequest", function (event) {
    if (libraryRefreshRevision(event.detail) && event.detail.target.id === "search-results") {
      event.detail.xhr.qlSearchSubmission = latestSearchSubmission;
      return;
    }
    var form = searchRequestForm(event);
    if (!form || !event.detail.xhr) return;
    cancelHistoryRequest();
    // The same save as leaving the page: on desktop the list scrolls in its
    // own panel, and Back should find it where it was.
    (searchExitSave || saveSearchSnapshot)();
    var row = form.closest("[data-search-item]");
    if (row && form.matches(".ql-result-link-form")) {
      updateSearchRecord(searchStateFromUrl(), { opener: row.dataset.searchKey });
    }
    // "Search results" returns to where the list was left, as Back does.
    if (form.matches(".ql-search-back-form")) {
      var fields = form.elements;
      var left = readSearchRecord(searchStateId(
        fields.kind.value, fields.q.value, "", fields.album_id && fields.album_id.value));
      pendingSearchRestoreY = left && typeof left.scrollY === "number" ? left.scrollY : null;
    }
    var generation = parseInt(form.dataset.searchSubmission || "0", 10);
    if (!generation || activeSearchRequests.some(function (xhr) {
      return xhr.qlSearchSubmission === generation;
    })) {
      latestSearchSubmission += 1;
      generation = latestSearchSubmission;
      form.dataset.searchSubmission = generation;
    }
    event.detail.xhr.qlSearchSubmission = generation;
    event.detail.xhr.qlSearchResultLink = !form.matches(".ql-search-form");
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

  function setSearchForm(kind, q) {
    var form = document.querySelector(".ql-search-form");
    if (!form) return;
    if (["artist", "album", "track"].indexOf(kind) === -1) kind = "artist";
    var query = form.querySelector('input[name="q"]');
    if (query) query.value = q || "";
    form.querySelectorAll('input[name="kind"]').forEach(function (radio) {
      radio.checked = radio.value === kind;
    });
    syncSearchLabel(form);
  }

  function restoreSearchFormFromUrl() {
    if (typeof URL !== "function") return;
    try {
      var url = new URL(location.href);
      setSearchForm(url.searchParams.get("kind"), url.searchParams.get("q"));
    } catch (error) { /* leave the form alone if the address cannot be read */ }
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    if (!event.detail.xhr) return;
    var index = activeSearchRequests.indexOf(event.detail.xhr);
    if (index === -1) return;
    activeSearchRequests.splice(index, 1);
    if (activeSearchRequests.length) return;
    var results = document.getElementById("search-results");
    var status = document.getElementById("search-status");
    if (results) results.setAttribute("aria-busy", "false");
    if (status) status.textContent = "";
    if (!event.detail.successful) pendingSearchRestoreY = null;
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var button = event.target && event.target.closest && event.target.closest("#search-results button[type=submit]");
    if (!button || !button.form || !button.form.matches(".ql-result-action-form, .ql-result-link-form, .ql-search-back-form")) return;
    keyboardSearchForm = button.form;
  }, true);

  document.addEventListener("pointerdown", function () {
    keyboardSearchForm = null;
  }, true);

  document.addEventListener("htmx:beforeRequest", function (event) {
    var source = event.detail && event.detail.elt;
    var form = source && (source.matches("form") ? source : source.closest("form"));
    if (!form || form !== keyboardSearchForm) return;
    pendingSearchFocus = {
      form: form,
      returning: form.classList.contains("ql-search-back-form"),
      albumId: form.dataset.searchAlbumId,
      artistId: form.dataset.searchArtistId
    };
    keyboardSearchForm = null;
  });

  function restoreSearchFocus(event) {
    var target = event.detail && event.detail.target;
    if (!pendingSearchFocus || !target || target.id !== "search-results") return;
    var pending = pendingSearchFocus;
    pendingSearchFocus = null;
    var root = target.querySelector("[data-search-results-root]");
    function focusResult() {
      var active = document.activeElement;
      if (!target.isConnected || (active !== document.body && active !== target)) return;
      var control = null;
      if (pending.returning) {
        var saved = readSearchRecord(searchResponseState(target)) || {};
        var field = pending.albumId ? "album_id" : "artist_id";
        var id = pending.albumId || pending.artistId;
        var buttons = target.querySelectorAll(".ql-result-link-form button, .ql-result-action-form button");
        var matches = Array.prototype.filter.call(buttons, function (button) {
          var input = button.form.elements[field];
          return input && input.value === id && button.getClientRects().length;
        });
        control = matches.find(function (button) {
          var row = button.closest("[data-search-item]");
          return row && row.dataset.searchKey === saved.opener;
        }) || matches[0];
      } else {
        control = target.querySelector(".ql-search-back");
      }
      (control || target).focus();
    }
    Promise.resolve(root && root.qlSearchRestored).then(function () {
      requestAnimationFrame(focusResult);
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

  var diagnosticActionRows = ".ql-diagnostic-row:has(button[data-confirm]:enabled)";
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var target = evt.detail.target;
    var button = document.activeElement;
    if (!target || target.id !== "diagnostics-list" || !target.contains(button)
        || !button.matches("button[data-confirm]:focus-visible")) return;
    var rows = Array.from(target.querySelectorAll(diagnosticActionRows));
    evt.detail.xhr.qlDiagnosticFocus = {
      button: button, row: rows.indexOf(button.closest(".ql-diagnostic-row"))
    };
  });

  document.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail.target;
    if (target && target.id === "diagnostics-list") {
      evt.detail.xhr.qlChecksOpen = !!target.querySelector("details[open]");
      var spot = evt.detail.xhr.qlDiagnosticFocus;
      if (spot && document.activeElement !== document.body && document.activeElement !== spot.button) {
        evt.detail.xhr.qlDiagnosticFocus = null;
      }
    }
  });

  function announceDiagnosticsSwap(event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "diagnostics-list") return;
    if (event.detail.xhr.qlChecksOpen) {
      var checks = target.querySelector("details");
      if (checks) checks.open = true;
    }
    var spot = event.detail.xhr.qlDiagnosticFocus;
    if (spot && document.activeElement === document.body) {
      var rows = target.querySelectorAll(diagnosticActionRows);
      var row = rows[Math.min(spot.row, rows.length - 1)];
      var button = redrawnConfirmControl(spot.button)
        || (row && row.querySelector("button[data-confirm]:enabled"))
        || document.querySelector('[hx-get="/api/diagnostics"]');
      if (button) button.focus();
    }
    // Recheck alone. Restore, Remove and Delete swap the same list, and each
    // already says what it did; a second, generic notice beside a refusal
    // reads like the action went through.
    var req = event.detail && event.detail.requestConfig;
    if (!req || String(req.verb).toLowerCase() !== "get") return;
    var status = document.getElementById("diagnostics-status");
    if (status) status.textContent = "Diagnostics refreshed.";
    // The status line above is sr-only, so a sighted user gets a toast.
    showToast("Diagnostics refreshed.", "info");
  }

  function rememberSearchSwap(event) {
    var target = event.detail && event.detail.target;
    if (!target || target.id !== "search-results") return;
    clearSearchMoreError();
    // An artist link or "Search results" runs a search of its own; the box
    // and tab say which one, so Search repeats what is listed.
    var source = event.detail.requestConfig && event.detail.requestConfig.elt;
    if (source && source.matches
        && source.matches(".ql-result-action-form, .ql-result-link-form, .ql-search-back-form")) {
      var kindField = source.querySelector('input[name="kind"]');
      var queryField = source.querySelector('input[name="q"]');
      if (kindField && queryField) setSearchForm(kindField.value, queryField.value);
    }
    if (!target.querySelector("[data-search-results-root]")) {
      searchExitSave = saveSearchSnapshot;
    }
    searchSnapshotsInvalidated = false;
    saveSearchSnapshot();
    if (searchPositionRestoring || pendingSearchRestoreY !== null) {
      finishSearchRestore(readSearchRecord(searchResponseState(target)));
    }
  }

  function requestSearchAtAddress(results, headers) {
    var params = new URLSearchParams(location.search);
    var form = document.querySelector(".ql-search-form");
    var values = { _csrf_token: form.elements._csrf_token.value };
    ["q", "kind", "artist_id", "artist_name", "album_id", "back_kind",
      "back_q", "back_album_id", "back_track_q"].forEach(function (key) {
      values[key] = params.get(key) || "";
    });
    return window.htmx.ajax("POST", "/search", {
      source: headers ? results : form, target: results,
      swap: "innerHTML", values: values,
      headers: Object.assign({ "HX-History-Restore-Request": "true" }, headers)
    }).catch(function () {});
  }

  function refreshLibraryResults(revision) {
    if (libraryRevision !== revision) {
      libraryRevision = revision;
      keptSearchViews = {};
      failedLibraryReads = new WeakMap();
    }
    var view = document.querySelector("[data-search-response-state], #discover-body");
    if (!view || view.dataset.libraryRevision === revision || !window.htmx
        || activeSearchRequests.length || document.querySelector(
          "#ql-confirm[open], .ql-mobile-drawer[open], [data-search-more-url]:disabled, [data-search-bulk-download]:disabled")) return;
    var headers = { "X-QL-Refresh-Library": revision };
    var tracks = document.querySelector(".ql-track-sheet[open]");
    var list = tracks && tracks.querySelector(".ql-track-sheet-list");
    var target = list || (view.id === "discover-body" ? view : view.parentElement);
    // Retry a failed catalogue read once, then wait until the tab is
    // revisited or the library changes again.
    if (failedLibraryReads.get(target) >= 2 || target.classList.contains("htmx-request")) return;
    if (tracks) {
      var toggle = sheetToggle(tracks.id);
      if (tracks.dataset.libraryRevision === revision || toggle.classList.contains("htmx-request")) return;
      window.htmx.ajax("GET", toggle.getAttribute("hx-get"), {
        source: list, target: list, swap: "innerHTML", headers: headers
      }).catch(function () {});
    } else if (view.id === "discover-body") {
      if (discoverWrites || document.querySelector("[data-discover-undo]")) return;
      view.querySelectorAll("[hx-preserve]").forEach(function (panel) {
        if (!panel.firstElementChild) return;
        if (panel.classList.contains("hidden")) { panel.replaceChildren(); return; }
        var decade = panel.querySelector('[data-decade][aria-pressed="true"]');
        window.htmx.ajax("GET", sheetToggle(panel.id).getAttribute("hx-get"), {
          source: panel, target: panel, swap: "innerHTML", headers: headers
        }).then(function () {
          var chip = decade && panel.querySelector('[data-decade="' + CSS.escape(decade.dataset.decade) + '"]');
          if (chip) chip.click();
        }).catch(function () {});
      });
      window.htmx.ajax("GET", location.pathname + location.search, {
        source: view, target: view, select: "#discover-body", swap: "outerHTML", headers: headers
      }).catch(function () {});
    } else if (searchResponseState(view.parentElement) === searchStateFromUrl()) {
      requestSearchAtAddress(view.parentElement, headers);
    }
  }

  document.addEventListener("close", function (event) {
    if (event.target.matches(".ql-release-sheet, #ql-confirm") && libraryRevision !== undefined) {
      refreshLibraryResults(libraryRevision);
    }
  }, true);

  function libraryRefreshRevision(detail) {
    var headers = detail.requestConfig && detail.requestConfig.headers;
    return headers && headers["X-QL-Refresh-Library"];
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    var detail = event.detail;
    var revision = libraryRefreshRevision(detail);
    if (!revision || revision !== libraryRevision) return;
    if (detail.failed || detail.xhr.qlLibraryReadFailed) {
      failedLibraryReads.set(detail.target, (failedLibraryReads.get(detail.target) || 0) + 1);
    }
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail;
    var revision = libraryRefreshRevision(detail);
    if (!revision || !detail.shouldSwap) return;
    var trackList = detail.target.matches(".ql-track-sheet-list");
    if ((trackList && detail.xhr.responseText.indexOf("data-flash") >= 0)
        || (detail.target.id === "search-results" && detail.xhr.responseText.indexOf("ql-search-error") >= 0)) {
      detail.xhr.qlLibraryReadFailed = true;
      detail.shouldSwap = false;
      return;
    }
    if (trackList) {
      detail.target.closest("dialog").dataset.libraryRevision = revision;
      return;
    }
    if (detail.target.id !== "search-results") return;
    if (detail.xhr.qlSearchSubmission !== latestSearchSubmission
        || document.querySelector("#ql-confirm[open], .ql-track-sheet[open], .ql-mobile-drawer[open]")) {
      detail.shouldSwap = false;
      return;
    }
    (searchExitSave || saveSearchSnapshot)();
    pendingSearchRestoreY = window.scrollY;
    var versions = detail.target.querySelector(".ql-version-sheet[open]");
    if (versions) detail.xhr.qlOpenVersions = {
      id: versions.id, y: versions.querySelector(".ql-release-sheet-list").scrollTop,
      focus: document.activeElement.getAttribute("aria-label")
    };
    detail.xhr.qlExpanded = Array.from(detail.target.querySelectorAll(
      '[aria-expanded="true"][data-version-toggle], [aria-expanded="true"][data-tracks-toggle]'
    ), function (button) { return button.getAttribute("aria-controls"); });
  });
  document.addEventListener("htmx:afterSettle", function (event) {
    var saved = event.detail.xhr && event.detail.xhr.qlOpenVersions;
    var expanded = event.detail.xhr && event.detail.xhr.qlExpanded;
    if (!saved && !expanded) return;
    var root = event.detail.target.querySelector("[data-search-results-root]");
    Promise.resolve(root && root.qlSearchRestored).then(function () {
      (expanded || []).forEach(function (id) {
        var toggle = sheetToggle(id);
        if (toggle && toggle.getAttribute("aria-expanded") === "false") toggle.click();
      });
      if (saved && history.state && history.state.qlSheet === saved.id) {
        sheetOnLoad = saved.id;
        restorePageSheet();
        var panel = document.getElementById(saved.id);
        if (panel) {
          var control = saved.focus && panel.querySelector('[aria-label="' + CSS.escape(saved.focus) + '"]');
          if (control && !control.disabled) control.focus({ preventScroll: true });
          panel.querySelector(".ql-release-sheet-list").scrollTop = saved.y;
        }
      }
    });
  });

  function restoreSearchHistory() {
    clearSearchMoreError();
    restoreSearchFormFromUrl();
    var results = document.getElementById("search-results");
    if (!results) return;
    var state = searchStateFromUrl();
    var marker = results.querySelector("[data-search-response-state]");
    if (state && (searchResponseState(results) !== state
        || (marker && marker.dataset.libraryRevision !== libraryRevision))) {
      var record = readSearchRecord(state);
      pendingSearchRestoreY = record && typeof record.scrollY === "number"
        ? record.scrollY : 0;
      var snapshot = keptSearchViews[searchSnapshotKey(state)] || readSearchSnapshot(state);
      if (snapshot) {
        results.innerHTML = snapshot;
        window.htmx.process(results);
        finishSearchRestore(record);
      } else {
        results.innerHTML = "";
        requestSearchAtAddress(results);
        return;
      }
    }
    results.setAttribute("aria-busy", "false");
    var status = document.getElementById("search-status");
    if (status) status.textContent = "";
    results.querySelectorAll("[data-search-wired]").forEach(function (node) {
      node.removeAttribute("data-search-wired");
    });
    results.querySelectorAll("[data-cover-fallback-wired]").forEach(function (node) {
      node.removeAttribute("data-cover-fallback-wired");
    });
    initSearchResults();
    initCoverFallbacks();
  }

  // Re-scan swapped content for flashes and streams.
  document.addEventListener("htmx:afterSwap", initAll);
  // A refusal can arrive with nothing else swapped.
  document.addEventListener("htmx:oobAfterSwap", function () {
    settleServerToasts();
    normalizeFlashAnnouncements();
  });
  document.addEventListener("htmx:afterSwap", rememberSearchSwap);
  document.addEventListener("htmx:afterSwap", restoreSearchFocus);
  document.addEventListener("htmx:afterSwap", revealSearchFeedback);
  document.addEventListener("htmx:afterSwap", announceDiagnosticsSwap);
  document.addEventListener("htmx:historyRestore", restoreSearchHistory);
  // Drop restored result IDs once the replay is over, whether or not it
  // worked. Kept after a failure, the id would ride along with the next
  // search the user typed and open the album they had just left.
  document.addEventListener("htmx:afterRequest", function (e) {
    var form = e.target && e.target.closest && e.target.closest(".ql-search-form");
    if (!form) return;
    form.querySelectorAll("[data-deep-link]").forEach(function (el) { el.remove(); });
  });
  // One album downloaded from a search row updates the Queue badge at once,
  // like a bulk download, instead of at the next poll.
  document.addEventListener("htmx:afterRequest", function (e) {
    var source = e.target;
    if (!source || !source.closest) return;
    if (!source.closest("[data-search-download-form]")) return;
    if (e.detail && e.detail.successful === false) return;
    if (window.qlRefreshQueueBadge) window.qlRefreshQueueBadge();
  });
  cleanFlashUrl();
  function actionBarInset(actions) {
    if (!actions || !actions.offsetHeight) return 0;
    var style = getComputedStyle(actions);
    return style.position === "fixed" || style.position === "sticky"
      ? actions.offsetHeight + parseFloat(style.bottom) : 0;
  }

  // The sticky chrome (review summary row, review footer, toasts, search bar)
  // offsets itself by the header and tab-bar heights. Those vary with the
  // safe-area insets and the font, so measure the real elements and let the
  // CSS read the result; the stylesheet's rem values are fallbacks for the
  // moment before this runs.
  (function () {
    var actionBar = null;
    var observer = new ResizeObserver(apply);
    function apply() {
      var root = document.documentElement;
      var bar = document.querySelector(".ql-mobilebar");
      var tabs = document.querySelector(".ql-tabbar");
      if (bar && bar.offsetHeight) {
        root.style.setProperty("--ql-mobilebar-h", bar.offsetHeight + "px");
      }
      if (tabs && tabs.offsetHeight) {
        root.style.setProperty("--ql-tabbar-h", tabs.offsetHeight + "px");
      } else if (drawerScrollY !== null) {
        window.qlCloseDropdowns();
      }
      var actions = document.querySelector(".ql-bulk-bar, .ql-review-footer");
      if (actions !== actionBar) {
        observer.disconnect();
        actionBar = actions;
        if (actions) observer.observe(actions);
      }
      root.style.setProperty("--ql-action-bar-inset", actionBarInset(actions) + "px");
    }
    window.addEventListener("resize", apply);
    window.addEventListener("load", apply);
    document.addEventListener("htmx:afterSwap", apply);
    document.addEventListener("htmx:historyRestore", apply);
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", apply);
    } else {
      apply();
    }
  })();
  // The browser scrolls a control that Tab reaches into the window, not out
  // from under the header, the tab bar or a review's footer.
  document.addEventListener("focusin", function (e) {
    var el = e.target;
    if (!el.matches || !el.matches(":focus-visible")) return;
    requestAnimationFrame(function () {
      if (document.activeElement !== el) return;
      var r = el.getBoundingClientRect();
      var hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!hit || el.contains(hit) || hit.contains(el)) return;
      var bar = hit;
      while (bar && !/^(fixed|sticky)$/.test(getComputedStyle(bar).position)) {
        bar = bar.parentElement;
      }
      if (!bar || bar.contains(el)) return;
      var b = bar.getBoundingClientRect();
      if (b.top > window.innerHeight / 2) {
        var inset = actionBarInset(document.querySelector(".ql-bulk-bar, .ql-review-footer"));
        window.scrollBy(0, r.bottom - Math.min(b.top, window.innerHeight - inset) + 8);
      } else {
        window.scrollBy(0, r.top - b.bottom - 8);
      }
    });
  });
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
