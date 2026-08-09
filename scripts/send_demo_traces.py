"""Send demo OTLP traces to Phoenix instances to demonstrate project isolation.

Usage:
  python scripts/send_demo_traces.py A   # send 3 spans to instance A (6006)
  python scripts/send_demo_traces.py B   # send 2 spans to instance B (6007)
"""
import sys
import time

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

INSTANCES = {
    "A": {
        "port": 6006,
        "key_file": ".local-keys/instance_a.key",
        "project": "project-demo-a",
        "messages": [
            "project A: hello from team A #1",
            "project A: hello from team A #2",
            "project A: hello from team A #3",
        ],
    },
    "B": {
        "port": 6007,
        "key_file": ".local-keys/instance_b.key",
        "project": "project-demo-b",
        "messages": [
            "project B: hello from team B #1",
            "project B: hello from team B #2",
        ],
    },
}


def main() -> None:
    which = sys.argv[1].upper() if len(sys.argv) > 1 else "A"
    cfg = INSTANCES[which]
    api_key = open(cfg["key_file"], encoding="utf-8").read().strip()

    resource = Resource.create(
        {
            "service.name": f"demo-{which.lower()}",
            "openinference.project.name": cfg["project"],
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"http://localhost:{cfg['port']}/v1/traces",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer(f"demo-{which.lower()}")
    for msg in cfg["messages"]:
        with tracer.start_as_current_span("llm-request") as span:
            span.set_attribute("message", msg)
            time.sleep(0.1)
    # graceful shutdown: flushes pending batches
    provider.shutdown()
    print(f"sent {len(cfg['messages'])} spans to instance {which} (project={cfg['project']})")


if __name__ == "__main__":
    main()