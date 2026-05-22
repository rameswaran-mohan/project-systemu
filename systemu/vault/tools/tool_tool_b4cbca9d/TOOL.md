---
name: close_application
tool_type: cli_command
status: deployed
enabled: true
dependencies: []
---

# close_application

## Description

Close a running desktop application by name or PID

## Parameters

- application_name (string, default: ): App name or executable
- pid (integer, optional): PID to terminate directly
- force (boolean, default: False): Kill immediately

## Returns

- success (boolean)
- killed (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
