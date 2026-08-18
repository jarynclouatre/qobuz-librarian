// Applies the saved theme to the offline page, the same way base.html does
// for the app. Lives as its own cached file because the CSP only allows
// inline scripts with a per-request nonce, which a static page served by the
// service worker can't have.
(function () {
  try {
    var t = localStorage.getItem('ql-theme');
    if (t !== 'night' && t !== 'winter') {
      t = matchMedia('(prefers-color-scheme: light)').matches ? 'winter' : 'night';
    }
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}

  // Go back on our own once there is something to go back to. The browser's
  // online event only covers the network dropping, not the server going away,
  // so ask the health endpoint as well. The service worker served this page
  // under the address the user asked for, so a reload lands there.
  var probing = false;
  function probe() {
    if (probing || document.hidden) return;
    probing = true;
    fetch('/healthz', { cache: 'no-store' })
      .then(function (r) { if (r.ok) window.location.reload(); })
      .catch(function () {})
      .then(function () { probing = false; });
  }
  window.addEventListener('online', probe);
  document.addEventListener('visibilitychange', probe);
  setInterval(probe, 5000);

  // Retry answers, win or lose. Without this the tap only re-rendered the same
  // page from the cache and read as though nothing had happened at all.
  // This file runs in the head, before the body it is reaching into exists.
  var retrying = false;
  function wireRetry() {
    var retry = document.querySelector('[data-retry]');
    var status = document.querySelector('[data-retry-status]');
    if (!retry) return;
    retry.addEventListener('click', function (e) {
      e.preventDefault();
      if (retrying) return;
      retrying = true;
      var label = retry.textContent;
      var started = Date.now();
      retry.textContent = 'Checking…';
      if (status) status.textContent = '';
      fetch('/healthz', { cache: 'no-store' })
        .then(function (r) {
          if (r.ok) { window.location.reload(); return true; }
          return false;
        })
        .catch(function () { return false; })
        .then(function (reached) {
          if (reached) return;
          // Hold the checking state long enough to be seen: the failure comes
          // back instantly when there is nothing to connect to.
          setTimeout(function () {
            retry.textContent = label;
            retrying = false;
            if (status) {
              status.textContent = 'Still no answer. This page comes back on'
                + ' its own as soon as the server does.';
            }
          }, Math.max(0, 400 - (Date.now() - started)));
        });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireRetry);
  } else {
    wireRetry();
  }
})();
