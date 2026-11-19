(() => {
  if (window === window.top && ["http:", "https:"].includes(location.protocol)) {
    import('/static/browser-presence.js').then(module=>module.watchBrowserTab()).catch(()=>{});
    return;
  }
  if (parent === window || new URLSearchParams(location.search).get("embedded") !== "1") return;
  document.documentElement.classList.add("embedded");
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.source !== parent) return;
    const message = event.data;
    if (message?.type === "aruba-theme" && ["light", "dark"].includes(message.theme)) {
      if (document.documentElement.dataset.theme !== message.theme) {
        document.dispatchEvent(new CustomEvent("aruba-viewer-theme", {detail: message.theme}));
      }
    }
  });
  parent.postMessage({type:"aruba-viewer-ready",view:location.pathname.slice(1)}, location.origin);
})();
