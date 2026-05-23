---
name: http-data-fetching
description: Proficiency in fetching data from REST APIs and web endpoints using HTTP
  GET requests
metadata:
  category: data
  proficiency_level: intermediate
  required_tools:
  - fetch_json
  - fetch_html
  - download_file
---

# http_data_fetching

## Description

Proficiency in fetching data from REST APIs and web endpoints using HTTP GET requests

## Procedural Instructions

To fetch API data: 1) Use fetch_json for endpoints returning JSON — pass query parameters via the params dict. 2) Use fetch_html when you need raw HTML to parse manually. 3) Use download_file for binary files (PDFs, images, archives) — set overwrite=True if re-downloading. 4) Check status_code in the response — 200 means success, 4xx means client error, 5xx means server error. 5) For APIs requiring authentication, pass the Authorization header in the headers dict.

## Required Tools

- fetch_json
- fetch_html
- download_file

## Evidence Scrolls

_No evidence scrolls._
