# Data model: US-02

All models Pydantic v2, `frozen=True`. Upstream models ignore unknown fields; outputs are wrapped in
the US-01 envelope (`source`: `inventory` | `notifications`).

## Inputs

| Model | Field | Rule |
|---|---|---|
| `ListProductsInput` | `limit` | int 1–50, default 20 |
| | `offset` | int ≥ 0, default 0 |
| `GetProductInput` | `productId` | str, `^[A-Za-z0-9_-]{1,64}$`, case kept |
| `ListMyNotificationsInput` | `limit`, `offset` | as above |
| | `orderId` | `UUID \| None` |

`extra="forbid"` on inputs, so `customerId` is rejected as `invalid_input`.

## Outputs

- **Product**: `id` str (1–64), `name` str (truncated ≤ 500, flag), `availableQuantity` int ≥ 0.
- **ProductsPage**: `items: list[Product]`, `total` int ≥ 0, `limit` int, `offset` int.
- **Notification**: `id` UUID/str, `orderId` UUID, `type`/`channel`/`status` str ≤ 64,
  `message` str (truncated ≤ 500, flag), `createdAt` UTC aware datetime (ISO 8601).
- **NotificationsPage**: `items: list[Notification]`, `total`, `limit`, `offset`.

Dropped from upstream: notification `customerId`, `sourceEventId`.

## Upstream requests

| Tool | Request | Query |
|---|---|---|
| `list_products` | `GET {INVENTORY_BASE_URL}/products` | `limit`, `offset` |
| `get_product` | `GET {INVENTORY_BASE_URL}/products/{productId}` | — |
| `list_my_notifications` | `GET {NOTIFICATIONS_BASE_URL}/notifications` | `customerId` (claim), `limit`, `offset`, `orderId?` |

Headers on all: `Authorization: Bearer <caller token>`, `X-Request-Id`, `Accept: application/json`.
