"""v0.9.6 — tool parameter reconciliation.

LLMs (and occasionally the forge's own schema↔code drift) supply parameter
names that don't match a tool's actual run() signature — e.g. the LLM calls
write_file(path=…) when run() declares output_path. Before this fix that raised
TypeError → "Parameter mismatch" → the objective was never credited and the run
parked. This reconciles supplied params onto the real run() signature so tools
actually execute. Verified as the blocker behind 3 consecutive live-run parks.
"""
import pytest

from systemu.runtime.tool_registry import _reconcile_params


class TestReconcileParams:
    def test_exact_match_unchanged(self):
        def run(output_path, content):
            ...
        out, notes = _reconcile_params(run, {"output_path": "a", "content": "b"})
        assert out == {"output_path": "a", "content": "b"}
        assert notes == []

    def test_kwargs_signature_passes_through(self):
        def run(**kwargs):
            ...
        params = {"anything": 1, "goes": 2}
        out, notes = _reconcile_params(run, params)
        assert out == params

    def test_path_synonym_remapped_to_output_path(self):
        """The exact live failure: LLM sent path=, tool wants output_path."""
        def run(output_path, content):
            ...
        out, notes = _reconcile_params(run, {"path": "/tmp/x", "content": "hi"})
        assert out == {"output_path": "/tmp/x", "content": "hi"}
        assert any("path" in n and "output_path" in n for n in notes)

    def test_output_path_synonym_remapped_to_path(self):
        """The reverse live failure: LLM sent output_path=, tool wants path."""
        def run(path, text):
            ...
        out, notes = _reconcile_params(run, {"output_path": "/tmp/x", "text": "hi"})
        assert out["path"] == "/tmp/x"
        assert out["text"] == "hi"

    def test_content_synonym_remapped(self):
        def run(output_path, content):
            ...
        out, _ = _reconcile_params(run, {"output_path": "/tmp/x", "text": "body"})
        assert out == {"output_path": "/tmp/x", "content": "body"}

    def test_extra_hallucinated_param_dropped(self):
        def run(path):
            ...
        out, _ = _reconcile_params(run, {"path": "/tmp/x", "encoding": "utf-8", "mode": "w"})
        assert out == {"path": "/tmp/x"}

    def test_single_unknown_single_unfilled_positional_fallback(self):
        """No synonym match, but exactly one unknown and one unfilled slot →
        map by position rather than fail."""
        def run(destination):
            ...
        out, notes = _reconcile_params(run, {"wherever": "/tmp/x"})
        assert out == {"destination": "/tmp/x"}

    def test_recognized_params_preserved_when_some_unknown(self):
        def run(output_path, content, overwrite=False):
            ...
        out, _ = _reconcile_params(
            run, {"path": "/tmp/x", "content": "hi", "overwrite": True},
        )
        assert out == {"output_path": "/tmp/x", "content": "hi", "overwrite": True}

    def test_non_dict_params_returned_unchanged(self):
        def run(x):
            ...
        out, notes = _reconcile_params(run, None)
        assert out is None
        assert notes == []

    def test_url_synonym(self):
        def run(url):
            ...
        out, _ = _reconcile_params(run, {"uri": "http://x"})
        assert out == {"url": "http://x"}
