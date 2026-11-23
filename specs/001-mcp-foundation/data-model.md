# Data model: US-01

All models Pydantic v2, `frozen=True`, `extra="forbid"` for inputs, `extra="ignore"` for upstream.

| Model | Layer | Fields | Rules |
|---|---|---|---|
| `CallerIdentity` | infrastructure | `subject: str`, `roles: frozenset[str]`, `customer_id: UUID \| None` | from verified claims `sub`, `roles`, `customerId` |
| `RequestCaller` | infrastructure | `identity`, `token: SecretStr`, `request_id: str`, `upstream_unauthenticated: bool` (mutable flag) | ContextVar, request lifetime only |
| `ToolDefinition` | tools | `name`, `allowlist_entry: AllowlistEntry`, `input_model`, `output_model`, `timeout_seconds > 0`, `max_response_bytes > 0`, `read_only: bool`, `handler` | all required; validated by `ToolRegistry.build()` |
| `AllowlistEntry` | tools | `service`, `method`, `path`, `operation` (`order` / `orders`) | must be in the `ALLOWLIST` constant |
| `GetOrderInput` | tools | `orderId: UUID` | |
| `ListMyOrdersInput` | tools | `limit: int = 20 (1..50)`, `status: OrderStatus \| None` | |
| `OrderStatus` | tools | `StrEnum`: `PENDING`, `CONFIRMED`, `CANCELLED` | |
| `OrderItem` | tools | `productId: str` (≤500), `quantity: int`, `unitPrice: float`, `subtotal: float` | |
| `Order` | tools | `orderId: UUID`, `customerId: UUID`, `status`, `totalAmount`, `placedAt`, `confirmedAt`, `cancelledAt` (UTC datetimes), `cancellationReason` (≤500), `items: list[OrderItem] \| None` | nullable fields per Orders schema (not yet projected = null) |
| `OrdersPage` | tools | `items: list[Order]`, `totalCount: int ≥ 0`, `hasNextPage: bool` | |
| `ToolEnvelope[T]` | tools | `source: "ecommerce-orders"`, `trust: "untrusted_data"`, `data: T`, `truncated: list[str]` | every success result |
| `ToolErrorResult` | tools | `error: ToolErrorCode`, `message: str` | `ToolErrorCode` StrEnum: `not_found`, `not_allowed`, `invalid_input`, `upstream_error` |

Upstream exceptions (infrastructure): `Unauthenticated`, `Forbidden`, `UpstreamError`. Auth
exceptions: `InvalidToken`, `KeysUnavailable`. Tools exception: `ToolFailure(code)`.

Mapping: `order: null` → `not_found`; `Forbidden` or missing `customerId` claim → `not_allowed`;
`Unauthenticated` → flag + HTTP 401; `UpstreamError` or output validation failure →
`upstream_error`.
