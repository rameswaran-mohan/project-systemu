---
name: run_command
tool_type: cli_command
status: deployed
enabled: true
dependencies: []
---

# run_command

## Description

Execute a shell command and return stdout, stderr, and exit code

## Parameters

- command (string, optional): Shell command to run
- timeout (integer, default: 30): Timeout in seconds
- cwd (string, default: ): Working directory

## Returns

- success (boolean)
- stdout (string)
- stderr (string)
- return_code (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
