(() => {
  if (parent === window || new URLSearchParams(location.search).get("embedded") !== "1") return;
  document.documentElement.classList.add("embedded");
  window.addEventListener("message", async event => {
    if (event.origin !== location.origin || event.source !== parent) return;
    const message = event.data;
    if (message?.type === "aruba-theme" && ["light", "dark"].includes(message.theme)) {
      document.getElementById(message.theme === "light" ? "themeLight" : "themeDark")?.click();
    }
    if (message?.type === "aruba-switch-log" && location.pathname === "/logs"
        && typeof message.text === "string" && typeof message.source === "string") {
      const source = document.getElementById("pasteSource");
      const text = document.getElementById("pasteText");
      source.value = message.source;
      text.value = message.text;
      document.getElementById("pasteApply").click();
      const error = document.getElementById("fileError");
      parent.postMessage({type:"aruba-log-loaded",ok:error.hidden,message:error.hidden ? "" : error.textContent},location.origin);
    }
    if (message?.type === "aruba-live-matrix" && location.pathname === "/csv"
        && typeof message.fileId === "string" && /^[a-f0-9]{32}$/.test(message.fileId)) {
      window.PortMonitor?.loadLive(message.fileId);
    }
  });
  parent.postMessage({type:"aruba-viewer-ready",view:location.pathname.slice(1)}, location.origin);
})();
