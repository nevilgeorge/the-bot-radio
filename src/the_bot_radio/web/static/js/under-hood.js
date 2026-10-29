/* About page "How it really works" panel: progressive enhancement.
   The <details id="under-the-hood"> works without JS; this reveals its Close button
   and opens it when the URL hash is #under-the-hood. */
(function () {
  "use strict";

  var hood = document.getElementById("under-the-hood");
  if (!hood) return;
  var summary = hood.querySelector("summary");
  var close = hood.querySelector(".under-hood__close");

  if (close) {
    close.hidden = false;
    close.addEventListener("click", function () {
      hood.open = false;
      summary.focus();
    });
  }

  function openFromHash() {
    if (window.location.hash !== "#under-the-hood") return;
    hood.open = true;
    hood.scrollIntoView();
  }

  openFromHash();
  window.addEventListener("hashchange", openFromHash);
})();
