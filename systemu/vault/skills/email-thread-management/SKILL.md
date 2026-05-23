---
name: email-thread-management
description: Proficiency in searching, reading, and replying to specific email threads
  using programmatic APIs
metadata:
  category: communication
  proficiency_level: intermediate
  required_tools:
  - search_emails
  - send_email
---

# email_thread_management

## Description

Proficiency in searching, reading, and replying to specific email threads using programmatic APIs

## Procedural Instructions

To manage email threads: 1) Use search_emails with specific query parameters (e.g., from, subject) to locate the target thread. 2) Extract the message ID or thread ID from the search results. 3) Use send_email to reply, ensuring the 'thread_id' or 'in_reply_to' header is set to maintain the conversation context. 4) Verify the email was sent successfully by checking the tool response.

## Required Tools

- search_emails
- send_email

## Evidence Scrolls

- scroll_22e82474
