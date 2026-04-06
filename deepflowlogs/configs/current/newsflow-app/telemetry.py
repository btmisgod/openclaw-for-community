from __future__ import annotations

import socket
from contextlib import contextmanager

from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import load_settings


SETTINGS = load_settings()
_PROVIDERS: dict[str, trace.Tracer] = {}


def _detect_host_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def get_tracer(service_name: str):
    if service_name in _PROVIDERS:
        return _PROVIDERS[service_name]
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "app.host.ip": _detect_host_ip(),
            }
        )
    )
    if SETTINGS.otlp_endpoint:
        exporter = OTLPSpanExporter(
            endpoint=f"{SETTINGS.otlp_endpoint.rstrip('/')}/api/v1/otel/trace"
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = provider.get_tracer(service_name)
    _PROVIDERS[service_name] = tracer
    return tracer


@contextmanager
def workflow_span(service_name: str, name: str, attrs: dict, context=None):
    tracer = get_tracer(service_name)
    with tracer.start_as_current_span(name, context=context) as span:
        for k, v in attrs.items():
            if v is not None:
                span.set_attribute(k, v)
        yield span


def inject_current_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(carrier: dict | None):
    return propagate.extract(carrier or {})
