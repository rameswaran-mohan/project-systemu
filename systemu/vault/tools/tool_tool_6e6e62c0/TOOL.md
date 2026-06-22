---
name: create_word_doc
tool_type: file_operation
status: deployed
enabled: true
dependencies:
  - python-docx
---

# create_word_doc

## Description

Create a Word .docx document with title, body text, and optional embedded image

## Parameters

- output_path (string, optional): Path for the output .docx file
- title (string, default: ): Document heading
- body_text (string, default: ): Body paragraph text
- image_path (string, default: ): Path to image to embed
- overwrite (boolean, default: True): Overwrite if exists

## Returns

- success (boolean)
- output_path (string)
- error (string)

## Implementation Notes

Use python-docx: Document(). If title is provided, add_heading(title, 0). If body_text, add_paragraph(body_text). If image_path exists, add_picture(image_path). Save to output_path. Expand ~ in paths with Path(output_path).expanduser(). Catch IOError and return error.
