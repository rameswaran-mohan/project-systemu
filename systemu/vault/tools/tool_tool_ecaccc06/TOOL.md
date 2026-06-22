---
name: take_screenshot
tool_type: cli_command
status: deployed
enabled: true
dependencies:
  - mss
  - pillow
---

# take_screenshot

## Description

Capture a screenshot of the full screen or a specific region

## Parameters

- output_path (string, optional): Where to save the screenshot PNG
- region (object, default: {}): Optional region dict with left,top,width,height

## Returns

- success (boolean)
- output_path (string)
- width (integer)
- height (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
