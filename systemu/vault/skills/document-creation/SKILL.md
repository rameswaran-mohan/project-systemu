---
name: document-creation
description: Proficiency in creating formatted Word documents with text content and
  embedded images
metadata:
  category: file_ops
  proficiency_level: beginner
  required_tools:
  - create_word_doc
  - file_write
---

# document_creation

## Description

Proficiency in creating formatted Word documents with text content and embedded images

## Procedural Instructions

To create a Word document: 1) Determine the output path and filename — expand ~ for home directory. 2) Call create_word_doc with output_path, title (heading), body_text, and optionally image_path. 3) If you need to embed an image, ensure image_path exists before calling create_word_doc. 4) For plain text output without Word formatting, use file_write with a .txt extension instead. 5) Always confirm the returned output_path exists after creation.

## Required Tools

- create_word_doc
- file_write

## Evidence Scrolls

_No evidence scrolls._
