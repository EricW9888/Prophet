"""Wake hints only; evidence, questions and shadow receipts are the durable queue."""

import asyncio
import logging
from collections.abc import Callable

import asyncpg

from investos.config import settings

WORK_SIGNAL_CHANNEL = "prophet_work_ready"
logger = logging.getLogger(__name__)


async def listen_for_work_signals(on_signal: Callable[[str], None]) -> None:
    """Keep one idle LISTEN session outside the request transaction pool."""
    unavailable = False
    while True:
        connection = None
        try:
            disconnected = asyncio.Event()
            connection = await asyncpg.connect(
                host=settings.POSTGRES_SERVER,
                port=int(settings.POSTGRES_PORT),
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD,
                database=settings.POSTGRES_DB,
                timeout=5,
                server_settings={"application_name": "prophet_work_signals"},
            )
            connection.add_termination_listener(lambda _: disconnected.set())
            await connection.add_listener(
                WORK_SIGNAL_CHANNEL,
                lambda _connection, _pid, _channel, payload: on_signal(payload),
            )
            unavailable = False
            # Re-read durable queues only after LISTEN is active, so a commit
            # concurrent with startup or reconnect cannot fall between the two.
            on_signal("queue_catchup")
            await disconnected.wait()
        except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
            if not unavailable:
                logger.warning(
                    "Work signal listener unavailable; interval recovery remains active."
                )
                unavailable = True
        finally:
            if connection is not None and not connection.is_closed():
                try:
                    await connection.close(timeout=3)
                except (
                    OSError,
                    TimeoutError,
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                ):
                    connection.terminate()
        await asyncio.sleep(15)
