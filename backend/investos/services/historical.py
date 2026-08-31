from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investos.models.catalog import HistoricalEpisode


def _dt(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


# Curated analog library. The point of dominant_channel is to carry forward the
# lesson: what *actually* ended up driving the outcome, so a present-day narrative
# can be checked against how its closest historical rhyme resolved.
DEFAULT_EPISODES: list[dict] = [
    {
        "name": "Dot-com bust (1999-2001)",
        "episode_type": "regime_shift",
        "start_time": _dt(1999, 1, 1),
        "end_time": _dt(2001, 10, 1),
        "affected_sectors": ["technology", "telecom", "internet", "semiconductors"],
        "affected_themes": [
            "internet buildout",
            "new economy",
            "growth at any price",
            "capex supercycle",
        ],
        "dominant_channel": "Unprofitable growth and overcapacity repriced once cheap capital reversed; infrastructure demand was real but arrived years after the equity peak.",
        "notes": "Closest rhyme for AI-capex enthusiasm: the buildout thesis can be correct while the equity still de-rates on overcapacity and timing. Telecom/fiber overbuild is the cautionary channel.",
    },
    {
        "name": "Global Financial Crisis (2007-2009)",
        "episode_type": "macro_shock",
        "start_time": _dt(2007, 7, 1),
        "end_time": _dt(2009, 3, 1),
        "affected_sectors": ["financials", "housing", "consumer", "industrials"],
        "affected_themes": [
            "leverage",
            "credit expansion",
            "securitization",
            "housing",
        ],
        "dominant_channel": "Credit contraction and forced deleveraging dominated everything; correlations went to one and fundamentals mattered less than funding access.",
        "notes": "When the channel is funding/credit, diversification within risk assets fails. Watch leverage and refinancing walls over the equity story.",
    },
    {
        "name": "SPAC & meme mania (2020-2021)",
        "episode_type": "regime_shift",
        "start_time": _dt(2020, 6, 1),
        "end_time": _dt(2021, 11, 1),
        "affected_sectors": ["spac", "ev", "crypto", "technology", "consumer"],
        "affected_themes": [
            "retail speculation",
            "zero rates",
            "liquidity",
            "narrative investing",
        ],
        "dominant_channel": "Liquidity-driven multiple expansion detached from fundamentals; unwound sharply when the rate-hike cycle began.",
        "notes": "When the channel is liquidity, the signal to watch is policy/rates, not the story. Multiples, not earnings, did the moving in both directions.",
    },
    {
        "name": "2022 rate-shock repricing",
        "episode_type": "macro_shock",
        "start_time": _dt(2022, 1, 1),
        "end_time": _dt(2022, 12, 1),
        "affected_sectors": ["technology", "growth", "real estate", "crypto"],
        "affected_themes": [
            "duration",
            "unprofitable tech",
            "discount rate",
            "inflation",
        ],
        "dominant_channel": "Discount-rate repricing of long-duration assets; the longer the cash flows, the harder the de-rating, independent of company quality.",
        "notes": "Duration is the channel. Long-dated growth names fall on rates regardless of execution. Confounder check: is a move company-specific or just the rates beta?",
    },
    {
        "name": "2014-2016 oil price collapse",
        "episode_type": "sector_rotation",
        "start_time": _dt(2014, 6, 1),
        "end_time": _dt(2016, 2, 1),
        "affected_sectors": ["energy", "materials", "industrials"],
        "affected_themes": ["commodity supercycle", "shale", "oversupply"],
        "dominant_channel": "Supply growth (US shale) rather than demand collapse drove the price down; the surprise was on the supply side, which most demand-focused analysis missed.",
        "notes": "For commodity-linked names, model the supply channel explicitly. A demand-only thesis missed the dominant driver.",
    },
]

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "is",
    "are",
    "with",
    "as",
    "by",
    "at",
    "from",
    "this",
    "that",
    "it",
    "be",
    "will",
    "was",
    "about",
    "into",
    "their",
    "its",
    "what",
    "how",
    "why",
    "do",
    "does",
    "vs",
}


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    raw = "".join(c.lower() if c.isalnum() else " " for c in text).split()
    return {w for w in raw if len(w) > 2 and w not in _STOPWORDS}


class HistoricalEpisodeService:
    """Matches present-day subjects/queries to analogous historical episodes.

    Wires the previously-dormant ``HistoricalEpisode`` table into the reasoning
    path so the system can say "this rhymes with X, where the dominant channel
    turned out to be Y."
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_episodes(self) -> list[HistoricalEpisode]:
        return list(
            (await self.session.execute(select(HistoricalEpisode))).scalars().all()
        )

    async def seed_default_episodes(self) -> int:
        """Insert the curated episodes that don't already exist (by name)."""
        existing = set(
            (await self.session.execute(select(HistoricalEpisode.name))).scalars().all()
        )
        created = 0
        for spec in DEFAULT_EPISODES:
            if spec["name"] in existing:
                continue
            self.session.add(HistoricalEpisode(**spec))
            created += 1
        if created:
            await self.session.commit()
        return created

    async def find_analogies(self, text: str, limit: int = 3) -> list[dict]:
        """Rank stored episodes by keyword overlap with the given text.

        Returns lightweight dicts suitable for reasoning context or UI display.
        """
        query_tokens = _tokens(text)
        if not query_tokens:
            return []

        scored: list[tuple[int, HistoricalEpisode]] = []
        for ep in await self.list_episodes():
            hay = _tokens(
                " ".join(
                    [
                        ep.name or "",
                        ep.description or "",
                        " ".join(ep.affected_sectors or []),
                        " ".join(ep.affected_themes or []),
                        ep.dominant_channel or "",
                        ep.notes or "",
                    ]
                )
            )
            overlap = len(query_tokens & hay)
            # One generic token (for example "technology" or "growth") is not
            # enough to justify injecting a historical frame into the answer.
            # Require at least two independent lexical anchors; the question
            # research planner can still explicitly opt into the resulting lens.
            if overlap >= 2:
                scored.append((overlap, ep))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [self._format(ep, score) for score, ep in scored[:limit]]

    @staticmethod
    def _format(ep: HistoricalEpisode, score: int) -> dict:
        start = ep.start_time.year if ep.start_time else None
        end = ep.end_time.year if ep.end_time else None
        period = f"{start}-{end}" if start and end else (str(start) if start else "")
        return {
            "id": str(ep.id),
            "name": ep.name,
            "period": period,
            "episode_type": ep.episode_type,
            "dominant_channel": ep.dominant_channel,
            "affected_sectors": ep.affected_sectors or [],
            "affected_themes": ep.affected_themes or [],
            "lesson": ep.notes,
            "match_score": score,
        }

    @staticmethod
    def as_context_text(analogies: list[dict]) -> str:
        """Compact block to inject into reasoning/chat output."""
        if not analogies:
            return ""
        lines = ["Historical analogies (rhymes, not predictions):"]
        for a in analogies:
            lines.append(
                f"- {a['name']} ({a['period']}): dominant channel was "
                f"{a['dominant_channel']}"
            )
        return "\n".join(lines)

    @staticmethod
    def application_lenses(
        analogies: list[dict],
        *,
        query_text: str | None = None,
        subject_name: str | None = None,
        portfolio_context: dict[str, Any] | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Convert matched episodes into present-day investment checks.

        Matching the episode is only the first step. The useful investor move is
        applying the old dominant channel to the current setup, then explicitly
        naming where that analogy could fail.
        """
        if not analogies:
            return []

        portfolio_context = portfolio_context or {}
        subject_text = " ".join(item for item in [query_text, subject_name] if item)
        subject_tokens = _tokens(subject_text)
        lenses: list[dict] = []
        for analogy in analogies[:limit]:
            sectors = [
                str(item)
                for item in (analogy.get("affected_sectors") or [])
                if str(item).strip()
            ][:6]
            themes = [
                str(item)
                for item in (analogy.get("affected_themes") or [])
                if str(item).strip()
            ][:6]
            channel = str(analogy.get("dominant_channel") or "").strip()
            lesson = str(analogy.get("lesson") or "").strip()
            matched_terms = HistoricalEpisodeService._matched_context_terms(
                sectors + themes + [channel, lesson],
                subject_tokens,
            )
            exposed_holdings = HistoricalEpisodeService._portfolio_matches_for_analogy(
                portfolio_context,
                sectors=sectors,
                themes=themes,
            )

            label = str(analogy.get("name") or "historical episode").strip()
            period = str(analogy.get("period") or "").strip()
            target = subject_name or "the current question"
            rhyme_basis = ", ".join(matched_terms[:5]) or ", ".join(
                (themes or sectors)[:3]
            )
            what_rhymes = (
                f"{target} overlaps this episode through {rhyme_basis}; the useful comparison is the causal channel, "
                "not the surface narrative."
                if rhyme_basis
                else f"{target} matched this episode, but the specific overlap should be verified before using it."
            )
            portfolio_phrase = HistoricalEpisodeService._portfolio_phrase(
                exposed_holdings,
                sectors=sectors,
                themes=themes,
            )

            lenses.append(
                {
                    "name": label,
                    "period": period,
                    "lens_use_policy": (
                        "Seed, not checklist: use this episode to generate falsifiable causal checks, "
                        "then add, ignore, or revise checks based on current evidence and portfolio exposure."
                    ),
                    "current_application_prompt": (
                        f"For {target}, decide whether the old episode's actual driver is active now, "
                        "what current evidence would prove or disprove that, and which holding exposure is investable."
                    ),
                    "what_rhymes": what_rhymes,
                    "dominant_channel_test": (
                        f"Test whether today's outcome is being driven by the same dominant channel: {channel}"
                        if channel
                        else "Test the dominant causal channel before treating this as a useful analogy."
                    ),
                    "where_analogy_breaks": (
                        "Break the analogy if today's profitability, balance sheets, supply discipline, demand timing, "
                        "capital cost, or market structure differs enough that the old channel no longer dominates."
                    ),
                    "portfolio_transmission": portfolio_phrase,
                    "best_next_check": HistoricalEpisodeService._best_next_check(
                        channel=channel,
                        lesson=lesson,
                        sectors=sectors,
                        themes=themes,
                    ),
                    "investor_questions": HistoricalEpisodeService._investor_questions(
                        target=target,
                        channel=channel,
                        lesson=lesson,
                        portfolio_phrase=portfolio_phrase,
                    ),
                }
            )
        return lenses

    @staticmethod
    def _matched_context_terms(
        candidates: list[str], subject_tokens: set[str]
    ) -> list[str]:
        matches: list[str] = []
        if not subject_tokens:
            return matches
        for candidate in candidates:
            text = " ".join(str(candidate).split())
            if not text:
                continue
            if _tokens(text) & subject_tokens:
                matches.append(text)
        return matches

    @staticmethod
    def _portfolio_matches_for_analogy(
        portfolio_context: dict[str, Any],
        *,
        sectors: list[str],
        themes: list[str],
    ) -> list[str]:
        hay_tokens = _tokens(" ".join(sectors + themes))
        if not hay_tokens:
            return []
        holdings = (
            portfolio_context.get("top_holdings")
            or portfolio_context.get("tracked_positions")
            or []
        )
        matches: list[str] = []
        for holding in holdings[:20]:
            if not isinstance(holding, dict):
                continue
            label = HistoricalEpisodeService._holding_label(holding)
            holding_text = " ".join(
                str(holding.get(key) or "")
                for key in (
                    "ticker",
                    "symbol",
                    "name",
                    "company_name",
                    "entity_name",
                    "security_name",
                    "sector",
                    "industry",
                    "theme",
                    "thesis",
                    "summary",
                )
            )
            if _tokens(holding_text) & hay_tokens and label:
                matches.append(label)
        return matches[:6]

    @staticmethod
    def _holding_label(holding: dict[str, Any]) -> str:
        ticker = str(holding.get("ticker") or holding.get("symbol") or "").strip()
        name = str(
            holding.get("name")
            or holding.get("company_name")
            or holding.get("entity_name")
            or holding.get("security_name")
            or ""
        ).strip()
        if ticker and name and ticker.lower() not in name.lower():
            return f"{ticker} · {name}"
        return ticker or name

    @staticmethod
    def _portfolio_phrase(
        exposed_holdings: list[str],
        *,
        sectors: list[str],
        themes: list[str],
    ) -> str:
        exposure_basis = ", ".join((sectors + themes)[:5])
        if exposed_holdings:
            return (
                f"Map the channel first to {', '.join(exposed_holdings)}; those holdings overlap the episode's "
                f"affected areas ({exposure_basis})."
            )
        if exposure_basis:
            return (
                f"Check portfolio holdings exposed to {exposure_basis}; do not treat the analogy as relevant until a "
                "specific holding and transmission route are named."
            )
        return "Do not treat the analogy as portfolio-relevant until a specific holding and transmission route are named."

    @staticmethod
    def _best_next_check(
        *,
        channel: str,
        lesson: str,
        sectors: list[str],
        themes: list[str],
    ) -> str:
        driver = " ".join(item for item in [channel, lesson] if item).strip()
        basis = ", ".join((sectors + themes)[:3])
        if driver and basis:
            return (
                f"Find current, source-backed evidence that confirms or rejects this historical driver for {basis}: "
                f"{driver}"
            )
        if driver:
            return f"Find current, source-backed evidence that confirms or rejects this historical driver: {driver}"
        if basis:
            return f"Find the current measurable driver for {basis} before carrying the analogy into a thesis."
        return "Find the current measurable driver before carrying the analogy into a thesis."

    @staticmethod
    def _investor_questions(
        *,
        target: str,
        channel: str,
        lesson: str,
        portfolio_phrase: str,
    ) -> list[str]:
        driver = " ".join(item for item in [channel, lesson] if item).strip()
        questions = [
            f"What present-day evidence says the historical driver is active for {target}?",
            "What would make this analogy misleading enough to discard?",
            f"What is the investable transmission route? {portfolio_phrase}",
        ]
        if driver:
            questions.insert(
                1,
                f"Which part of the historical driver is actually measurable now: {driver}",
            )
        return questions[:4]
