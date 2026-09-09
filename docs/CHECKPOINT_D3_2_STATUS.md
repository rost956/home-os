# Checkpoint D3.2 — Planner Web Push delivery

## Architecture

D3.2 keeps three separate concerns: `PlannerReminder` is the canonical user configuration, `PushSubscription` is one browser/device endpoint, and `PlannerReminderDelivery` is the persistent outbox/deduplication record for one reminder occurrence on one subscription. Occurrences are still derived on demand by the D2/D3.1 engine; future delivery rows are not generated months ahead.

The FastAPI lifespan starts one cancellable async scheduler task only when background jobs and configured Web Push are enabled. Each cycle calls the synchronous delivery service in a worker thread so network I/O does not block the event loop. Shutdown cancels and awaits the task. A 60-second poll and 60-minute catch-up window are defaults and are configurable.

## Data and delivery contract

`PushSubscription` has a globally unique endpoint, keys, optional user agent, created/updated/last-seen timestamps and `disabled_at`. Re-subscription upserts keys and clears `disabled_at`; multiple endpoints per user remain active independently. Unsubscribe and HTTP 404/410 responses soft-disable an endpoint so delivery history does not break.

`PlannerReminderDelivery` stores reminder ID, occurrence key, subscription ID, UTC-naive `due_at` (the established SQLite persistence convention), status, attempts, claim metadata, sent/error timestamps and audit timestamps. Its database unique constraint is:

`planner_reminder_id + occurrence_key + push_subscription_id`

The service materializes only reminders in `[now - catch-up, now]`, then atomically claims eligible `pending`/`retry` rows. A sent row is never eligible again. The claim protects overlapping workers; its five-minute expiry allows recovery after a crashed worker. External push endpoints cannot provide a truly atomic database-and-network transaction, but the claim prevents concurrent duplicate sends in the normal multi-process race.

Transient/network/5xx failures retry on later scheduler cycles, at most three total attempts. HTTP 404 and 410 are terminal for that subscription and immediately disable it. One device failure does not block other devices. Reminders older than the catch-up window are not materialized or sent. Removing a reminder or Planner series cascades its pending delivery rows.

## Web Push and UI

The existing `pywebpush` dependency sends directly to browser push endpoints with VAPID; there is no hosted notification service. Payloads contain only title, concise reminder body, server-generated Planner URL, item ID, occurrence key, deterministic notification tag and timestamp. Notes, user IDs and complete model dumps are excluded.

The service worker handles `push`, calls `showNotification`, and uses the deterministic tag for browser-level replacement in addition to server-side deduplication. `notificationclick` validates same-origin navigation, focuses an existing Home OS window when possible and otherwise opens the relevant Planner date. The registration URL is versioned as `service-worker.js?v=2` and still uses the existing non-aggressive update lifecycle.

Planner shows an “Уведомления” block with enabled, disabled, denied, unsupported and server-unavailable states. Permission is requested only from an explicit Enable click. Disable unsubscribes the current browser and soft-disables its server row. Existing browser subscriptions are resynchronized without prompting. Layout collapses to full-width controls on mobile.

## Environment and security

- `HOME_PUSH_ENABLED=false`
- `HOME_PUSH_POLL_SECONDS=60` (5–3600)
- `HOME_PUSH_CATCHUP_MINUTES=60` (1–10080)
- `HOME_VAPID_PUBLIC_KEY`
- `HOME_VAPID_PRIVATE_KEY` — PEM text, raw key supported by pywebpush, or a container-visible file path
- `HOME_VAPID_SUBJECT` — `mailto:` or HTTPS contact URI

Incomplete/invalid configuration logs a clear unavailable state, does not start the scheduler and never prevents Home OS or Planner from starting. Only the public key reaches the browser. Subscription endpoints require the authenticated session, derive ownership server-side, accept only trusted HTTPS push hosts, validate key syntax and never accept notification URLs. Logs contain delivery IDs/statuses rather than endpoint/key material.

Ordinary user JSON export continues to include Planner reminder configurations but excludes device endpoints, auth keys and delivery history. Administrative full-database backups naturally retain runtime state and remain access-controlled.

## Migration

Runtime SQLite migration adds `updated_at`, `last_seen_at` and `disabled_at` to an existing `push_subscriptions` table and creates `planner_reminder_deliveries` plus indexes/constraints. Existing users start with no new subscriptions; Planner items, recurrence and reminder configurations are unchanged. The normal pre-schema-change backup hook detects the new table/columns.

## Raspberry Pi deployment

1. Build the current image so the installed `py-vapid` CLI matches `pywebpush`: `docker compose build web`.
2. Generate keys in the writable data volume (never in Git): `docker compose run --rm --entrypoint sh web -c "cd /app/data && vapid --gen && vapid --applicationServerKey"`.
3. Keep the generated `./data/private_key.pem` with owner-only permissions. Copy the printed application server key into `HOME_VAPID_PUBLIC_KEY` in `.env` and set `HOME_VAPID_PRIVATE_KEY=/app/data/private_key.pem`.
4. Set `HOME_VAPID_SUBJECT=mailto:you@example.com` and `HOME_PUSH_ENABLED=true`. Optionally tune poll/catch-up values.
5. Restart with `docker compose up -d --build` and check `docker compose logs web` for `Planner push scheduler started` rather than an unavailable message.
6. In browser DevTools → Application, confirm `service-worker.js?v=2` controls the HTTPS page. Open Planner and click “Включить уведомления”; do not expect a permission prompt before this click.
7. Create a Planner event a few minutes ahead with a reminder, close/minimize the PWA, and verify the notification arrives and opens the relevant Planner date.

## Tests

Targeted coverage includes authentication and validation, endpoint upsert, multiple devices, owner-scoped soft-disable and fan-out, due/future/catch-up behavior, multiple reminders, recurring monthly clamping and `recurrence_until`, database uniqueness, atomic claims across sessions, repeated scheduler passes, bounded retry, 404/410 expiry, device-independent failures, reminder/series cascade deletion, cancellable scheduler shutdown, missing/disabled configuration, private-key non-exposure, user-export privacy, service worker/click handlers, UI permission placement and non-destructive migration. Tests use an injected fake sender and never access the network.

## Known limitation

Web Push requires a secure context and browser/platform support. iOS users must follow the platform's installed-PWA requirements. Delivery is best-effort according to browser push endpoint guarantees; email/Telegram fallbacks and a device-management dashboard are outside D3.2.
