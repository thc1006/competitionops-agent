"""Shared OTel decorators + span annotation helper.

Lifted out of ``competitionops.services.execution`` in Sprint 4 so the MCP
server (and any future service) can wrap public entry points with the
same root-span pattern without duplicating the ParamSpec boilerplate.

All three primitives are no-ops when the global TracerProvider is the
default ProxyTracerProvider (NonRecording span); production code can
freely decorate hot paths.
"""

from __future__ import annotations

import functools
from typing import Awaitable, Callable, ParamSpec, TypeVar

from opentelemetry import trace

_tracer = trace.get_tracer("competitionops")

_P = ParamSpec("_P")
_R = TypeVar("_R")


def traced_sync(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Wrap a sync function in an OTel root span named ``name``."""

    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with _tracer.start_as_current_span(name):
                return func(*args, **kwargs)

        return wrapper

    return decorator


def traced_async(
    name: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Wrap an async function in an OTel root span named ``name``."""

    def decorator(
        func: Callable[_P, Awaitable[_R]],
    ) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(func)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with _tracer.start_as_current_span(name):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def annotate_span(**attributes: object) -> None:
    """Set non-None attributes onto the currently active span.

    Keys with ``None`` values are skipped — useful for optional MCP tool
    args like ``source_uri`` that may legitimately be absent.
    """
    span = trace.get_current_span()
    for key, value in attributes.items():
        if value is None:
            continue
        # OTel accepts str/int/float/bool/sequence; coerce anything else.
        if isinstance(value, (str, int, float, bool)):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, str(value))
