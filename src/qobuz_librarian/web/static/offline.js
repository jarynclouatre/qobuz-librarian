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
})();
