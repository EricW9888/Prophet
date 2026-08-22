from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from investos.models.catalog import HistoricalEpisode
from investos.models.coverage import UnresolvedQuestion
from investos.models.entity import Security
from investos.models.fundamental import FundamentalMetric
from investos.models.market_setup import MarketSetupSignal
from investos.services.artifact_hygiene import label_from_profile_texts
from investos.services.entity_hygiene import EntityHygieneService
from investos.services.integrity import IntegrityService
from investos.services.research import ResearchService
from investos.services.review import ReviewService
from investos.services.theme_hygiene import ThemeHygieneService
from investos.workers.extraction import (
    ExtractionWorker,
    is_topic_subject_name,
    is_unusable_subject,
    normalize_subject_name,
)


def test_normalize_search_query_strips_recursive_research_wrappers():
    assert ResearchService._normalize_search_query(
        "Research on Research on Unclassified Research: What additional evidence would materially strengthen the current view?"
    ).startswith("Unclassified Research")


def test_artifact_research_query_detection_blocks_recursive_internal_questions():
    assert ResearchService._is_artifact_research_query(
        "What additional evidence would materially strengthen the current view on Research on Unclassified Research?"
    )
    assert ResearchService._is_artifact_research_query(
        "Research on Research on Research on Unclassified Research: strongest counter-argument"
    )


def test_artifact_research_query_detection_allows_real_holding_questions():
    assert not ResearchService._is_artifact_research_query(
        "Can MEMA hit 26% CAGR over the long run?"
    )
    assert not ResearchService._is_artifact_subject_name("MEMA · Memory Alpha Corp.")


def test_internal_missing_classes_questions_are_artifacts():
    question = "What is the expected performance of the model on the missing classes?"
    assert IntegrityService.is_artifact_text(question)
    assert ResearchService._is_artifact_research_query(question)
    assert ReviewService._is_artifact_question(question)


def test_lineage_signature_metadata_is_versioned_idempotent_and_preserves_fields():
    text = " ".join(
        f"memory demand pricing supply margin cycle evidence token {index}"
        for index in range(30)
    )

    metadata = IntegrityService.lineage_signature_metadata(
        {"existing": "preserved"}, text
    )

    assert metadata is not None
    assert metadata["existing"] == "preserved"
    assert metadata["lineage_signature_version"] == 1
    assert metadata["lineage_signature_status"] == "ready"
    assert len(metadata["near_duplicate_signature"]) == 16
    assert IntegrityService.lineage_signature_metadata(metadata, text) is None


def test_lineage_signature_metadata_checkpoints_short_and_malformed_legacy_rows():
    metadata = IntegrityService.lineage_signature_metadata(
        {"lineage_signature_version": "bad"}, "short evidence"
    )
    assert metadata is not None
    assert metadata["lineage_signature_status"] == "insufficient_text"
    assert metadata["near_duplicate_signature"] is None
    assert metadata["signature_token_count"] == 2
    assert (
        IntegrityService.lineage_signature_metadata(
            {"lineage_signature_version": 1}, "short evidence"
        )
        is not None
    )


def test_integrity_registry_includes_investment_graph_nodes():
    assert IntegrityService._model_for("security") is Security
    assert IntegrityService._model_for("fundamental_metric") is FundamentalMetric
    assert IntegrityService._model_for("market_setup_signal") is MarketSetupSignal
    assert IntegrityService._model_for("historical_episode") is HistoricalEpisode
    assert IntegrityService._model_for("unresolved_question") is UnresolvedQuestion


def test_integrity_registry_does_not_guess_unknown_node_models():
    assert IntegrityService._model_for("future_investment_object") is None


async def test_integrity_preserves_edges_with_unknown_node_types():
    edge_id = uuid4()
    edge_result = MagicMock()
    edge_result.all.return_value = [
        (
            edge_id,
            "future_investment_object",
            uuid4(),
            "portfolio",
            uuid4(),
        )
    ]
    session = MagicMock()
    session.execute = AsyncMock(return_value=edge_result)

    assert await IntegrityService(session)._orphan_edge_ids() == []


async def test_integrity_removes_only_proven_missing_registered_nodes():
    edge_id = uuid4()
    edge_result = MagicMock()
    edge_result.all.return_value = [(edge_id, "fact", uuid4(), "portfolio", uuid4())]
    existing_result = MagicMock()
    existing_result.scalars.return_value.all.return_value = []
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[edge_result, existing_result])

    assert await IntegrityService(session)._orphan_edge_ids() == [edge_id]


def test_comma_ticker_basket_is_not_a_subject_entity():
    assert is_unusable_subject("MEMA, MEMB, OPTC, INFR, AUTO")
    assert is_unusable_subject("Portfolio holdings (MEMA, AUTO, MEMB, OPTC, INFR)")
    assert ResearchService._is_artifact_subject_name("MEMA, MEMB, OPTC, INFR, AUTO")
    assert ResearchService._is_artifact_subject_name(
        "Portfolio holdings (MEMA, AUTO, MEMB, OPTC, INFR)"
    )
    assert not is_unusable_subject("Memory Alpha Corp.")


def test_leaked_action_labels_are_artifact_class_entities():
    assert EntityHygieneService._classify("Oops") == "artifact"
    assert EntityHygieneService._classify("Skip") == "artifact"
    assert EntityHygieneService._classify("Both") == "artifact"
    assert EntityHygieneService._classify("u") == "artifact"
    assert EntityHygieneService._classify("$20,000 position size") == "artifact"
    assert EntityHygieneService._classify("35% position") == "artifact"
    assert EntityHygieneService._classify("Which market approves first?") == "artifact"
    assert EntityHygieneService._classify("30 Jan 2026 CR expiration") == "artifact"
    assert EntityHygieneService._classify("there / anything / else") == "artifact"
    assert EntityHygieneService._classify("eHouse Studio") == "unusable"
    assert (
        EntityHygieneService._classify("mTab Halo Insight Management System")
        == "unusable"
    )
    assert EntityHygieneService._classify("Memory Alpha Corp.") is None


def test_auto_sector_subjects_are_not_internal_artifacts():
    assert not ResearchService._is_artifact_subject_name(
        "Auto insurance premium drivers"
    )
    assert not ResearchService._is_artifact_subject_name(
        "Autonomous-vehicle regulatory outlook"
    )
    assert (
        normalize_subject_name("Auto insurance premium drivers")
        == "Auto insurance premium drivers"
    )


def test_topic_like_subjects_go_to_theme_layer_not_entity_layer():
    assert is_topic_subject_name("robotaxi")
    assert is_topic_subject_name("data center industry")
    assert is_topic_subject_name("vendor lock-in")
    assert is_topic_subject_name("high bandwidth flash storage")
    assert not is_topic_subject_name("eHouse Studio")
    assert not is_topic_subject_name("mTab Halo Insight Management System")
    assert not is_topic_subject_name("$20,000 position size")
    assert not is_topic_subject_name(
        "Which markets have regulatory approval timelines?"
    )

    worker = ExtractionWorker.__new__(ExtractionWorker)
    assert worker._subject_type("robotaxi", "research note") == "theme"
    assert worker._subject_type("data center industry", "research note") == "theme"
    assert worker._subject_type("eHouse Studio", "research note") == "entity"


def test_topic_entities_are_reclassification_candidates_not_deletion_only():
    assert EntityHygieneService._classify("robotaxi") == "unusable"
    assert EntityHygieneService._is_migratable_topic_label("robotaxi")
    assert EntityHygieneService._is_migratable_topic_label("data center industry")
    assert not EntityHygieneService._is_migratable_topic_label("u")
    assert not EntityHygieneService._is_migratable_topic_label("$20,000 position size")
    assert not EntityHygieneService._is_migratable_topic_label(
        "Which specific markets have approval timelines?"
    )


def test_entity_hygiene_reclassifies_substantive_broad_labels_not_products():
    substantive_profile = [
        SimpleNamespace(
            executive_summary="Universities are entering a structurally tighter financial regime.",
            business_model=None,
            bull_case=None,
            bear_case=None,
            key_drivers=None,
            competitor_landscape=None,
            strategist_reasoning=None,
            source_rationale=None,
        )
    ]
    placeholder_profile = [
        SimpleNamespace(
            executive_summary=(
                "Prophet does not have stored research on this topic yet. Portfolio has 14 "
                "positions including AUTO, MEMA, INFR, MEMB, MEDH. A research pass would help "
                "build a proper evidence base."
            ),
            business_model=None,
            bull_case=None,
            bear_case=None,
            key_drivers=None,
            competitor_landscape=None,
            strategist_reasoning=None,
            source_rationale=None,
        )
    ]

    assert EntityHygieneService._should_reclassify_unusable_entity(
        "universities", substantive_profile
    )
    assert EntityHygieneService._should_reclassify_unusable_entity(
        "industry", substantive_profile
    )
    assert not EntityHygieneService._should_reclassify_unusable_entity(
        "eHouse Studio", substantive_profile
    )
    assert not EntityHygieneService._should_reclassify_unusable_entity(
        "university", placeholder_profile
    )
    assert EntityHygieneService._placeholder_only_entity(placeholder_profile)
    assert (
        EntityHygieneService._theme_name_from_profiles(
            "universities", substantive_profile
        )
        == "Universities are entering a structurally tighter financial regime"
    )


def test_profile_label_skips_meta_evidence_packet_sentences():
    assert (
        label_from_profile_texts(
            [
                "The evidence packet is thin and generic. For most integrators, CAC is driven by long sales cycles.",
            ]
        )
        == "For most integrators, CAC is driven by long sales cycles"
    )
    assert (
        label_from_profile_texts(
            [
                "The stored evidence packet is empty. However, the portfolio is heavily concentrated in policy-sensitive names.",
            ]
        )
        == "The portfolio is heavily concentrated in policy-sensitive names"
    )


def test_duplicate_entity_matcher_merges_sndk_aliases_to_canonical_security():
    canonical_entries = [
        {"ticker": "MEMA", "name": "Memory Alpha Corp.", "is_active_holding": True},
        {"ticker": "MEMB", "name": "Memory Beta Inc.", "is_active_holding": True},
    ]

    target = EntityHygieneService._duplicate_entity_target_name(
        "MEMA · Memory Alpha Corp",
        canonical_entries,
    )

    assert target is not None
    assert target["ticker"] == "MEMA"
    assert target["match_reason"] == "explicit ticker MEMA"


def test_duplicate_entity_matcher_keeps_multi_ticker_baskets_out_of_entity_merge():
    canonical_entries = [
        {"ticker": "MEMA", "name": "Memory Alpha Corp.", "is_active_holding": True},
        {"ticker": "AUTO", "name": "Auto Dynamics", "is_active_holding": True},
        {"ticker": "MEMB", "name": "Memory Beta Inc.", "is_active_holding": True},
    ]

    assert (
        EntityHygieneService._duplicate_entity_target_name(
            "Portfolio holdings (MEMA, AUTO, MEMB)",
            canonical_entries,
        )
        is None
    )


def test_duplicate_entity_matcher_does_not_treat_product_name_as_underlying_ticker():
    canonical_entries = [
        {"ticker": "MEMA", "name": "Memory Alpha Corp.", "is_active_holding": True},
        {
            "ticker": "MEMX",
            "name": "Tradr 2X Long Memory Alpha Daily ETF",
            "is_active_holding": False,
        },
    ]

    target = EntityHygieneService._duplicate_entity_target_name(
        "Tradr 2X Long Memory Alpha Daily ETF",
        canonical_entries,
    )

    assert target is not None
    assert target["ticker"] == "MEMX"
    assert target["match_reason"] == "normalized name match"


async def test_entity_merge_retargets_first_class_investment_objects():
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(rowcount=2),
        SimpleNamespace(rowcount=1),
        SimpleNamespace(rowcount=3),
        SimpleNamespace(rowcount=0),
    ]
    service = EntityHygieneService(session)

    moved = await service._retarget_investment_objects_to_entity(uuid4(), uuid4())

    assert moved == {"fundamental_metrics": 3, "market_setup_signals": 3}
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert (
        sum("UPDATE fundamental_metrics" in statement for statement in statements) == 2
    )
    assert (
        sum("UPDATE market_setup_signals" in statement for statement in statements) == 2
    )


async def test_entity_reclassification_moves_first_class_objects_to_theme_layer():
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(rowcount=2),
        SimpleNamespace(rowcount=0),
        SimpleNamespace(rowcount=1),
        SimpleNamespace(rowcount=0),
    ]
    service = EntityHygieneService(session)

    moved = await service._retarget_investment_objects_to_theme(uuid4(), uuid4())

    assert moved == {"fundamental_metrics": 2, "market_setup_signals": 1}
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any("subject_type=:subject_type" in statement for statement in statements)
    assert (
        sum("UPDATE fundamental_metrics" in statement for statement in statements) == 2
    )
    assert (
        sum("UPDATE market_setup_signals" in statement for statement in statements) == 2
    )


def test_clean_research_title_preserves_real_subject_from_metadata():
    assert (
        ResearchService._clean_research_title(
            title="Auto research: MEMA · Memory Alpha Corp.: long-run CAGR test",
            query="Can MEMA hit 26% CAGR over the long run?",
            metadata_json={"subject_name": "MEMA · Memory Alpha Corp."},
        )
        == "Research on MEMA · Memory Alpha Corp.: Can MEMA hit 26% CAGR over the long run?"
    )


def test_theme_hygiene_treats_generic_profile_placeholder_as_non_substantive():
    profile = SimpleNamespace(
        executive_summary=(
            "Prophet does not have stored research on this topic yet. Portfolio has 14 "
            "positions including AUTO, MEMA, INFR, MEMB, MEDH. A research pass would help "
            "build a proper evidence base."
        ),
        business_model=None,
        bull_case=None,
        bear_case=None,
        key_drivers=None,
        competitor_landscape=None,
        strategist_reasoning=None,
        source_rationale=None,
        active_contradictions=[],
    )

    assert ThemeHygieneService._is_artifact_theme_name(
        "Research on MEMA · Memory Alpha Corp."
    )
    assert not ThemeHygieneService._profile_has_substantive_text(profile)

    profile.executive_summary = (
        "HBM demand changes NAND supply allocation and matters directly to MEMB/MEMA."
    )
    assert ThemeHygieneService._profile_has_substantive_text(profile)


def test_theme_hygiene_treats_placeholder_variants_as_non_substantive():
    profile = SimpleNamespace(
        executive_summary=(
            "InvestOS does not have stored research on this topic yet. Portfolio has 14 "
            "positions including AUTO, MEMA, INFR, MEMB, MEDH. A research pass would help "
            "build a proper evidence base."
        ),
        business_model=None,
        bull_case=None,
        bear_case=None,
        key_drivers=None,
        competitor_landscape=None,
        strategist_reasoning=None,
        source_rationale=None,
        active_contradictions=[],
    )

    assert not ThemeHygieneService._profile_has_substantive_text(profile)

    profile.executive_summary = (
        "Prophet has some relevant stored context for this subject, but not enough "
        "targeted evidence on this specific angle to make a high-conviction "
        "opportunity call yet. Portfolio has 14 positions including AUTO, MEMA, "
        "INFR, MEMB, MEDH. The right next step is to investigate likely beneficiaries, "
        "losers, and possible reallocations rather than stop at current holdings."
    )

    assert not ThemeHygieneService._profile_has_substantive_text(profile)


def test_theme_hygiene_cleans_artifact_names_without_domain_hardcoding():
    assert (
        ThemeHygieneService._clean_artifact_theme_name(
            "Research on: What is the daily dollar liquidity of the position relative to portfolio size?"
        )
        == "What is the daily dollar liquidity of the position relative to portfolio size?"
    )
    assert (
        ThemeHygieneService._clean_artifact_theme_name(
            "Autonomous reflection: AUTO: Auto Dynamics remains a 33 % portfolio weight"
        )
        == "AUTO - Auto Dynamics remains a 33 % portfolio weight"
    )
    assert (
        ThemeHygieneService._clean_artifact_theme_name(
            "Research on: What additional evidence would materially strengthen the current view on Research on: margins"
        )
        is None
    )


def test_theme_hygiene_uses_profile_label_for_truncated_artifact_names():
    profile = SimpleNamespace(
        executive_summary=(
            "Auto Dynamics requires a hybrid valuation framework that weights auto manufacturing "
            "fundamentals against probability-weighted AI optionality."
        ),
        business_model=None,
        bull_case=None,
        bear_case=None,
        key_drivers=None,
        competitor_landscape=None,
        strategist_reasoning=None,
        source_rationale=None,
        active_contradictions=[],
    )

    assert ThemeHygieneService._clean_artifact_theme_name(
        "Research on: What valuation framework should govern the decision here: auto manufacturer, AI/robotics option value, or a",
        [profile],
    ) == (
        "Auto Dynamics requires a hybrid valuation framework that weights auto manufacturing "
        "fundamentals against probability-weighted AI optionality"
    )
    assert ThemeHygieneService._clean_artifact_theme_name(
        "Autonomous reflection: AUTO: Auto Dynamics remains a 33 % portfolio weight (~$1",
        [profile],
    ) == (
        "AUTO - Auto Dynamics requires a hybrid valuation framework that weights auto "
        "manufacturing fundamentals against probability-weighted AI optionality"
    )


def test_theme_hygiene_preserves_substantive_conclusions():
    empty_no_view = SimpleNamespace(
        current_thesis_summary="",
        current_stance="no_view",
        confidence_band="very_low",
        key_supporting_evidence_ids=[],
        key_contradicting_evidence_ids=[],
        what_would_falsify=[],
        what_would_strengthen=[],
    )
    actual_view = SimpleNamespace(
        current_thesis_summary="HBM demand constrains DRAM supply and can support NAND pricing.",
        current_stance="bullish",
        confidence_band="low",
        key_supporting_evidence_ids=[],
        key_contradicting_evidence_ids=[],
        what_would_falsify=[],
        what_would_strengthen=[],
    )

    assert not ThemeHygieneService._conclusion_has_substantive_state(empty_no_view)
    assert ThemeHygieneService._conclusion_has_substantive_state(actual_view)
