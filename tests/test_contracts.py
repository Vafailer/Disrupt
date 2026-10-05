import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.contracts import AdminSummary, Percentage
from app.main import create_app


def test_admin_reference_shape_and_nulls():
    raw = json.loads(Path("docs/fixtures/admin-summary.json").read_text())
    summary = AdminSummary.model_validate(raw)
    assert summary.cards.unique_users == 2
    assert sum(d.dau for d in summary.daily) == summary.quality.costs.dau_sum == 3
    assert summary.retention.d7.denominator == 0 and summary.retention.d7.value is None
    assert summary.quality.costs.llm_cost is None
    assert summary.quality.costs.known_llm_cost == "0.25000000"
    assert summary.quality.costs.unknown_usage_calls == 1
    assert summary.cards.ai_activation.value == 50.0


@pytest.mark.parametrize(
    "raw",
    [
        {"numerator": 1, "denominator": 0, "value": 0.0},
        {"numerator": 0, "denominator": 0, "value": 0.0},
        {"numerator": 1, "denominator": 2, "value": 0.0},
        {"numerator": 1, "denominator": 2, "value": None},
    ],
)
def test_percentages_do_not_hide_missing_denominator(raw):
    with pytest.raises(ValidationError):
        Percentage.model_validate(raw)


def test_published_contract_marks_unimplemented_routes_and_valid_refs():
    spec = json.loads(Path("docs/integration-v1.openapi.json").read_text())
    assert spec["paths"]["/internal/v1/telegram/updates"]["post"]["x-implementation-status"] == "implemented"
    assert spec["paths"]["/api/admin/summary"]["get"]["x-implementation-status"] == "contract-only"
    app = create_app(Settings(auto_worker=False, database_url="sqlite:///:memory:"))
    try:
        runtime = app.openapi()
        assert "/api/admin/summary" not in runtime["paths"]
        assert "/internal/v1/deliveries/claim" not in runtime["paths"]
        for path in runtime["paths"]:
            assert path in spec["paths"]
    finally:
        app.state.engine.dispose()

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = spec
                for key in value["$ref"].removeprefix("#/").split("/"):
                    target = target[key]
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(spec)
