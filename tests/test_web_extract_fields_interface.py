"""LLM-usability fix for web_extract — fields interface + heuristic fallback.

Motivated by live v0.8.22.1 failure on 'find top burrito places near me':

    WARNING [Runtime] Tool web_extract failed: Invalid parameters for 'web_extract':
    missing required parameter 'schema' (type object). Correct the parameters and
    call the tool again.

The LLM kept mis-calling web_extract because the vault tool record demanded a
full JSON Schema as a required parameter with no example or fallback. This test
suite locks in the LLM-friendly `fields=["name","url","rating"]` interface and
the schemaless heuristic fallback, while keeping backward compat for the
legacy `schema=` interface.
"""
from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest


_FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "sample_listings.html"


def _fixture_html() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


class _FakeResponse:
    """Minimal stub for requests.Response used by web_extract._fetch."""

    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _fake_fetch_factory(html: str, status_code: int = 200):
    def _fake_fetch(url, headers, params, timeout):
        return _FakeResponse(html, status_code)
    return _fake_fetch


class TestVaultRecordSchemaNotRequired:
    """Gate 2.5 (param validation) must not reject a call that omits `schema`
    when the LLM supplies `fields` instead. This is the bug that triggered the
    runaway loop in the live daemon log."""

    def test_schema_param_is_no_longer_required(self):
        import json
        rec_path = pathlib.Path(__file__).resolve().parent.parent / \
            "systemu/vault/tools/tool_tool_web_extract.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        params = rec["parameters_schema"]
        # The whole point of the fix: `schema` is no longer required.
        assert params["schema"].get("required") is not True, (
            "schema must NOT be required — that was the v0.8.22.1 LLM-loop bug"
        )

    def test_fields_param_is_declared_as_array(self):
        import json
        rec_path = pathlib.Path(__file__).resolve().parent.parent / \
            "systemu/vault/tools/tool_tool_web_extract.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        params = rec["parameters_schema"]
        assert "fields" in params, "fields param must be advertised in the vault record"
        assert params["fields"].get("type") == "array"

    def test_index_lists_fields_in_parameter_names(self):
        import json
        idx_path = pathlib.Path(__file__).resolve().parent.parent / \
            "systemu/vault/tools/index.json"
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        entry = next(e for e in idx if e["name"] == "web_extract")
        assert "fields" in entry["parameter_names"]
        assert "url" in entry["parameter_names"]


class TestFieldsInterface:
    """`fields=["name","url","rating"]` should work without the caller writing
    a JSON Schema — the runtime builds one internally."""

    def test_fields_only_builds_schema_and_returns_records(self):
        from systemu.vault.tools.implementations import web_extract

        captured = {}

        def _fake_extract_records(text, schema, max_records=20):
            captured["schema"] = schema
            return {"success": True,
                    "records": [
                        {"name": "El Charrito", "url": "/places/el-charrito"},
                        {"name": "Burrito Bros", "url": "/places/burrito-bros"},
                    ],
                    "count": 2, "error": None}

        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())), \
             patch("systemu.runtime.extractor.extract_records", _fake_extract_records):
            out = web_extract.run(url="https://example.com/burritos",
                                  fields=["name", "url"])
        assert out["success"] is True
        assert out["count"] == 2
        # The runtime must have built a JSON Schema from fields.
        built = captured["schema"]
        assert isinstance(built, dict)
        assert built.get("type") == "object"
        assert "name" in built["properties"]
        assert "url" in built["properties"]
        # Required = the field list (in order) so dedup_key works.
        assert built["required"][0] == "name"

    def test_fields_infers_numeric_types_for_rating_price(self):
        from systemu.vault.tools.implementations import web_extract

        captured = {}

        def _fake_extract_records(text, schema, max_records=20):
            captured["schema"] = schema
            return {"success": True, "records": [], "count": 0, "error": None}

        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())), \
             patch("systemu.runtime.extractor.extract_records", _fake_extract_records):
            web_extract.run(url="https://example.com/x",
                            fields=["name", "url", "rating", "price"])
        props = captured["schema"]["properties"]
        assert props["name"]["type"] == "string"
        assert props["url"]["type"] == "string"
        # rating + price should auto-type as number for LLM ergonomics.
        assert props["rating"]["type"] == "number"
        assert props["price"]["type"] == "number"


class TestBackwardCompatSchema:
    """Existing callers that pass `schema=` must keep working unchanged."""

    def test_schema_only_call_still_works(self):
        from systemu.vault.tools.implementations import web_extract

        seen = {}

        def _fake_extract_records(text, schema, max_records=20):
            seen["schema"] = schema
            return {"success": True,
                    "records": [{"name": "El Charrito"}],
                    "count": 1, "error": None}

        explicit_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())), \
             patch("systemu.runtime.extractor.extract_records", _fake_extract_records):
            out = web_extract.run(url="https://example.com/burritos",
                                  schema=explicit_schema)
        assert out["success"] is True
        # Explicit schema is passed through untouched.
        assert seen["schema"] == explicit_schema

    def test_explicit_schema_wins_over_fields(self):
        from systemu.vault.tools.implementations import web_extract

        seen = {}

        def _fake_extract_records(text, schema, max_records=20):
            seen["schema"] = schema
            return {"success": True, "records": [], "count": 0, "error": None}

        explicit_schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        }
        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())), \
             patch("systemu.runtime.extractor.extract_records", _fake_extract_records):
            web_extract.run(url="https://example.com/burritos",
                            schema=explicit_schema,
                            fields=["name", "url"])
        # When both are provided, the explicit `schema` wins.
        assert seen["schema"] == explicit_schema


class TestHeuristicFallback:
    """Neither schema nor fields supplied -> heuristic extraction returns
    something usable, not a hard error. This is what saves the LLM when it
    forgets BOTH params."""

    def test_neither_schema_nor_fields_returns_records_via_heuristic(self, caplog):
        from systemu.vault.tools.implementations import web_extract

        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())):
            with caplog.at_level("INFO"):
                out = web_extract.run(url="https://example.com/burritos")
        assert out["success"] is True, (
            "Heuristic mode must not return a hard error — that was the LLM-loop trigger"
        )
        assert out["count"] >= 1
        # We should have logged that we used the heuristic so an operator can see why.
        assert any("heuristic" in m.lower() for m in caplog.messages)

    def test_heuristic_picks_up_anchor_text_and_href(self):
        from systemu.vault.tools.implementations import web_extract

        with patch.object(web_extract, "_fetch", _fake_fetch_factory(_fixture_html())):
            out = web_extract.run(url="https://example.com/burritos")
        # At least one record should expose a name/title or url/href-like field.
        records = out["records"]
        assert len(records) >= 1
        first = records[0]
        # Either name/title or url/href should be populated for the first record.
        has_name_field = any(k in first for k in ("name", "title"))
        has_url_field = any(k in first for k in ("url", "href"))
        assert has_name_field or has_url_field, (
            f"heuristic mode produced an unrecognizable record: {first!r}"
        )


class TestUrlStillRequired:
    """`url` remains required — only `schema` got relaxed."""

    def test_missing_url_is_a_clean_error(self):
        from systemu.vault.tools.implementations import web_extract
        out = web_extract.run(fields=["name"])
        assert out["success"] is False
        assert out["error_type"] == "bad_request"
