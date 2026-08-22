from investos.core.prompting import compact_packet_context
from investos.services.historical import (
    DEFAULT_EPISODES,
    HistoricalEpisodeService,
    _tokens,
)


def test_default_episodes_have_required_fields():
    required = {"name", "episode_type", "start_time", "dominant_channel"}
    for spec in DEFAULT_EPISODES:
        assert required <= set(spec), f"{spec.get('name')} missing fields"
        assert spec["affected_sectors"], "episodes need sectors for matching"


def test_ai_capex_query_overlaps_dotcom_episode():
    dotcom = next(e for e in DEFAULT_EPISODES if "Dot-com" in e["name"])
    hay = _tokens(
        " ".join(
            [
                dotcom["name"],
                " ".join(dotcom["affected_themes"]),
                dotcom["dominant_channel"],
                dotcom["notes"],
            ]
        )
    )
    q = _tokens("Is the AI capex buildout an overcapacity bubble?")
    assert {"capex", "buildout", "overcapacity"} & hay
    assert q & hay  # the matcher would score this episode > 0


def test_context_text_is_empty_without_analogies():
    assert HistoricalEpisodeService.as_context_text([]) == ""


def test_context_text_lists_dominant_channel():
    text = HistoricalEpisodeService.as_context_text(
        [
            {
                "name": "Dot-com bust (1999-2001)",
                "period": "1999-2001",
                "dominant_channel": "overcapacity",
            }
        ]
    )
    assert "Dot-com bust" in text
    assert "overcapacity" in text


def test_compact_packet_context_carries_historical_analogies():
    packet = compact_packet_context(
        {
            "query_text": "Is AI capex like prior telecom overbuild?",
            "subject_type": "portfolio",
            "subject_id": "portfolio",
            "historical_analogies": [
                {
                    "name": "Dot-com bust (1999-2001)",
                    "period": "1999-2001",
                    "episode_type": "regime_shift",
                    "dominant_channel": "Infrastructure demand was real but arrived after the equity peak.",
                    "lesson": "The buildout thesis can be right while equity timing is wrong.",
                    "match_score": 3,
                }
            ],
        }
    )

    assert packet["historical_analogies"][0]["name"] == "Dot-com bust (1999-2001)"
    assert "equity peak" in packet["historical_analogies"][0]["dominant_channel"]


def test_application_lens_turns_dotcom_into_current_channel_check():
    lenses = HistoricalEpisodeService.application_lenses(
        [
            {
                "name": "Dot-com bust (1999-2001)",
                "period": "1999-2001",
                "affected_sectors": ["technology", "telecom", "semiconductors"],
                "affected_themes": ["internet buildout", "capex supercycle"],
                "dominant_channel": (
                    "Unprofitable growth and overcapacity repriced once cheap capital reversed; "
                    "infrastructure demand was real but arrived years after the equity peak."
                ),
                "lesson": "The buildout thesis can be correct while equity still de-rates on timing.",
            }
        ],
        query_text="Is current AI capex like prior telecom overcapacity?",
        subject_name="Portfolio",
        portfolio_context={
            "top_holdings": [
                {
                    "ticker": "MEMB",
                    "company_name": "Memory Beta Inc.",
                    "sector": "semiconductors",
                },
                {
                    "ticker": "OPTC",
                    "company_name": "Optical Systems",
                    "theme": "AI datacenter optical buildout",
                },
            ]
        },
    )

    lens = lenses[0]
    assert lens["name"] == "Dot-com bust (1999-2001)"
    assert lens["lens_use_policy"].startswith("Seed, not checklist")
    assert "old episode's actual driver" in lens["current_application_prompt"]
    assert "overcapacity" in lens["dominant_channel_test"]
    assert "Break the analogy" in lens["where_analogy_breaks"]
    assert "MEMB" in lens["portfolio_transmission"]
    assert "Unprofitable growth" in lens["best_next_check"]
    assert lens["investor_questions"]
    assert any("measurable now" in question for question in lens["investor_questions"])


def test_compact_packet_context_carries_historical_analogy_lenses():
    packet = compact_packet_context(
        {
            "query_text": "Does AI capex rhyme with dot-com?",
            "subject_type": "portfolio",
            "subject_id": "portfolio",
            "historical_analogy_lenses": [
                {
                    "name": "Dot-com bust (1999-2001)",
                    "period": "1999-2001",
                    "lens_use_policy": "Seed, not checklist: expand or discard based on evidence.",
                    "current_application_prompt": "Apply the old driver to the current portfolio only if evidence supports it.",
                    "what_rhymes": "Current AI capex overlaps through capex supercycle.",
                    "dominant_channel_test": "Test overcapacity versus realized demand.",
                    "where_analogy_breaks": "Break if profitability and supply discipline are materially different.",
                    "portfolio_transmission": "Map to semiconductor and optical holdings.",
                    "best_next_check": "Compare committed capacity to realized demand.",
                    "investor_questions": [
                        "Which holding has the cleanest transmission route?"
                    ],
                }
            ],
        }
    )

    lens = packet["historical_analogy_lenses"][0]
    assert lens["lens_use_policy"].startswith("Seed, not checklist")
    assert "old driver" in lens["current_application_prompt"]
    assert lens["what_rhymes"].startswith("Current AI capex")
    assert "overcapacity" in lens["dominant_channel_test"]
    assert "Compare committed capacity" in lens["best_next_check"]
    assert lens["investor_questions"][0].startswith("Which holding")
