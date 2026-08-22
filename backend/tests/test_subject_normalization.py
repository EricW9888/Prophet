from investos.workers.extraction import is_unusable_subject, normalize_subject_name


def test_strips_auto_research_prefix_and_slug():
    assert (
        normalize_subject_name(
            "Auto research: MEMA · Memory Alpha Incorporated: hbm / shortage"
        )
        == "MEMA · Memory Alpha Incorporated"
    )


def test_strips_autonomous_reflection_to_ticker():
    assert (
        normalize_subject_name(
            "Autonomous reflection: AUTO: AUTO is a 32.5 % position at $392.51"
        )
        == "AUTO"
    )


def test_strips_research_on_prefix():
    assert (
        normalize_subject_name("Research on: Western Digital separation")
        == "Western Digital separation"
    )
    assert (
        normalize_subject_name(
            "Research on Research on Research on Unclassified Research: What additional evidence would materially strengthen the current view?"
        )
        == "Unclassified Research"
    )


def test_plain_company_name_unchanged():
    assert normalize_subject_name("Apple Inc.") == "Apple Inc"
    assert normalize_subject_name("AXT, Inc.") == "AXT, Inc"


def test_unusable_detects_junk():
    assert is_unusable_subject("$20,000 position size")
    assert is_unusable_subject("2014 U.S. college graduates")
    assert is_unusable_subject(
        "academic majors and credential types"
    )  # lowercase fragment
    assert is_unusable_subject(
        "At what organizational scale do fixed costs outweigh savings?"
    )  # question
    assert is_unusable_subject("Unclassified Research")
    assert is_unusable_subject("Research on Research on Unclassified Research")
    assert is_unusable_subject("Autonomous reflection cycle for MEMA")
    assert is_unusable_subject("Oops")
    assert is_unusable_subject("Skip")
    assert is_unusable_subject("")


def test_unusable_allows_real_names():
    assert not is_unusable_subject("Apple Inc.")
    assert not is_unusable_subject("Auto Dynamics")
    assert not is_unusable_subject("MEMA · Memory Alpha Corp.")
