from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from investos.db import async_session_maker
from investos.models.market_calendar import MarketSessionObservation
from investos.services.market_data import MarketDataService


@pytest.mark.asyncio(loop_scope="session")
async def test_completed_session_survives_rollover_restart_and_price_revision(
    monkeypatch,
):
    class DateTimeType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class Clock(datetime, metaclass=DateTimeType):
        instant = datetime(2025, 5, 21, 17, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.instant.timestamp(), tz=tz or UTC)

    opens = int(datetime(2025, 5, 21, 13, 30, tzinfo=UTC).timestamp())
    closes = int(datetime(2025, 5, 21, 17, tzinfo=UTC).timestamp())
    meta = {
        "symbol": "SYN-SESSION",
        "exchangeName": "SYN-EXCHANGE",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "currentTradingPeriod": {"regular": {"start": opens, "end": closes}},
    }
    chart = {
        "meta": meta,
        "timestamp": [opens - 86400, opens],
        "indicators": {
            "quote": [{"close": [100, 90]}],
            "adjclose": [{"adjclose": [100, 90]}],
        },
    }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, *_args, **_kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"chart": {"result": [chart]}},
            )

    monkeypatch.setattr("investos.services.market_data.datetime", Clock)
    monkeypatch.setattr(
        "investos.services.market_data.httpx.AsyncClient",
        lambda **_kwargs: Client(),
    )
    async with async_session_maker() as session:
        await session.execute(delete(MarketSessionObservation))
        await session.commit()
    try:
        async with async_session_maker() as session:
            first = await MarketDataService(session).fetch_signal_snapshot(
                "SYN-SESSION"
            )
            assert first["current_price"] == 90
            assert first["completed_at"] == "2025-05-21T17:00:00+00:00"
            await session.commit()

        Clock.instant += timedelta(hours=3)
        meta["currentTradingPeriod"]["regular"] = {
            "start": opens + 86400,
            "end": closes + 86400,
        }
        # A new service and database session must recover the observed schedule.
        async with async_session_maker() as session:
            service = MarketDataService(session)
            after = await service.fetch_signal_snapshot("SYN-SESSION")
            assert after["as_of"] == first["as_of"]
            assert after["completed_at"] == first["completed_at"]
            assert after["retrieved_at"] != first["retrieved_at"]
            assert after["current_price"] == 90
            assert (
                after["daily_observations"][-1]["completion_observed_at"]
                == first["retrieved_at"]
            )
            chart["indicators"]["quote"][0]["close"][-1] = 91
            revised = await service.fetch_signal_snapshot("SYN-SESSION")
            assert revised["current_price"] == 91

            chart["timestamp"] = [opens - 86400]
            missing = await service.fetch_signal_snapshot("SYN-SESSION")
            assert missing["status"] == "missing_verified_session"
            assert missing["expected_session_date"] == "2025-05-21"

            chart["timestamp"] = [opens - 86400, opens]
            meta["exchangeName"] = "OTHER-EXCHANGE"
            other = await service.fetch_signal_snapshot("SYN-SESSION")
            assert other["as_of"] != first["as_of"]
            assert (
                await session.scalar(
                    select(func.count()).select_from(MarketSessionObservation)
                )
                == 1
            )
    finally:
        async with async_session_maker() as session:
            await session.execute(delete(MarketSessionObservation))
            await session.commit()
