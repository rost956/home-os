from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter()


@router.get("/manifest.json", include_in_schema=False)
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "id": "/",
            "name": "Домашний сервис",
            "short_name": "Дом",
            "description": "Рецепты, покупки, бюджет и домашние планы",
            "lang": "ru",
            "start_url": "/today",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#ffffff",
            "theme_color": "#111827",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            ],
        }
    )


@router.get("/service-worker.js", include_in_schema=False)
def service_worker() -> Response:
    javascript = r"""
self.addEventListener('push', event => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch (error) {
        payload = {body: event.data ? event.data.text() : ''};
    }

    const title = payload.title || 'Домашний сервис';
    const options = {
        body: payload.body || 'Новое уведомление',
        icon: payload.icon || '/static/icon-192.png',
        badge: payload.badge || '/static/icon-192.png',
        tag: payload.tag || 'home-service',
        timestamp: Number(payload.timestamp) || Date.now(),
        renotify: true,
        requireInteraction: Boolean(payload.requireInteraction),
        data: {
            url: payload.url || '/today',
            planner_item_id: payload.planner_item_id || null,
            occurrence_key: payload.occurrence_key || null
        }
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    const requestedUrl = new URL(
        event.notification.data && event.notification.data.url ? event.notification.data.url : '/today',
        self.location.origin
    );
    const targetUrl = requestedUrl.origin === self.location.origin
        ? requestedUrl.href
        : new URL('/today', self.location.origin).href;
    event.waitUntil((async () => {
        const allClients = await clients.matchAll({type: 'window', includeUncontrolled: true});
        for (const client of allClients) {
            if ('focus' in client) {
                await client.focus();
                if ('navigate' in client) return client.navigate(targetUrl);
                return;
            }
        }
        return clients.openWindow(targetUrl);
    })());
});
""".strip()
    return Response(
        javascript,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )
