/* Share buttons: progressive enhancement.
   Markup: <button data-share-url="…" data-share-title="…" data-share-status="id-of-live-region" hidden>
   The button stays hidden unless the browser can share (Web Share API) or copy (Clipboard API). */
(function () {
  "use strict";

  var canShare = typeof navigator.share === "function";
  var canCopy = !!(navigator.clipboard && typeof navigator.clipboard.writeText === "function");
  if (!canShare && !canCopy) return;

  var timers = new WeakMap();

  function announce(button, message) {
    var id = button.getAttribute("data-share-status");
    var region = id ? document.getElementById(id) : null;
    if (!region) return;
    region.textContent = message;
    clearTimeout(timers.get(region));
    timers.set(region, setTimeout(function () { region.textContent = ""; }, 4000));
  }

  function copy(button, url) {
    if (!canCopy) {
      announce(button, "Couldn't copy the link: " + url);
      return;
    }
    navigator.clipboard.writeText(url).then(
      function () { announce(button, "Link copied"); },
      function () { announce(button, "Couldn't copy the link: " + url); }
    );
  }

  function onClick(event) {
    var button = event.currentTarget;
    var url = button.getAttribute("data-share-url") || window.location.href;
    var title = button.getAttribute("data-share-title") || document.title;

    if (canShare) {
      navigator.share({ title: title, url: url }).catch(function (err) {
        // User dismissed the share sheet: do nothing. Anything else: fall back to copying.
        if (!err || err.name !== "AbortError") copy(button, url);
      });
    } else {
      copy(button, url);
    }
  }

  function initializeSharing() {
    var buttons = document.querySelectorAll("[data-share-url]");
    for (var i = 0; i < buttons.length; i++) {
      if (buttons[i].dataset.shareReady) continue;
      buttons[i].dataset.shareReady = "true";
      buttons[i].hidden = false;
      buttons[i].addEventListener("click", onClick);
    }
  }
  window.initializeSharing = initializeSharing;
  initializeSharing();
})();
