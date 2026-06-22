# tests/test_v0935_generalization_model.py
"""v0.9.35 Phase 1 — ScrollParameter model + Scroll.generalization/parameters."""
from __future__ import annotations

import json
import uuid

import pytest

from systemu.core.models import Scroll, ScrollParameter


def _minimal_scroll(**overrides) -> Scroll:
    base = dict(
        id=f"scroll_{uuid.uuid4().hex[:8]}",
        name="Test SOP",
        source_session_id="sess_test",
        raw_instructions_path="",
        narrative_md="Do the thing.",
    )
    base.update(overrides)
    return Scroll(**base)


# ─── ScrollParameter ──────────────────────────────────────────────────────────

class TestScrollParameter:
    def test_minimal_param_only_name_required(self):
        p = ScrollParameter(name="product")
        assert p.name == "product"
        assert p.description == ""
        assert p.type == "string"          # default
        assert p.default is None
        assert p.salient_kind is None
        assert p.enum is None
        assert p.format is None
        assert p.required is True           # default

    def test_full_param_round_trips(self):
        p = ScrollParameter(
            name="report_date",
            description="The reporting period",
            type="string",
            default="2026-03-01",
            salient_kind="date",
            enum=["2026-03-01", "2026-04-01"],
            format="date",
            required=False,
        )
        restored = ScrollParameter.model_validate_json(p.model_dump_json())
        assert restored.default == "2026-03-01"
        assert restored.salient_kind == "date"
        assert restored.enum == ["2026-03-01", "2026-04-01"]
        assert restored.format == "date"
        assert restored.required is False

    def test_type_literal_rejects_unknown(self):
        with pytest.raises(Exception):
            ScrollParameter(name="x", type="object")  # not in the Literal set

    def test_type_accepts_all_documented(self):
        for t in ("string", "number", "integer", "boolean"):
            assert ScrollParameter(name="x", type=t).type == t

    def test_default_is_any_typed(self):
        # default holds THE CAPTURED VALUE — any JSON-serialisable type.
        assert ScrollParameter(name="x", type="integer", default=42).default == 42
        assert ScrollParameter(name="x", type="boolean", default=True).default is True


# ─── Scroll.generalization + Scroll.parameters ────────────────────────────────

class TestScrollGeneralizationFields:
    def test_defaults_are_none_and_empty(self):
        s = _minimal_scroll()
        assert s.generalization is None       # None == standard == today
        assert s.parameters == []

    def test_round_trip_dict_mode(self):
        s = _minimal_scroll(
            generalization="broad",
            parameters=[ScrollParameter(name="product", default="Widget-A",
                                        salient_kind="product")],
        )
        data = s.model_dump(mode="json")      # the exact path vault.save_scroll uses
        assert data["generalization"] == "broad"
        assert data["parameters"][0]["name"] == "product"
        assert data["parameters"][0]["default"] == "Widget-A"

        restored = Scroll.model_validate(data)
        assert restored.generalization == "broad"
        assert len(restored.parameters) == 1
        assert isinstance(restored.parameters[0], ScrollParameter)
        assert restored.parameters[0].salient_kind == "product"

    def test_generalization_literal_rejects_unknown(self):
        with pytest.raises(Exception):
            _minimal_scroll(generalization="loose")

    def test_generalization_accepts_all_three(self):
        for mode in ("broad", "standard", "narrow"):
            assert _minimal_scroll(generalization=mode).generalization == mode

    def test_old_scroll_json_without_new_fields_still_validates(self):
        # Backward-compat: a pre-v0.9.35 Scroll dict validates with the defaults.
        old_style = {
            "id": "scroll_legacy",
            "name": "Legacy SOP",
            "source_session_id": "sess_old",
            "raw_instructions_path": "",
            "narrative_md": "Old prose.",
            "status": "approved",
            # No generalization / parameters keys at all.
        }
        s = Scroll.model_validate(old_style)
        assert s.generalization is None
        assert s.parameters == []

    def test_parameters_from_raw_dicts_coerce_to_model(self):
        # The refiner will hand the refine flow plain dicts; they must coerce.
        s = _minimal_scroll(parameters=[{"name": "site", "default": "github.com"}])
        assert isinstance(s.parameters[0], ScrollParameter)
        assert s.parameters[0].name == "site"
        assert s.parameters[0].required is True
