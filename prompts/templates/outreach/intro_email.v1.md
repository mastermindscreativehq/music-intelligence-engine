PURPOSE: Draft one recipient-specific music outreach email (subject + body) from the artist's own song story, release metadata, recipient context, and the release's saved listening URL. Never invent facts.
VARIABLES: song_title, artist_name, recipient_name, recipient_role, organization, song_story, listening_url, objective, style, instructions
OUTPUT_SCHEMA: {"type": "object", "required": ["subject", "message"], "properties": {"subject": {"type": "string"}, "message": {"type": "string"}}}
EXAMPLE: {"subject": "New single from Ada Vessel for your playlist", "message": "Hi Jordan,\n\nI'm Ada Vessel. My new single \"Neon Sky\" grew out of late drives through the city...\n\nYou can listen here: https://example.com/neon-sky\n\nBest,\nAda"}
---
You write ONE outreach email for a music submission. Return EXACTLY one
JSON object and nothing else:
{"subject": "<email subject>", "message": "<full plain-text email body>"}

Use ONLY the facts provided below. Never invent a connection, a prior
conversation, a preference, an award, a song's meaning, or that the
recipient has listened. If a field is empty, do not mention it.

Release title: {{song_title}}
Artist name: {{artist_name}}

Recipient name: {{recipient_name}}
Recipient role: {{recipient_role}}
Organization: {{organization}}

The artist's own account of the song (source material; paraphrase, do NOT
dump verbatim, and do NOT add meaning it does not contain):
"""
{{song_story}}
"""

Saved official listening URL (include it verbatim; never invent one):
{{listening_url}}

Campaign objective: {{objective}}
Requested writing style: {{style}}
Operator personalization notes (follow these; they take priority):
{{instructions}}

Write a complete email: a specific subject line; a greeting using the
recipient's real name when given; a short opening that identifies the artist
and the release; a natural, specific paragraph drawn from the song story
(omit if no story was provided); a clear reason this release fits THIS
recipient's role and organization (only if supported by the facts above); a
single call to action; the listening link; and a brief sign-off with the
artist's name. Keep it concise and human. Do not use a generic "I hope this
email finds you well" opener. Do not exaggerate. Do not claim the recipient
has listened.
