// Google Analytics (GA4) for SlimeBallBench, plus a tiny custom-event helper.
//
// sbbTrack(name, params) is always safe to call — it only sends when GA is
// active. GA itself is skipped inside an iframe (the homepage embeds the replay
// in one), so embedded views don't double-count as separate page_views.
(function () {
  window.sbbTrack = function (name, params) {
    if (typeof window.gtag === "function") window.gtag("event", name, params || {});
  };

  if (window.top !== window.self) return; // embedded (homepage replay iframe) — don't init GA

  var GA_ID = "G-X25GLNFLEK";
  var s = document.createElement("script");
  s.async = true;
  s.src = "https://www.googletagmanager.com/gtag/js?id=" + GA_ID;
  document.head.appendChild(s);

  window.dataLayer = window.dataLayer || [];
  window.gtag = function () { dataLayer.push(arguments); };
  gtag("js", new Date());
  gtag("config", GA_ID);
})();
