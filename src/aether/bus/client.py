"""aiomqtt-backed bus client: publish records and subscribe the source tree.

The only module that imports aiomqtt. It turns the pure routing in
:mod:`aether.bus.topics` into real publishes, and runs a reconnecting subscriber
that parses each payload and hands the validated record to a sink (the hub).

Failure isolation (PRD §37): one malformed payload is dropped and logged, never
crashing the loop; a broker drop triggers a bounded reconnect rather than killing
the backend.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

import aiomqtt
from pydantic import ValidationError

from aether.bus.topics import SUBSCRIBE_FILTERS, record_qos, record_retain, record_topic
from aether.config import Settings
from aether.schema.records import Record
from aether.schema.validation import RecordTooLargeError, dump_record_json, parse_record

log = logging.getLogger(__name__)

#: Sink for inbound records — the hub's synchronous ``publish``.
RecordHandler = Callable[[Record], None]

#: Backoff between reconnect attempts when the broker is unreachable.
DEFAULT_RECONNECT_S = 5.0


class Bus:
    """Thin publisher over a connected aiomqtt client."""

    def __init__(self, client: aiomqtt.Client) -> None:
        self._client = client

    async def publish_record(self, record: Record) -> None:
        """Publish one record on its topic with the policy from :mod:`topics`."""
        await self._client.publish(
            record_topic(record),
            payload=dump_record_json(record),
            qos=record_qos(record),
            retain=record_retain(record),
        )


def apply_payload(payload: object, handle: RecordHandler) -> bool:
    """Parse one wire payload and feed the record to ``handle``; ``False`` if dropped.

    Isolates a single malformed/oversized payload (PRD §37) so the subscriber loop
    never dies on bad input. Non-string/bytes payloads (we only publish JSON) are
    ignored too.
    """
    if not isinstance(payload, (bytes, str)):
        return False
    try:
        record = parse_record(payload)
    except (ValidationError, RecordTooLargeError, ValueError):
        log.warning("dropping malformed record payload")
        return False
    handle(record)
    return True


@contextlib.asynccontextmanager
async def connect(settings: Settings, *, identifier: str | None = None) -> AsyncIterator[Bus]:
    """Open a bus connection for publishing (e.g. a source adapter or the demo)."""
    async with aiomqtt.Client(
        hostname=settings.mqtt_host, port=settings.mqtt_port, identifier=identifier
    ) as client:
        yield Bus(client)


async def run_record_subscriber(
    settings: Settings,
    handle: RecordHandler,
    *,
    reconnect_s: float = DEFAULT_RECONNECT_S,
    ready: asyncio.Event | None = None,
    identifier: str | None = None,
) -> None:
    """Subscribe the source tree and feed validated records to ``handle`` forever.

    Reconnects with a fixed backoff on broker errors. ``ready`` (if given) is set
    once the subscription is live so callers can publish without racing the
    subscribe. Cancellation propagates out cleanly for lifespan shutdown.
    """
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.mqtt_host, port=settings.mqtt_port, identifier=identifier
            ) as client:
                for topic_filter in SUBSCRIBE_FILTERS:
                    await client.subscribe(topic_filter)
                if ready is not None:
                    ready.set()
                async for message in client.messages:
                    apply_payload(message.payload, handle)
        except aiomqtt.MqttError as exc:
            log.warning("bus subscriber lost broker (%s); reconnecting in %.0fs", exc, reconnect_s)
            await asyncio.sleep(reconnect_s)
