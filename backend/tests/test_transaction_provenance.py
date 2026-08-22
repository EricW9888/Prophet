from types import SimpleNamespace
from uuid import UUID

from investos.services.dashboard import DashboardService


def test_transaction_source_summary_prefers_broker_email_provenance():
    txn = SimpleNamespace(
        notes="Deterministic parse: BUY 1 ORBT @ $173.04",
        provenance_json={
            "source": "gmail",
            "source_type": "email_order_confirmation",
            "source_label": "Broker confirmation email",
            "raw_evidence_id": "55555555-5555-5555-5555-555555555555",
            "confidence": 1.0,
        },
    )

    summary = DashboardService._transaction_source_summary(txn)

    assert summary["source_type"] == "email_order_confirmation"
    assert summary["source_label"] == "Broker confirmation email"
    assert summary["source_evidence_id"] == UUID("55555555-5555-5555-5555-555555555555")
    assert summary["source_confidence"] == 1.0


def test_transaction_source_summary_falls_back_to_manual_api():
    txn = SimpleNamespace(notes=None, provenance_json=None)

    summary = DashboardService._transaction_source_summary(txn)

    assert summary["source_label"] == "Manual/API"
    assert summary["source_evidence_id"] is None


def test_transaction_source_summary_names_manual_corrections():
    txn = SimpleNamespace(
        notes="Corrected ORBT fill",
        provenance_json={
            "source_type": "manual_correction",
            "source_label": "Manual correction",
            "corrects_transaction_id": "11111111-1111-1111-1111-111111111111",
        },
    )

    summary = DashboardService._transaction_source_summary(txn)

    assert summary["source_type"] == "manual_correction"
    assert summary["source_label"] == "Manual correction"
