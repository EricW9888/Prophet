"""Identifier-style metric and setup names are shown as words."""

import pytest

from investos.services.graph import GraphService, readable_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("customer_deposits_qoq", "Customer deposits QoQ"),
        ("equity_swap_realized_gain_month_2", "Equity swap realized gain month 2"),
        ("eps_ttm", "EPS TTM"),
        ("q3_revenue", "Q3 revenue"),
        ("yoy_growth", "YoY growth"),
        ("Net Assets", "Net Assets"),
        ("FY2025 Revenue", "FY2025 Revenue"),
        ("revenue", "revenue"),
        (None, None),
    ],
)
def test_readable_name(name, expected) -> None:
    assert readable_name(name) == expected


def test_graph_labels_use_readable_names() -> None:
    class Metric:
        metric_name = "local_affordability_squeeze"

    assert (
        GraphService(None)._node_label("fundamental_metric", Metric())
        == "Local affordability squeeze"
    )
