---
name: search_emails
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - google-api-python-client
  - google-auth-httplib2
  - google-auth-oauthlib
---

# search_emails

## Description

Search for emails in the inbox based on query criteria and return matching thread information

## Parameters

- query (string, optional): Search query string (e.g., 'from:rameswaran subject:NYSE')
- max_results (integer, default: 5): Maximum number of emails to return

## Returns

- success (boolean)
- emails (array)
- error (string)

## Implementation Notes

Use the Gmail API (google-api-python-client). Authenticate via OAuth2. Call users().messages().list(q=query). Iterate through results to fetch message details using users().messages().get(). Return a list of objects containing 'id', 'threadId', 'subject', and 'snippet'. Catch HttpError and return error.
