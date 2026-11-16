(function () {
  const settings = document.querySelector('.backup-settings');
  settings.addEventListener('submit', event => {
    if (Number(settings.elements.retention_days.value) < Number(settings.dataset.currentRetention)
        && !window.confirm('Aufbewahrung verkuerzen? Aeltere Sicherungen werden nach dem naechsten erfolgreichen Backup automatisch entfernt.')) {
      event.preventDefault();
    }
  });
  const watcher = document.querySelector('[data-backup-watch="1"]');
  if (!watcher) return;
  let edited = false;
  settings.addEventListener('input', () => { edited = true; });
  const timer = window.setInterval(async () => {
    if (edited || document.hidden) return;
    try {
      const response = await fetch('/backups/status', { cache: 'no-store' });
      if (!response.ok) return;
      const state = await response.json();
      if (state.online && state.worker.status !== 'running' && !['queued', 'running'].includes(state.request.status)) {
        window.clearInterval(timer);
        window.location.reload();
      }
    } catch (_) { /* Keep the current page on a temporary connection failure. */ }
  }, 10000);
})();
