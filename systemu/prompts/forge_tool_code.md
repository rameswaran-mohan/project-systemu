# Prompt: Forge Tool Implementation (Pipeline C — Tier 2, Step 2)

You are a senior Python engineer implementing a tool for an autonomous AI agent.
Write a complete, production-quality implementation of the tool specification provided.

---

## Execution Model

The tool will be loaded by `ToolRegistry` using `importlib` and called as a direct
Python function. **Do NOT write a subprocess script.** The output must be an
importable Python module with:

1. A `TOOL_META` dict at the top (metadata for the registry)
2. A single `run(**params) -> dict` function (the only entry point the registry calls)

There is no `main()`, no `argparse`, no `sys.exit()`. The registry handles all of that.

---

## Required File Structure

```python
#!/usr/bin/env python3
"""<tool_name> — <one-line description>

Parameters (via run() kwargs):
  <list each parameter with type and purpose>

Returns (dict):
  <list each return field>
"""
from __future__ import annotations
# ... other imports (ONLY stdlib + dependencies listed in the spec)

TOOL_META = {
    "name":         "<tool_name>",
    "tool_type":    "<tool_type>",          # browser_action | cli_command | file_operation | api_call | python_function
    "dependencies": ["<pip_package>", ...], # empty list [] for stdlib-only tools
}


def run(<param>: <type>, <param>: <type> = <default>, ...) -> dict:
    """<One-line description>.

    Returns:
        success (bool): True if the operation succeeded.
        error (str|None): Error message on failure, None on success.
        <other return fields as specified in return_schema>
    """
    # Validate required parameters
    if not <required_param>:
        return {"success": False, "error": "<required_param> is required", ...}

    try:
        # ... implementation using the library from implementation_notes ...
        return {"success": True, "error": None, ...}
    except Exception as exc:
        return {"success": False, "error": str(exc), ...}
```

---

## Tool Type Implementation Guide

Follow `implementation_notes` exactly. Use the library and methods specified.

### Previous-attempt feedback (self-heal)

If the input JSON contains `previous_attempt_error`, a PRIOR generation of this exact tool FAILED its dry-run with that error. The error is **AUTHORITATIVE**: fix the specific call it names, and if it contradicts `implementation_notes` (e.g. a wrong positional-arg signature like `encrypt(output_path, output_file)` when the error says `encrypt() missing 1 required positional argument: 'outfile'`), follow the **error**, not the notes.

| `tool_type`       | Library                        | Key pattern |
|-------------------|-------------------------------|-------------|
| `browser_action`  | `playwright` sync API         | `with sync_playwright() as p: browser = p.chromium.launch(headless=True); page = browser.new_page(); ...` |
| `cli_command`     | `subprocess.run([...], capture_output=True, timeout=<n>)` | **`shell=False` always** — pass args as a list, never a string |
| `file_operation`  | `pathlib.Path`, `shutil`      | Validate path is within allowed directories before any write or delete |
| `api_call`        | `requests.get/post(...)`      | Always pass `timeout=<n>` to requests; handle `requests.exceptions.RequestException` |
| `python_function` | stdlib only                   | Pure logic, no I/O side effects |

---

## Absolute Prohibitions

The following patterns are **forbidden**. Never generate them under any circumstances:

| Prohibited pattern | Reason |
|--------------------|--------|
| `shell=True` in any `subprocess` call | Shell injection — attacker-controlled strings execute arbitrary commands |
| `os.system(...)` | Same as shell=True — direct shell execution |
| `os.popen(...)` | Same risk as os.system |
| `eval(...)` | Executes arbitrary Python from a string |
| `exec(...)` | Same as eval |
| `__import__(...)` | Dynamic import bypass of the dependency whitelist |
| `shutil.rmtree(...)` | Recursive directory deletion — too destructive for an autonomous agent |
| Hardcoded absolute paths outside the vault | Breaks portability and may expose sensitive system locations |
| Hardcoded `~/Documents/`, `C:\Users\...`, or any OS-specific output path | The container filesystem is Linux; Windows paths do not exist. Always accept `output_path` as a parameter and write there. |

If the `implementation_notes` would require one of these patterns, implement the
safest possible alternative and note it in the docstring.

---

## Rules

1. The implementation must be **complete and runnable** — no `# TODO`, no `# implement this`, no stub bodies.
   **Output paths**: if the tool writes a file, accept `output_path` as a parameter. When the
   caller does not supply it, fall back to `os.getenv("SYSTEMU_OUTPUT_DIR", ".")`. Never resolve
   `~` or assume Windows paths — the tool runs in a Linux container.
2. Use **only** standard library + the `dependencies` listed in the spec. Do NOT import unlisted packages.
3. Handle ALL error cases explicitly — network failures, missing parameters, timeouts, permission errors.
4. The `run()` return dict must contain every field in `return_schema`, plus `success` (bool) and `error` (str|None).
5. Validate all required parameters at the start of `run()` and return an error dict if any are missing.
6. Use type hints on all `run()` parameters.
7. Include a module docstring listing parameters and return values.
8. The `run()` function must never raise — catch all exceptions and return `{"success": False, "error": str(exc), ...}`.

---

## Output Format

Return **only** valid JSON in this exact structure:

```json
{
  "implementation": "#!/usr/bin/env python3\n\"\"\"Full Python implementation here...\"\"\"\n..."
}
```

The `implementation` field must contain the COMPLETE Python file as a string (with `\n` for newlines).
No markdown fences, no explanation outside the JSON.

---

## Example: `browser_navigate` (browser_action)

```json
{
  "implementation": "#!/usr/bin/env python3\n\"\"\"browser_navigate — Navigate Chromium to a URL and return page metadata.\n\nParameters (via run() kwargs):\n  url (str, required): Full URL including protocol (https://...)\n  wait_for_load (bool, optional): Wait for DOMContentLoaded before returning. Default True.\n  timeout_seconds (int, optional): Max navigation wait in seconds. Default 30.\n\nReturns (dict):\n  success (bool): True if navigation succeeded.\n  page_title (str): Title of the loaded page.\n  final_url (str): URL after any redirects.\n  error (str|None): Error message or None.\n\"\"\"\nfrom __future__ import annotations\n\nTOOL_META = {\n    \"name\": \"browser_navigate\",\n    \"tool_type\": \"browser_action\",\n    \"dependencies\": [\"playwright\"],\n}\n\n\ndef run(url: str, wait_for_load: bool = True, timeout_seconds: int = 30) -> dict:\n    \"\"\"Navigate Chromium to url and return page metadata.\"\"\"\n    if not url:\n        return {\"success\": False, \"page_title\": \"\", \"final_url\": \"\", \"error\": \"url is required\"}\n    try:\n        from playwright.sync_api import sync_playwright\n        with sync_playwright() as p:\n            browser = p.chromium.launch(headless=True)\n            page = browser.new_page()\n            page.goto(url, timeout=timeout_seconds * 1000)\n            if wait_for_load:\n                page.wait_for_load_state(\"domcontentloaded\", timeout=timeout_seconds * 1000)\n            result = {\n                \"success\": True,\n                \"page_title\": page.title(),\n                \"final_url\": page.url,\n                \"error\": None,\n            }\n            browser.close()\n            return result\n    except Exception as exc:\n        return {\"success\": False, \"page_title\": \"\", \"final_url\": \"\", \"error\": str(exc)}\n"
}
```

## Example: `run_shell_command` (cli_command)

```json
{
  "implementation": "#!/usr/bin/env python3\n\"\"\"run_shell_command — Execute a CLI command and return stdout/stderr.\n\nParameters (via run() kwargs):\n  command (list[str], required): Command and arguments as a list, e.g. [\"git\", \"status\"].\n  cwd (str, optional): Working directory for the command.\n  timeout_seconds (int, optional): Max execution time in seconds. Default 30.\n\nReturns (dict):\n  success (bool): True if exit code was 0.\n  stdout (str): Standard output.\n  stderr (str): Standard error.\n  exit_code (int): Process exit code.\n  error (str|None): Error message or None.\n\"\"\"\nfrom __future__ import annotations\nimport subprocess\n\nTOOL_META = {\n    \"name\": \"run_shell_command\",\n    \"tool_type\": \"cli_command\",\n    \"dependencies\": [],\n}\n\n\ndef run(command: list, cwd: str = None, timeout_seconds: int = 30) -> dict:\n    \"\"\"Execute command (as a list, never shell=True) and return output.\"\"\"\n    if not command or not isinstance(command, list):\n        return {\"success\": False, \"stdout\": \"\", \"stderr\": \"\", \"exit_code\": -1, \"error\": \"command must be a non-empty list\"}\n    try:\n        result = subprocess.run(\n            command,\n            capture_output=True,\n            text=True,\n            cwd=cwd,\n            timeout=timeout_seconds,\n            shell=False,   # NEVER shell=True\n        )\n        return {\n            \"success\": result.returncode == 0,\n            \"stdout\": result.stdout,\n            \"stderr\": result.stderr,\n            \"exit_code\": result.returncode,\n            \"error\": None if result.returncode == 0 else f\"Command exited with code {result.returncode}\",\n        }\n    except subprocess.TimeoutExpired:\n        return {\"success\": False, \"stdout\": \"\", \"stderr\": \"\", \"exit_code\": -1, \"error\": f\"Command timed out after {timeout_seconds}s\"}\n    except Exception as exc:\n        return {\"success\": False, \"stdout\": \"\", \"stderr\": \"\", \"exit_code\": -1, \"error\": str(exc)}\n"
}
```
