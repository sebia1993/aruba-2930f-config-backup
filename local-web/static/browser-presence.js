async function holdPresence(token, signal) {
  let delay = 250;
  while (!signal.aborted) {
    try {
      const response = await fetch('/api/browser-presence', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal,
        headers: {'Content-Type':'application/json', 'X-Local-Token':token},
        body: '{}'
      });
      if (!response.ok) return;
      const reader = response.body.getReader();
      while (!(await reader.read()).done) {
        delay = 250;
      }
    } catch {
      // A transport interruption can occur while the local server is still running.
    }
    if (signal.aborted || delay > 4000) return;
    await new Promise(resolve => setTimeout(resolve, delay));
    delay *= 2;
  }
}

if (typeof window === 'undefined') {
  const clients = new Set();
  let connection = null;
  self.onconnect = event => {
    const port = event.ports[0];
    function leave() {
      if (clients.delete(port) && !clients.size) {
        connection.abort();
        connection = null;
      }
      port.close();
    }
    port.onmessage = ({data}) => {
      if (data === 'leave') {
        leave();
      } else if (typeof data?.token === 'string' && !clients.has(port)) {
        clients.add(port);
        if (!connection) {
          connection = new AbortController();
          holdPresence(data.token, connection.signal);
        }
      }
    };
    port.addEventListener('close', leave);
  };
}

export async function watchBrowserTab(bootstrap) {
  if (window !== window.top || !['http:', 'https:'].includes(location.protocol)) return;
  if (!bootstrap) {
    const response = await fetch('/api/bootstrap', {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) return;
    bootstrap = await response.json();
  }
  const {instance, token} = bootstrap;
  if (!instance || !token) return;
  const name = 'aruba-local-browser-' + instance;
  let active = null;

  function open() {
    if (active) return;
    const controller = new AbortController();
    active = controller;
    const {signal} = controller;
    function fallback() {
      if (signal.aborted) return;
      if (navigator.locks) {
        navigator.locks.request(name, {signal}, () => holdPresence(token, signal)).catch(() => {});
      } else {
        holdPresence(token, signal);
      }
    }
    if (typeof SharedWorker === 'function') {
      try {
        // Hidden tabs share this transport without a timer or an elected tab handing it off.
        const worker = new SharedWorker(import.meta.url, {type:'module', name});
        const leave = () => {
          worker.onerror = null;
          worker.port.postMessage('leave');
          worker.port.close();
        };
        signal.addEventListener('abort', leave, {once:true});
        worker.onerror = () => {
          signal.removeEventListener('abort', leave);
          leave();
          fallback();
        };
        worker.port.postMessage({token});
        return;
      } catch {
        // Profiles that disallow shared workers can still track open documents.
      }
    }
    fallback();
  }
  function close() {
    active?.abort();
    active = null;
  }
  window.addEventListener('pagehide', close);
  window.addEventListener('pageshow', open);
  window.addEventListener('aruba-server-stopped', close, {once:true});
  open();
}
