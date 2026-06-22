# tests/runtime/test_param_resolution.py
from systemu.core.models import ScrollParameter
from systemu.runtime.param_resolution import slot_schema_from_parameters


def test_each_slot_is_required_absent_and_has_captured_default():
    params = [
        ScrollParameter(name="product", description="Item to order",
                        type="string", default="organic bananas",
                        salient_kind="product"),
        ScrollParameter(name="quantity", description="How many",
                        type="integer", default=3),
    ]
    schema = slot_schema_from_parameters(params)
    assert schema["type"] == "object"
    # KEY CONSTRAINT: every slot required[] ...
    assert set(schema["required"]) == {"product", "quantity"}
    # ... AND the captured value is the editable DEFAULT (pre-fill).
    assert schema["properties"]["product"]["default"] == "organic bananas"
    assert schema["properties"]["product"]["type"] == "string"
    assert schema["properties"]["quantity"]["default"] == 3
    assert schema["properties"]["quantity"]["type"] == "integer"


def test_enum_and_description_carry_through():
    params = [ScrollParameter(name="site", description="Which store",
                              type="string", default="amazon",
                              enum=["amazon", "walmart"])]
    schema = slot_schema_from_parameters(params)
    assert schema["properties"]["site"]["enum"] == ["amazon", "walmart"]
    assert schema["properties"]["site"]["description"] == "Which store"


def test_empty_parameters_yield_empty_properties():
    schema = slot_schema_from_parameters([])
    assert schema["properties"] == {}
    assert schema["required"] == []


from systemu.runtime.param_resolution import substitute_parameters


def _params():
    return [
        ScrollParameter(name="product", description="Item",
                        type="string", default="organic bananas"),
        ScrollParameter(name="store", description="Store",
                        type="string", default="amazon"),
    ]


def test_substitute_replaces_captured_default_in_objectives_and_intent():
    scroll_json = [
        {"id": 1, "goal": "Order organic bananas on amazon",
         "success_criteria": "organic bananas in cart"},
    ]
    intent = "Buy organic bananas from amazon"
    answers = {"product": "fuji apples", "store": "walmart"}
    new_json, new_intent, new_constraints, resolved = substitute_parameters(
        _params(), answers,
        scroll_json=scroll_json, intent=intent, constraints={},
    )
    assert new_json[0]["goal"] == "Order fuji apples on walmart"
    assert new_json[0]["success_criteria"] == "fuji apples in cart"
    assert new_intent == "Buy fuji apples from walmart"
    assert resolved == {"product": "fuji apples", "store": "walmart"}
    # input not mutated
    assert scroll_json[0]["goal"] == "Order organic bananas on amazon"


def test_unanswered_slot_falls_back_to_captured_default():
    scroll_json = [{"id": 1, "goal": "Order organic bananas",
                    "success_criteria": "done"}]
    # operator submitted nothing for 'store'; only 'product' answered
    new_json, _intent, _c, resolved = substitute_parameters(
        _params(), {"product": "fuji apples"},
        scroll_json=scroll_json, intent="", constraints={},
    )
    assert resolved["product"] == "fuji apples"
    assert resolved["store"] == "amazon"   # captured default retained
    assert new_json[0]["goal"] == "Order fuji apples"


def test_no_params_is_identity():
    sj = [{"id": 1, "goal": "x", "success_criteria": "y"}]
    new_json, intent, constraints, resolved = substitute_parameters(
        [], {}, scroll_json=sj, intent="z", constraints={"k": "v"},
    )
    assert new_json == sj and intent == "z" and constraints == {"k": "v"}
    assert resolved == {}
