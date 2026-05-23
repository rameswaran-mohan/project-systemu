---
name: system-command-execution
description: Proficiency in running shell commands and interpreting their output
metadata:
  category: system
  proficiency_level: intermediate
  required_tools:
  - run_command
---

# system_command_execution

## Description

Proficiency in running shell commands and interpreting their output

## Procedural Instructions

To run system commands: 1) Use run_command with the full command string — shell=True is used internally. 2) Always check return_code: 0 = success, non-zero = error. 3) Set a timeout appropriate to the command duration — default is 30 seconds. 4) Use cwd to set the working directory for relative paths in the command. 5) For potentially destructive commands (rm, del, format), always confirm the target path before executing.

## Required Tools

- run_command

## Evidence Scrolls

_No evidence scrolls._
