"""TracerProvider bootstrap.

Idempotent by design: OTel's ``trace.set_tracer_provider`` is guarded by an
internal ``_TRACER_PROVIDER_SET_ONCE`` flag that ignores re-installation.
We honor that contract by returning the existing provider whenever one is
already in place. The first caller wins.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


def setup_tracer_provider() -> TracerProvider:
    """Return the process-global TracerProvider, creating it if missing.

    Calling this more than once returns the same instance; tests can rely on
    that without poking OTel's internal global state.
    """
    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        return current
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    return provider
