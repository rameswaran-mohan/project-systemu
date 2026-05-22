---
name: send_email
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - smtplib
---

# send_email

## Description

Send an email with an optional file attachment using SMTP

## Parameters

- recipient (string, optional): Email address of the recipient
- subject (string, optional): Subject line of the email
- body (string, optional): Text content of the email
- attachment_path (string, default: ): Path to the file to attach (optional)

## Returns

- success (boolean)
- error (string)

## Implementation Notes

Use smtplib and email.mime. Use MIMEMultipart to construct the message. If attachment_path is provided, use MIMEBase to encode the file and add it as an attachment. Use SMTP_SSL for secure connection. Catch smtplib.SMTPException and return error.
