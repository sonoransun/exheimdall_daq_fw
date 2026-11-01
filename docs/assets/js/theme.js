/* Optional theme toggle. The site is fully usable without JavaScript. */
(function () {
  var btn = document.querySelector(".theme-toggle");
  if (!btn) return;
  var root = document.documentElement;
  function current() {
    var t = root.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function sync() {
    var dark = current() === "dark";
    // A toggle button for "dark theme": pressed when dark is showing.
    btn.setAttribute("aria-pressed", dark ? "true" : "false");
    btn.setAttribute("title", dark ? "Switch to the light theme" : "Switch to the dark theme");
    btn.classList.toggle("is-dark", dark);
  }
  btn.hidden = false;
  sync();
  btn.addEventListener("click", function () {
    var next = current() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("hd-theme", next); } catch (e) {}
    sync();
  });
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", sync);
  }
})();
