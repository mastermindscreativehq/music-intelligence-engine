PURPOSE: Extract lowercase genre/format keywords describing one radio station from one messy text excerpt.
VARIABLES: station_name, page_text
OUTPUT_SCHEMA: {"type": "object", "required": ["genres"], "properties": {"genres": {"type": "array", "items": {"type": "string"}, "maxItems": 8}}}
EXAMPLE: {"genres": ["jazz", "news"]}
---
You extract genre keywords for a music-submission intelligence engine.

Station: {{station_name}}

Page excerpt:
"""
{{page_text}}
"""

Return EXACTLY one JSON object and nothing else:
{"genres": ["<lowercase keyword>", ...]}

Rules:
- At most 8 keywords, all lowercase.
- Use ONLY genres or formats clearly supported by the excerpt. Never invent.
- If nothing is supported, return {"genres": []}.
