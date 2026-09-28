self.addEventListener('push', event => {
  let payload = {}
  try { payload = event.data?.json() || {} } catch { payload = { body: event.data?.text() } }
  event.waitUntil(self.registration.showNotification(payload.title || 'Tradew setup alert', {
    body: payload.body || 'A new advisory setup was detected.',
    icon: '/notification-icon.svg',
    badge: '/notification-icon.svg',
    tag: payload.tag || 'tradew-setup',
    renotify: true,
    data: { url: payload.url || '/' },
  }))
})

self.addEventListener('notificationclick', event => {
  event.notification.close()
  const target = new URL(event.notification.data?.url || '/', self.location.origin).href
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windows => {
    const existing = windows.find(client => client.url.startsWith(self.location.origin))
    return existing ? existing.focus().then(() => existing.navigate(target)) : self.clients.openWindow(target)
  }))
})
