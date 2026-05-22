---
name: type_text
tool_type: cli_command
status: deployed
enabled: true
dependencies:
  - pynput
---

# type_text

## Description

Type text into the currently focused window using OS-level key events (fallback tool)

## Parameters

- text (string, optional): Text to type
- delay (number, default: 0.02): Seconds between keystrokes

## Returns

- success (boolean)
- error (string)

## Implementation Notes

_No implementation notes yet._
