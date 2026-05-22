#!/usr/bin/env python3
"""search_emails — Search for emails in the inbox based on query criteria and return matching thread information.

Parameters (via run() kwargs):
  query (str, required): Search query string (e.g., 'from:rameswaran subject:NYSE').
  max_results (int, optional): Maximum number of emails to return. Default 5.

Returns (dict):
  success (bool): True if the operation succeeded.
  emails (list[dict]): List of objects containing 'id', 'threadId', 'subject', and 'snippet'.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

TOOL_META = {
    "name": "search_emails",
    "tool_type": "api_call",
    "dependencies": ["google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"],
}


def run(query: str, max_results: int = 5) -> dict:
    """Search Gmail inbox and return message details."""
    if not query:
        return {"success": False, "emails": [], "error": "query is required"}

    try:
        # Note: Assumes credentials are managed by the environment/agent context
        # In a production scenario, use google.auth.default() or similar
        service = build("gmail", "v1", cache_discovery=False)
        
        results = service.users().messages().list(
            userId="me", 
            q=query, 
            maxResults=max_results
        ).execute()
        
        messages = results.get("messages", [])
        email_list = []
        
        for msg in messages:
            msg_data = service.users().messages().get(
                userId="me", 
                id=msg["id"], 
                format="metadata", 
                metadataHeaders=["Subject"]
            ).execute()
            
            headers = msg_data.get("payload", {}).get("headers", [])
            subject = next((h["value"] for h in headers if h["name"] == "Subject"), "No Subject")
            
            email_list.append({
                "id": msg_data["id"],
                "threadId": msg_data["threadId"],
                "subject": subject,
                "snippet": msg_data.get("snippet", "")
            })
            
        return {"success": True, "emails": email_list, "error": None}
        
    except HttpError as err:
        return {"success": False, "emails": [], "error": f"Gmail API error: {str(err)}"}
    except Exception as exc:
        return {"success": False, "emails": [], "error": str(exc)}