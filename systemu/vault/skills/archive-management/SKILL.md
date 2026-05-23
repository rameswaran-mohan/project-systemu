---
name: archive-management
description: Ability to compress and extract ZIP and TAR archives
metadata:
  category: file_ops
  proficiency_level: beginner
  required_tools:
  - compress_files
  - extract_archive
---

# archive_management

## Description

Ability to compress and extract ZIP and TAR archives

## Procedural Instructions

To compress: use compress_files with a list of file paths or an include_dir to zip an entire folder. Output path should end in .zip. To extract: use extract_archive with the archive path and target output_dir. Supports .zip, .tar, .tar.gz, .tgz formats automatically. Verify file_count in the return value matches the expected number of files.

## Required Tools

- compress_files
- extract_archive

## Evidence Scrolls

_No evidence scrolls._
