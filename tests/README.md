# Test layers

- `tests/unit`: deterministic logic and architecture foundations without I/O.
- `tests/integration`: explicitly configured temporary storage and adapter integration.
- `tests/contract`: API and provider adapter contract checks.
- `tests/browser`: browser journeys. Live-provider tests remain opt-in.

Existing flat tests remain in place until they can be moved without losing git
history or changing fixtures. Normal tests must never call live metered providers.
