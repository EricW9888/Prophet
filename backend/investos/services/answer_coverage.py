"""Question coverage is distinct from accepting an investment conclusion."""

from collections import Counter
from urllib.parse import quote, urlsplit

from investos.services.quantity_evidence import audit_numeric_narrative

INFORMATION_NEED_REVIEW_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "need_index": {"type": "integer", "minimum": 0},
            "status": {"type": "string", "enum": ["answered", "partial", "unanswered"]},
            "answer": {"type": "string"},
            "basis": {
                "type": "string",
                "enum": ["source_report", "measurement", "conditional", "unavailable"],
            },
            "assertion_indices": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0},
            },
            "assumption_index": {"type": ["integer", "null"], "minimum": 0},
            "limitation": {"type": "string"},
        },
        "required": [
            "need_index",
            "status",
            "answer",
            "basis",
            "assertion_indices",
            "assumption_index",
            "limitation",
        ],
    },
}

INFORMATION_NEED_REVIEW_RULES = (
    "Answer the user directly from the supplied evidence and independently assessed assertions. Explain the mechanism when asked why something matters. "
    "Distinguish reported facts, measured information, conditional implications, and unknowns. Do not invent realized financial losses, dates, exposure or future actions. "
    "Answer each answer_needs item in information_need_reviews using its zero-based need_index. "
    "These needs come from the user's words. The original question controls scope; do not invent additional prerequisites. "
    "Do not demand a complete investment thesis, historical study or price-impact dataset for a supported explanation unless the user requested it. "
    "Give a concise user-facing answer for each answerable core need, including the actual mechanism or implication when requested. "
    "source_report answers must cite indices of the supplied material_assertions that bind retrieved evidence; they describe what sources report, not independent confirmation. "
    "An evidence limit can itself answer a question: explain what the reported facts do and do not establish, using source_report with partial status when the requested causal or financial conclusion is unknown. Do not suppress that useful explanation merely because the conclusion cannot be established. "
    "measurement answers must cite indices of the supplied material_assertions with valid typed quantity bindings. "
    "Conditional reasoning is allowed as an explicitly contingent explanation, never as an established event, cause, forecast or investment recommendation: "
    "cite an assumption_index with a concrete falsifier and explain its limitation. "
    "Use unanswered with an empty answer when no supported or appropriately conditional response is possible. "
    "Do not insert operating promises, future answers or claims that research was queued into these answers; actual admitted work is reported separately. "
    "Judge partial coverage by the core question, not by missing optional enrichment. Keep acceptance of an investment conclusion separate from explanation. "
)

ANSWER_COVERAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"information_need_reviews": INFORMATION_NEED_REVIEW_SCHEMA},
    "required": ["information_need_reviews"],
}


def requested_answer_needs(message: str, intent: dict | None = None) -> list[str]:
    """Use literal requested parts, never a planner's expanded study checklist."""
    question = " ".join(message.split())
    proposed = (intent or {}).get("requested_information")
    if not isinstance(proposed, list) or not proposed:
        return [question]
    needs = list(dict.fromkeys(" ".join(str(part).split()) for part in proposed))
    if any(not part or part not in question for part in needs):
        return [question]
    return needs


def assess_answer_coverage(
    plan: dict, review: dict, packet_context: dict | None = None
) -> dict | None:
    """Bind per-need replies to the existing independently assessed assertions.

    This checks identity and evidence admission, not semantic entailment. Model
    interpretation remains inspectable; no coverage result grants promotion.
    Historical reviews without a coverage contract retain their old behavior.
    """
    needs = plan.get("answer_information_needs") or plan.get("information_needs") or []
    raw = review.get("information_need_reviews")
    if not needs or not isinstance(raw, list):
        return None
    rows = [row for row in raw if isinstance(row, dict)]
    counts = Counter(
        row.get("need_index") for row in rows if type(row.get("need_index")) is int
    )
    assertions = (review.get("corroboration") or {}).get("assertions") or []
    proposals = review.get("material_assertions") or []
    assumptions = review.get("assumptions") or []
    nodes = {
        str(node.get("id")): node
        for layer in (
            "direct_evidence",
            "connected_evidence",
            "historical_evidence",
            "contradiction_evidence",
        )
        for node in (packet_context or {}).get(layer) or []
        if isinstance(node, dict) and node.get("id")
    }
    output = []
    delivery_assertions = []
    for index, need in enumerate(needs):
        row = next(
            (
                r
                for r in rows
                if type(r.get("need_index")) is int and r["need_index"] == index
            ),
            {},
        )
        status = row.get("status", "unanswered")
        answer = str(row.get("answer") or "").strip()
        basis = row.get("basis", "unavailable")
        limitation = str(row.get("limitation") or "").strip()
        admitted = (
            counts[index] == 1 and status in {"answered", "partial"} and bool(answer)
        )
        refs = row.get("assertion_indices") or []
        bound = [
            assertions[i] for i in refs if type(i) is int and 0 <= i < len(assertions)
        ]
        falsifier = None
        if basis in {"source_report", "measurement"}:
            allowed = (
                {"corroborated", "single_source"}
                if basis == "source_report"
                else {"account_evidence", "calculated_evidence"}
            )
            admitted = (
                admitted
                and bool(refs)
                and len(bound) == len(refs)
                and all(a.get("status") in allowed for a in bound)
                and all(
                    i < len(proposals)
                    and str(proposals[i].get("statement") or "").strip()
                    == assertions[i].get("statement")
                    and (
                        basis != "source_report"
                        or set(
                            map(str, proposals[i].get("supporting_evidence_ids") or [])
                        )
                        == set(assertions[i].get("supporting_evidence_ids") or [])
                    )
                    for i in refs
                    if type(i) is int and 0 <= i < len(assertions)
                )
            )
            if basis == "measurement":
                admitted = (
                    admitted and audit_numeric_narrative([answer], bound)["verified"]
                )
        elif basis == "conditional":
            assumption_index = row.get("assumption_index")
            assumption = (
                assumptions[assumption_index]
                if type(assumption_index) is int
                and 0 <= assumption_index < len(assumptions)
                else {}
            )
            falsifier = str(assumption.get("falsifier") or "").strip()
            admitted = (
                admitted
                and bool(assumption.get("statement"))
                and bool(falsifier)
                and bool(limitation)
            )
            status = "partial"
        else:
            admitted = False
        if admitted and basis in {"source_report", "measurement"}:
            delivery_assertions.extend(a for a in bound if a not in delivery_assertions)
        source_refs = []
        for assertion in bound if admitted else []:
            for evidence_id in assertion.get("supporting_evidence_ids") or []:
                source = (nodes.get(str(evidence_id)) or {}).get("source") or {}
                url = str(source.get("evidence_url") or source.get("url") or "")
                try:
                    safe_url = urlsplit(url).scheme in {"https", "http"}
                except ValueError:
                    safe_url = False
                if safe_url and url not in [s["url"] for s in source_refs]:
                    source_refs.append(
                        {
                            "url": url,
                            "title": str(
                                source.get("evidence_title")
                                or source.get("name")
                                or "Source"
                            ),
                        }
                    )
        output.append(
            {
                "need_index": index,
                "need": str(need),
                "status": status if admitted else "unanswered",
                "answer": answer if admitted else "",
                "basis": basis if admitted else "unavailable",
                "limitation": limitation
                or (
                    "The reviewed evidence does not establish this part."
                    if not admitted
                    else ""
                ),
                "supporting_evidence_ids": (
                    list(
                        dict.fromkeys(
                            e
                            for a in bound
                            for e in a.get("supporting_evidence_ids") or []
                        )
                    )
                    if admitted
                    else []
                ),
                "supporting_context_paths": (
                    list(
                        dict.fromkeys(
                            p
                            for a in bound
                            for p in a.get("valid_supporting_context_paths") or []
                        )
                    )
                    if admitted
                    else []
                ),
                "falsifier": falsifier if admitted else None,
                "sources": source_refs,
            }
        )
    answered = sum(bool(row["answer"]) for row in output)
    complete = all(row["status"] == "answered" for row in output)
    return {
        "status": "adequate" if complete else "partial" if answered else "inadequate",
        "needs": output,
        "delivery_assertions": delivery_assertions,
        "optional_information_needs": plan.get("optional_information_needs") or [],
        "unmatched_review_count": sum(
            1
            for row in rows
            if type(row.get("need_index")) is not int
            or not 0 <= row["need_index"] < len(needs)
        ),
    }


def render_answer_coverage(coverage: dict | None) -> str | None:
    if not coverage or not any(
        row.get("answer") for row in coverage.get("needs") or []
    ):
        return None
    sections = []
    rows = coverage["needs"]
    for row in rows:
        if not row["answer"]:
            continue
        label = {
            "source_report": "Reported evidence",
            "measurement": "Measured",
            "conditional": "Conditional interpretation",
        }[row["basis"]]
        heading = f"### {row['need']}\n\n" if len(rows) > 1 else ""
        section = f"{heading}**{label}:** {row['answer']}"
        if row["limitation"]:
            section += f"\n\nLimit: {row['limitation']}"
        if row.get("falsifier"):
            section += f"\n\nWhat would break this interpretation: {row['falsifier']}"
        if row.get("sources"):
            links = []
            for source in row["sources"]:
                title = source["title"].replace("[", "\\[").replace("]", "\\]")
                url = quote(source["url"], safe=":/?&=%#@+;,")
                links.append(f"[{title}]({url})")
            section += "\n\nSources: " + ", ".join(links)
        sections.append(section)
    missing = [
        (
            f"{row['need']}: {row['limitation']}"
            if not row["answer"] and row.get("limitation")
            else row["need"]
        )
        for row in rows
        if row["status"] != "answered"
    ]
    if missing:
        sections.append(
            "Still unresolved:\n" + "\n".join(f"- {need}" for need in missing)
        )
    return "\n\n".join(sections)
