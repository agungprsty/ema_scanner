from contextvars import ContextVar

# Bound per request by RequestContextMiddleware; None outside a request (scheduler, startup)
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
request_path_var: ContextVar[str | None] = ContextVar("request_path", default=None)
