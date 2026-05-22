---
name: image_resize
tool_type: file_operation
status: deployed
enabled: true
dependencies:
  - pillow
---

# image_resize

## Description

Resize an image file, maintaining aspect ratio if only one dimension is given

## Parameters

- input_path (string, optional): Path to input image
- output_path (string, optional): Path for resized output image
- width (integer, default: 0): Target width in pixels (0 for auto)
- height (integer, default: 0): Target height in pixels (0 for auto)
- quality (integer, default: 85): JPEG quality 1-95

## Returns

- success (boolean)
- output_path (string)
- width (integer)
- height (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
