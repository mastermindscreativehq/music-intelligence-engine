# outreach/

Campaign & delivery layer (Phase 8–10).

**Owns:** campaign preparation, personalized message generation (drafts), submission/file
references, recipient selection, **human approval gate**, email delivery execution,
delivery status, bounce tracking, response tracking, suppression, opt-out handling.

**Hard rules:**

- No message is ever sent without explicit human approval of that recipient + message.
- "Email found" never implies "send". Confidence, verification, relevance, and
  suppression checks all gate sending.
- Rate limits and per-campaign caps are mandatory design elements.
- Every send is traceable to a Campaign, Message, approver, and timestamp.

No email provider credentials are configured in Phase 1; no emails are ever sent during
foundation. Not implemented yet.
