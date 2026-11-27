# QSR POS API: client contract

Base: same origin as the page (`/api/...`). In dev, proxy `/api` from Vite to
`http://localhost:8000` (`server.proxy` with `changeOrigin: true`) so cookies and CSRF just work.

## Session
1. `GET /api/auth/csrf/` on load, then send the `csrftoken` cookie value as `X-CSRFToken` on every POST.
2. `POST /api/auth/login/` `{username, password}` -> user `{id, username, role, is_manager}`. 401 = wrong credentials.
3. Every other endpoint returns 403 without a session.

## The rule that makes offline safe: client-generated ids
- Create the **order id (UUID)** once when the cashier starts an order.
- Create a **payment idempotency key (UUID)** once per tap of Confirm / Send prompt / Save code.
  Resend the SAME key after a timeout, double-tap or reconnect: a replay returns `200` with the original
  payment and changes nothing. A new key is a new payment.
- A failed, cancelled or timed-out STK prompt is final. "Try again" is a NEW payment with a NEW key.

## Endpoints
| Method and path | Who | Notes |
|---|---|---|
| `GET /api/menu/` | any | categories > items > modifier groups > options; unavailable items have `is_available: false` |
| `GET /api/shifts/current/` | any | `{shift: null}` when none is open |
| `POST /api/shifts/open/` `{opening_float}` | manager | 201 |
| `GET /api/orders/?status=` | any | current shift, newest first, max 100 |
| `POST /api/orders/` `{id}` | any | 201 new, 200 replay, 409 `no_open_shift` |
| `GET /api/orders/{id}/` | any | order with `items` and `payments` |
| `POST /api/orders/{id}/items/` `{menu_item, quantity?, options?: [option ids], note?}` | any | returns the order |
| `POST /api/orders/{id}/charge/` | any | -> `awaiting_payment`; 400 if empty |
| `POST /api/orders/{id}/payments/cash/` `{tendered, idempotency_key}` | any | -> `{payment, order}` |
| `POST /api/orders/{id}/payments/stk/` `{phone_number, idempotency_key}` | any | -> `{payment, order, timeout_seconds, expires_at}`; 503 `provider_unavailable` (offline) |
| `POST /api/orders/{id}/payments/manual-mpesa/` `{receipt_code, amount, phone_number?, idempotency_key}` | any | -> order `paid_pending_verification` |
| `GET /api/payments/{id}/` | any | poll about once a second while STK is open; times out after `timeout_seconds` |
| `GET /api/manager/review/` | manager | `{payments, counts: {needs_review, pending_verification}}` |
| `POST /api/payments/{id}/confirm/` `{receipt_code?}` | manager | one-tap confirm; 409 `illegal_transition` |
| `POST /api/payments/{id}/reject/` `{reason}` | manager | reopens the order if it was the last live payment |
| `POST /api/payments/{id}/defer/` | manager | leave pending |
| `POST /api/backfill/` `{id, reason, occurred_at, lines[], payment_key, cash_tendered \| mpesa_code}` | manager | paper orders after an outage, labelled `B-n` |
| `GET /api/sync/status/` | any | `{queue: {pending, processing, failed}, oldest_unsent_at, last_error, sync_enabled, pending_verification, needs_review}` |

## Errors
`{"error": {"code", "messages": [...]}}`, or `{"error": {"code": "validation_error", "fields": {...}}}`.
Codes: `validation_error` (400), `invalid_credentials` (401), `not_authenticated` / `forbidden` (403),
`not_found` (404), `no_open_shift` / `illegal_transition` (409), `provider_unavailable` (503).
Show `messages[0]` to the cashier: the backend copy is already plain language.

## Statuses
- order: `building`, `awaiting_payment`, `paid`, `paid_pending_verification` (show the amber badge)
- payment: `pending`, `confirmed`, `failed`, `timed_out`, `cancelled`, `pending_verification`, `manager_review`

## Dev only (DEBUG + mock provider)
- `POST /api/dev/mpesa/online/` `{online: false}` makes the mock Safaricom unreachable (copy D-01/M-09).
- `POST /api/dev/stk/{payment id}/simulate/` `{outcome: success | insufficient_funds | cancelled | wrong_pin | unreachable}`
- `python manage.py seed_demo --password ...` creates users `cashier` and `manager` and a demo menu.