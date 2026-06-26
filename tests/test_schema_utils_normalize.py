from systemu.core.schema_utils import normalize_parameters_schema, schema_param_names

WRAPPED = {
    "type": "object",
    "properties": {
        "source_path": {"type": "string"},
        "password": {"type": "string"},
        "output_path": {"type": "string"},
    },
    "required": ["source_path", "password"],
}
UNWRAPPED = {
    "source_path": {"type": "string"},
    "password": {"type": "string"},
    "output_path": {"type": "string"},
}


def test_unwraps_wrapped_schema_to_properties():
    assert list(normalize_parameters_schema(WRAPPED).keys()) == [
        "source_path", "password", "output_path"]


def test_folds_required_into_each_property():
    out = normalize_parameters_schema(WRAPPED)
    assert out["source_path"].get("required") is True
    assert out["password"].get("required") is True
    assert out["output_path"].get("required") in (False, None)


def test_idempotent_on_already_unwrapped():
    assert normalize_parameters_schema(UNWRAPPED) == UNWRAPPED


def test_param_names_from_wrapped():
    assert schema_param_names(WRAPPED) == ["source_path", "password", "output_path"]


def test_safe_on_non_dict_and_empty():
    assert normalize_parameters_schema({}) == {}
    assert normalize_parameters_schema(None) == {}
    assert schema_param_names("oops") == []
