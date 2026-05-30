---
name: run_cli_command
tool_type: cli_command
status: deployed
enabled: true
dependencies:
  []
---

# run_cli_command

## Description

Execute a shell command in the container and capture its stdout and stderr output.

## Parameters

- command (string): Shell command to execute (e.g., 'python --version')
- timeout_seconds (integer, default: 30): Maximum time to wait for command completion

## Returns

- success (boolean)
- stdout (string)
- stderr (string)
- return_code (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
