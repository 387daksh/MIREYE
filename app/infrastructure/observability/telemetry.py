from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

from fastapi import FastAPI, Request
from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode


correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
_tracer = trace.get_tracer("mireye")
_meter = metrics.get_meter("mireye")
_provider_credits = _meter.create_counter("mireye.provider.credits", unit="credit")
_model_tokens = _meter.create_counter("mireye.model.tokens", unit="token")
_operation_latency = _meter.create_histogram("mireye.operation.duration", unit="ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            value["trace_id"] = format(span_context.trace_id, "032x")
        for key in ("operation", "provider", "status", "duration_ms", "credits", "tokens"):
            if hasattr(record, key):
                value[key] = getattr(record, key)
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, separators=(",", ":"), default=str)


def configure_observability(settings: Any) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    if not settings.otel_enabled:
        return
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    exporter: Any
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    else:
        exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def install_http_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe_request(request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        token = correlation_id_var.set(correlation_id)
        started = time.perf_counter()
        with _tracer.start_as_current_span(f"HTTP {request.method}") as current:
            current.set_attribute("http.request.method", request.method)
            current.set_attribute("url.path", request.url.path)
            try:
                response = await call_next(request)
                current.set_attribute("http.response.status_code", response.status_code)
                response.headers["x-correlation-id"] = correlation_id
                logging.getLogger("mireye.http").info(
                    "request completed",
                    extra={
                        "operation": f"{request.method} {request.url.path}",
                        "status": response.status_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                )
                return response
            except Exception as exc:
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR))
                raise
            finally:
                _operation_latency.record((time.perf_counter() - started) * 1000, {"operation": "http.request"})
                correlation_id_var.reset(token)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    started = time.perf_counter()
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            _operation_latency.record((time.perf_counter() - started) * 1000, {"operation": name})


def record_provider_credits(provider: str, credits: int | float, operation: str) -> None:
    _provider_credits.add(credits, {"provider": provider, "operation": operation})


def record_model_usage(model: str, usage: dict[str, Any]) -> None:
    for direction, key in (("input", "input_tokens"), ("output", "output_tokens")):
        if usage.get(key):
            _model_tokens.add(int(usage[key]), {"model": model, "direction": direction})


def traced(name: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def traced_async(name: str):
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            with span(name):
                return await function(*args, **kwargs)

        return wrapped

    return decorate
