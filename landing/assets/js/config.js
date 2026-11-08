/* Where the landing's links point. Change these two values if the domains change.
   When the page is opened locally (file:// or localhost) the login buttons go to the panel's
   local dev server instead, so the whole flow can be tried on one machine. */
(function () {
  var host = location.hostname;
  var isLocal = location.protocol === 'file:' || host === 'localhost' || host === '127.0.0.1' ||
    host === '[::1]' || /\.(localhost|test)$/.test(host);

  window.RETANO_CONFIG = {
    // The Retano panel (Compose web app). "#/login" opens its sign-in page directly.
    panelUrl: isLocal ? 'http://localhost:8080/#/login' : (host.indexOf('retano360.com') !== -1 ? 'https://panel.retano360.com/#/login' : 'https://panel.retano.ir/#/login'),
    // Retano API, used by the free consultation form.
    apiBase: 'https://panel.retano.ir/api/v1'
  };

  // Lets the stylesheet hide reveal-on-scroll content only when scripts actually run.
  document.documentElement.classList.add('js');
})();
