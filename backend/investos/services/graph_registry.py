from __future__ import annotations


def durable_graph_model_map() -> dict[str, type]:
    """Return the canonical registry for graph nodes backed by durable rows."""
    from investos.models.catalog import HistoricalEpisode
    from investos.models.conclusion import ConclusionState
    from investos.models.coverage import CoverageMap, UnresolvedQuestion
    from investos.models.entity import Entity, Security
    from investos.models.evidence import RawEvidence, SourceItem
    from investos.models.fundamental import FundamentalMetric
    from investos.models.knowledge import Claim, Event, Fact
    from investos.models.lesson import Lesson
    from investos.models.market_setup import MarketSetupSignal
    from investos.models.portfolio import Position
    from investos.models.review import ReviewQueueItem
    from investos.models.shadow import ExperimentResult, ShadowExperiment
    from investos.models.source import Source
    from investos.models.theme import Theme

    return {
        "claim": Claim,
        "conclusion": ConclusionState,
        "coverage_map": CoverageMap,
        "entity": Entity,
        "event": Event,
        "experiment_result": ExperimentResult,
        "fact": Fact,
        "fundamental_metric": FundamentalMetric,
        "historical_episode": HistoricalEpisode,
        "lesson": Lesson,
        "market_setup_signal": MarketSetupSignal,
        "position": Position,
        "raw_evidence": RawEvidence,
        "review_item": ReviewQueueItem,
        "security": Security,
        "shadow_experiment": ShadowExperiment,
        "source": Source,
        "source_item": SourceItem,
        "theme": Theme,
        "unresolved_question": UnresolvedQuestion,
    }
