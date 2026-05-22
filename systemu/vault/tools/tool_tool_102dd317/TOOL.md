---
name: notify_desktop
tool_type: cli_command
status: deployed
enabled: true
dependencies:
  - plyer
---

# notify_desktop

## Description

Show a desktop notification or toast message

## Parameters

- title (string, optional): Notification title
- message (string, optional): Notification body text
- timeout (integer, default: 5): Auto-dismiss after N seconds

## Returns

- success (boolean)
- error (string)

## Implementation Notes

_No implementation notes yet._
