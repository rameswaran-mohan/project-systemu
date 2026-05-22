---
name: image_processing
description: Ability to capture, resize, and manipulate image files
category: productivity
proficiency_level: intermediate
required_tools:
  - take_screenshot
  - web_screenshot
  - image_resize
---

# image_processing

## Description

Ability to capture, resize, and manipulate image files

## Procedural Instructions

For image capture: use web_screenshot for web content (headless, no display needed), use take_screenshot for capturing the physical screen or a region. For resizing: use image_resize — provide either width or height and set the other to 0 to maintain aspect ratio. Common output formats: PNG for lossless, JPEG for photos (use quality=85). Always verify the returned width and height after resizing to confirm the operation succeeded.

## Required Tools

- take_screenshot
- web_screenshot
- image_resize

## Evidence Scrolls

_No evidence scrolls._
