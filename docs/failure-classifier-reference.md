# Failure Classifier Reference

The rule-based failure classifier in `systemu/runtime/failure_classifier.py` categorises every tool failure into one of 10 classes. Used by:

1. **Reflection blocks** (v0.4.0-b) — strategy enum tuned to the category.
2. **Pattern signatures** (v0.4.0-a) — first part of the cross-shadow signature.
3. **Supervisor decisions** (v0.4.0-d) — cheap pre-filter so most calls don't escalate to Tier-1.

The classifier is **deterministic** (no LLM) and **cheap** (microseconds per call). Rules are checked in order; first match wins.

## Categories

| Category | Detection rules | Example trigger | Recommended strategies (in order) |
|---|---|---|---|
| `missing_dependency` | • Explicit `error_type` of `missing_dependency` / `dependency_install_*`<br>• Regex: `no module named` / `ModuleNotFoundError` | `ModuleNotFoundError: No module named 'docx'` | FAIL (only resolution is operator approval) |
| `timeout` | • `parsed.timed_out=True` flag<br>• Text contains "timed out" / "timeout" | Tool exceeded `default_timeout` | RETRY_WITH_DIFFERENT_PARAMS → TRY_DIFFERENT_TOOL → FAIL |
| `param_error` | • Text contains "missing required argument/parameter/field/key"<br>• "validation error" / "schema validation"<br>• "unexpected keyword argument" | `TypeError: foo() got unexpected keyword argument 'badparam'` | RETRY_WITH_DIFFERENT_PARAMS → LOAD_RESOURCE → FAIL |
| `http_error` | • Text contains `4XX` or `5XX` HTTP status code AND keyword like "http"/"status"/"response" | `HTTP 503 from upstream` | RETRY_WITH_DIFFERENT_PARAMS → TRY_DIFFERENT_TOOL → FAIL |
| `network_error` | • "connection refused" / "connection reset" / "max retries exceeded" / "ssl" / "tls" / "dns" / "name resolution" | `Connection refused on port 443` | RETRY_WITH_DIFFERENT_PARAMS → FAIL |
| `permission_error` | • "permission denied" / "PermissionError" / "EACCES" / "access is denied" | `PermissionError: /root/secret` | FAIL (operator must fix; no autoresolve) |
| `file_not_found` | • "no such file" / "FileNotFoundError" / "ENOENT" / "errno 2" | `FileNotFoundError: 'config.yaml'` | RETRY_WITH_DIFFERENT_PARAMS → FAIL |
| `parse_error` | • "JSONDecodeError" / "invalid json" / "yaml.error" / "expecting value" / "expecting property" | `JSONDecodeError: Expecting value: line 1 column 1` | RETRY_WITH_DIFFERENT_PARAMS → FAIL |
| `api_error` | • "rate limit" / "RateLimitError" / "openrouter" / "openai" / "anthropic" / "deepseek" / "service unavailable" / "bad gateway" | `RateLimitError: 429 from OpenRouter` | RETRY_WITH_DIFFERENT_PARAMS → FAIL |
| `unknown` | No other rule matched | `something weird happened` | RETRY_WITH_DIFFERENT_PARAMS, TRY_DIFFERENT_TOOL, LOAD_RESOURCE, FAIL |

## Confidence levels

- **`high`** — explicit `error_type` in the parsed result (structured upstream signal)
- **`medium`** — regex / keyword match in the error text
- **`low`** — fallback to `unknown` when no rule matched

## Keyword extraction

For pattern signatures and reflection blocks, the classifier also extracts a short keyword:

- `missing_dependency` → the module name (e.g. `docx`)
- `param_error` → the failing parameter name
- `http_error` → the status code (e.g. `503`)
- `file_not_found` → the first path-like token (e.g. `config.yaml`)
- Others → a representative keyword for the category

## Adding a new category

1. Add the name to `CATEGORIES` in `systemu/runtime/failure_classifier.py`.
2. Insert a rule tuple in `_RULES` (predicate + keyword extractor).
3. Add the strategy ordering in `reflection_strategies_for()`.
4. Document the trigger + recommended strategies in this file.
5. Add unit tests in `tests/test_v040b_recovery.py`.

The classifier is **deliberately conservative** — false unknowns are fine, false categorisations are not. When in doubt, leave the rule out.

## Inspecting classifications in practice

The classifier output is recorded in:

- `data/failure_telemetry.jsonl` (under `error_type` field for `tool_failure` events)
- `data/audit/exec_<id>/supervisor.jsonl` (under `classifier` field on supervisor decisions)

Histogram by category:

```bash
python -m sharing_on debug failure-histogram --group-by error_type
```
