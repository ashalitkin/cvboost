/* CRM draft helpers (#39). CSP is script-src 'self': this file, no inline code, no build step. */
(function () {
  "use strict";
  function grow(t) {
    t.style.height = "auto";
    t.style.height = t.scrollHeight + 2 + "px";
  }
  document.querySelectorAll("textarea[data-autogrow]").forEach(function (t) {
    grow(t);
    window.addEventListener("resize", function () { grow(t); });
  });
  document.querySelectorAll("button[data-copy]").forEach(function (b) {
    b.hidden = false; // without JS the textarea is still select-all-able
    b.addEventListener("click", function () {
      var t = document.getElementById(b.getAttribute("data-copy"));
      if (!t) return;
      var label = b.textContent;
      function done() {
        b.textContent = "Copied";
        setTimeout(function () { b.textContent = label; }, 1500);
      }
      t.focus();
      t.select();
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(t.value).then(done, function () {
          document.execCommand("copy");
          done();
        });
      } else {
        document.execCommand("copy");
        done();
      }
    });
  });
})();
