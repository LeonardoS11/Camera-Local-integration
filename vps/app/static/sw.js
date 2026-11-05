// Service worker do painel (31/10): só mostra os avisos (Web Push) e abre o painel no evento. Sem cache offline:
// o painel é sempre o da VPS.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {t: 'Aviso', b: e.data ? e.data.text() : ''}; }
  const op = {
    body: d.b || '', icon: '/icone-192.png', badge: '/icone-badge.png', tag: d.tag || undefined, renotify: !!d.tag,
    data: d, actions: (d.a || []).slice(0, 2), requireInteraction: (d.p || 3) >= 5, timestamp: Date.now(),
    vibrate: (d.p || 3) >= 4 ? [300, 120, 300, 120, 300] : [150],
  };
  if (d.i) op.image = d.i;
  // confirma a chegada (aba Avisos → "Últimos avisos" mostra o que chegou em cada aparelho)
  const ok = d.id ? fetch('/api/push/recebido', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: d.id})}).catch(() => {}) : null;
  e.waitUntil(Promise.all([self.registration.showNotification(d.t || 'Aviso', op), ok]));
});

self.addEventListener('notificationclick', e => {
  const d = e.notification.data || {};
  e.notification.close();
  if (e.action === 'silenciar') {
    e.waitUntil(fetch('/api/alarme', {method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({casa: d.casa || 'casa', silenciar_horas: 1})}).then(r => r.ok ? self.registration.showNotification(
        'Silenciado por 1 h', {body: 'Atividade suspeita continua avisando.', icon: '/icone-192.png', badge: '/icone-badge.png', tag: 'silencio'}) : null));
    return;
  }
  const url = new URL(d.u || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async cs => {
    for (const c of cs) {
      if (new URL(c.url).origin === self.location.origin) {
        try { await c.navigate(url); } catch (_) {}
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
