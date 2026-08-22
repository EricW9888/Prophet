from __future__ import annotations

import re

# Central hygiene policy for internal labels and extractor debris. This is not
# investment-domain intelligence; it is schema sanitation for text that should
# never become a portfolio subject or external-research question.
ARTIFACT_PREFIX_RE = re.compile(
    r"^\s*(auto research|autonomous reflection|autonomous discovery|"
    r"research on|research|assistant turn|user turn)\s*(?:[:\-]\s*|\s+)",
    re.IGNORECASE,
)
ARTIFACT_SUBJECT_RE = re.compile(
    r"\b(unclassified research|research on research|research on unclassified research|"
    r"autonomous reflection cycle)\b",
    re.IGNORECASE,
)
UNUSABLE_SUBJECT_RE = re.compile(r"^(https?://|www\.|\d|\$|[a-z])")
TICKER_BASKET_SUBJECT_RE = re.compile(
    r"^[A-Z][A-Z0-9.]{0,5}(?:\s*,\s*[A-Z][A-Z0-9.]{0,5}){2,}$"
)
PORTFOLIO_BASKET_SUBJECT_RE = re.compile(
    r"(?i)^\s*(?:portfolio\s+)?holdings?\s*\(\s*"
    r"[A-Z][A-Z0-9.]{0,5}(?:\s*,\s*[A-Z][A-Z0-9.]{0,5}){1,}\s*\)\s*$"
)
QUESTION_SUBJECT_RE = re.compile(
    r"(?i)^\s*(what|which|why|how|when|where|who|can|could|should|would|is|are|do|does|did)\b"
)
MONEY_OR_POSITION_FRAGMENT_RE = re.compile(
    r"(?i)(^\s*\$|%\s*position\b|\bposition\s+size\b|\bturn id\b|\belapsed\b)"
)
PROPER_ENTITY_SUFFIX_RE = re.compile(
    r"(?i)\b(inc|inc\.|corp|corp\.|corporation|co\.|company|ltd|limited|plc|llc|lp|sa|ag|nv|group|holdings)\b"
)
TOPIC_NOUN_RE = re.compile(
    r"(?i)\b("
    r"adoption|automation|bandwidth|benchmark|bill|bills|board|capex|capital|capacity|"
    r"commodity|competition|competitors|concentration|construction|credit|cycle|data center|"
    r"demand|depreciation|derivatives|drawdown|funding|industry|insurance|load|market|"
    r"manufacturing|margin|methodology|policy|pricing|process|regulation|regulatory|risk|"
    r"robotaxi|robots|sector|software|standard|standards|storage|strategy|supply|tax|"
    r"utility|vendor"
    r")\b"
)
GENERIC_SINGLE_WORD_TOPIC_LABELS = {
    "implementation",
    "industry",
}
INTERNAL_ARTIFACT_LABELS = {
    "general research",
    "unclassified research",
    "research",
    "auto research",
    # UI/LLM action labels that sometimes leak from tool/error handling. These
    # are not investable subjects and should never become graph entities.
    "oops",
    "skip",
    "both",
}

PLACEHOLDER_PROFILE_MARKERS = (
    "does not have stored research on this topic yet",
    "has some stored context relevant to this subject, but not enough targeted research",
    "has some relevant stored context for this subject, but not enough targeted evidence",
    "does not yet have enough stored research to make a high-conviction opportunity call",
    "found no direct or connected research in your current knowledge graph",
)
PLACEHOLDER_PROFILE_ACTION_MARKERS = (
    "a research pass would help build a proper evidence base",
    "a focused research pass would help build a stronger evidence base",
    "the right next step is to investigate likely beneficiaries, losers, and possible reallocations",
    "initiating a new research pass would be the best next step",
)
META_PROFILE_SENTENCE_PREFIXES = (
    "the evidence packet is thin",
    "evidence packet is too thin",
    "the evidence packet is too thin",
    "the stored evidence packet is empty",
)


def compact_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def is_placeholder_profile_text(value: str | None) -> bool:
    normalized = compact_key(value)
    if not normalized:
        return False
    return (
        "portfolio has " in normalized
        and any(marker in normalized for marker in PLACEHOLDER_PROFILE_MARKERS)
        and any(marker in normalized for marker in PLACEHOLDER_PROFILE_ACTION_MARKERS)
    )


def label_from_profile_texts(values: list[str], *, max_length: int = 140) -> str | None:
    for value in values:
        text = " ".join((value or "").split()).strip()
        if not text or is_placeholder_profile_text(text):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = re.sub(r"(?i)^(however|but),\s+", "", sentence)
            sentence = sentence.strip(" .·-—").strip()
            if len(sentence) < 12:
                continue
            if sentence[:1].islower():
                sentence = sentence[:1].upper() + sentence[1:]
            if any(
                sentence.casefold().startswith(prefix)
                for prefix in META_PROFILE_SENTENCE_PREFIXES
            ):
                continue
            if len(sentence) <= max_length:
                return sentence
            clipped = sentence[: max_length - 3].rsplit(" ", 1)[0]
            return f"{clipped}..."
    return None


def normalize_subject_name(name: str | None) -> str:
    """Normalize an extracted subject label without making domain claims."""
    s = (name or "").strip()
    for _ in range(8):
        stripped = ARTIFACT_PREFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    if ":" in s:
        head = s.split(":", 1)[0].strip()
        if head and len(head) <= 48:
            s = head
    if " / " in s and len(s) > 48:
        s = s.split(" / ", 1)[0].strip()
    return s.strip(" .·-—").strip()


def is_internal_artifact_text(text: str | None) -> bool:
    normalized = compact_key(text)
    if not normalized:
        return False
    if normalized in INTERNAL_ARTIFACT_LABELS:
        return True
    return any(
        marker in normalized
        for marker in (
            "auto research:",
            "autonomous discovery:",
            "autonomous reflection:",
            "autonomous reflection cycle",
            "operating loop refresh",
            "unclassified research",
            "research on research",
            "research on unclassified research",
            "research on: what additional evidence",
            "current view on research",
            "current thesis on research",
        )
    )


def is_ticker_basket_subject(value: str | None) -> bool:
    compact = " ".join((value or "").split())
    return bool(
        TICKER_BASKET_SUBJECT_RE.fullmatch(compact)
        or PORTFOLIO_BASKET_SUBJECT_RE.fullmatch(compact)
    )


def is_topic_subject_name(value: str | None) -> bool:
    """Return true for broad topic/theme labels that should not be company entities.

    This is subject taxonomy hygiene, not investment reasoning. It keeps generic
    research topics such as `robotaxi`, `data center industry`, or `vendor
    lock-in` in the theme layer while preserving company-like labels such as
    `Apple Inc.` or lower-camel product/org names as entity candidates.
    """
    s = normalize_subject_name(value)
    key = compact_key(s)
    if not key or len(s) < 4:
        return False
    if is_artifact_subject_name(s) or is_ticker_basket_subject(s):
        return False
    if "?" in s or QUESTION_SUBJECT_RE.match(s):
        return False
    if MONEY_OR_POSITION_FRAGMENT_RE.search(s):
        return False
    if key.startswith(("http://", "https://", "www.")):
        return False
    if "/" in s and not TOPIC_NOUN_RE.search(s):
        return False
    if PROPER_ENTITY_SUFFIX_RE.search(s):
        return False

    tokens = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", s.replace("_", " "))
    if not tokens:
        return False
    # Lower-camel/proper product labels like eHouse Studio or mTab Halo are
    # often real org/product entities, not themes. Leave them for review.
    if any(
        token[0].islower() and any(ch.isupper() for ch in token[1:]) for token in tokens
    ):
        return False
    if len(tokens) == 1:
        return (
            len(tokens[0]) >= 5
            and key not in GENERIC_SINGLE_WORD_TOPIC_LABELS
            and bool(TOPIC_NOUN_RE.search(s))
        )
    return bool(TOPIC_NOUN_RE.search(s) or s[:1].islower() or "_" in s or "-" in s)


def is_unusable_subject(name: str | None) -> bool:
    s = (name or "").strip()
    if len(s) < 2 or len(s) > 80:
        return True
    if ARTIFACT_SUBJECT_RE.search(s):
        return True
    lowered = compact_key(s)
    if lowered in INTERNAL_ARTIFACT_LABELS:
        return True
    if lowered.startswith(
        ("research on ", "research ", "auto research ", "autonomous reflection ")
    ):
        return True
    if "?" in s:
        return True
    if is_ticker_basket_subject(s):
        return True
    return bool(UNUSABLE_SUBJECT_RE.match(s))


def is_artifact_question_text(text: str | None) -> bool:
    normalized = compact_key(text)
    if is_internal_artifact_text(normalized):
        return True
    # Generic ML-evaluation debris emitted by extraction/coverage models. This
    # belongs in hygiene, not routing or investment reasoning.
    if "missing classes" in normalized and (
        "the model" in normalized
        or "overall performance of the model" in normalized
        or normalized.startswith("how can the model")
        or normalized.startswith("what is the expected performance of the model")
    ):
        return True
    return False


def is_artifact_subject_name(value: str | None) -> bool:
    key = compact_key(value)
    if not key:
        return False
    if ARTIFACT_PREFIX_RE.match(value or ""):
        return True
    if is_ticker_basket_subject(value):
        return True
    if key in INTERNAL_ARTIFACT_LABELS:
        return True
    if key.startswith(
        (
            "research on ",
            "research ",
            "auto research ",
            "autonomous discovery ",
            "autonomous reflection ",
        )
    ):
        return True
    if "unclassified research" in key:
        return True
    return False


def is_artifact_research_query(value: str | None) -> bool:
    compact = " ".join((value or "").split())
    key = compact_key(compact)
    stripped_key = compact_key(strip_research_wrappers(compact))
    if not key:
        return False
    if is_artifact_question_text(key):
        return True
    if "unclassified research" in key:
        return True
    if key.count("research on") >= 2:
        return True
    if key.startswith(("research on research", "research research")):
        return True
    if stripped_key in INTERNAL_ARTIFACT_LABELS:
        return True
    if re.search(
        r"(?i)\b(current view|current thesis)\s+on\s+(research|unclassified research)\b",
        compact,
    ):
        return True
    return False


def strip_research_wrappers(value: str | None) -> str:
    compact = " ".join((value or "").split()).strip()
    for _ in range(8):
        stripped = re.sub(
            r"(?i)^\s*(auto research|autonomous reflection|autonomous discovery|"
            r"research on|research|assistant turn|user turn)\s*(?:[:\-]\s*|\s+)",
            "",
            compact,
        ).strip()
        if stripped == compact:
            break
        compact = stripped
    return compact
