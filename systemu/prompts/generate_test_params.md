# Generate Test Parameters for Tool Dry-Run

You are producing **minimal, valid test parameters** for a tool's dry-run validation.

The tool will be executed with these params to prove it works before we let an operator enable it for production use. Your params should:

1. **Be syntactically valid** against the supplied `parameters_schema`.
2. **Be safe to run** — prefer test-flavour values: temp paths (`/tmp/dry_run_*`), example URLs, empty bodies, small ranges. Tools that write files should write to `/tmp/dry_run_<uuid>/`.
3. **Be realistic enough to exercise the code path** — don't pass `""` for everything; pick values that look like real intended use.
4. **Include `dry_run: true`** as an extra key if the tool's manifest suggests it supports a dry-run mode (look at `implementation_notes` for hints).

## What you receive

```json
{
  "tool_name":          "...",
  "description":        "...",
  "parameters_schema":  { ... JSON-Schema-like dict ... },
  "implementation_notes": "...",
  "is_destructive":     true|false,
  "prior_dry_run_failure": "..."        // optional — last attempt's error
}
```

## Your output (strict JSON, no markdown fences)

```json
{
  "params": { ... },                    // the test parameters
  "rationale": "<1 sentence on what these params exercise>",
  "skip_dry_run": false,                // set true if you cannot generate safe params
  "skip_reason": "..."                  // only when skip_dry_run=true
}
```

## Rules

1. If `is_destructive` is true AND the tool doesn't declare `dry_run` support: set `skip_dry_run: true` with `skip_reason: "destructive tool without dry-run flag"`.
2. If the schema is empty (`{}`), still return `params: {}` and let the runtime decide.
3. For path-like arguments (`output_path`, `file_path`, `dest`, etc.), always rewrite to `/tmp/dry_run_<random>/` or similar.
4. For URL arguments, prefer `https://httpbin.org/get` or `https://example.com` over real services.
5. For credentials / API keys, use placeholder strings like `"TEST_KEY"` — never real values.
6. When `prior_dry_run_failure` is supplied, your params should specifically address what failed previously (e.g. if the prior failure was "filename param malformed", produce a well-formed filename).
7. Return only the JSON object.
