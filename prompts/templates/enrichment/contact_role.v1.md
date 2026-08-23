PURPOSE: Classify the contact role implied by one ambiguous station-contact snippet using the engine's fixed snake_case role vocabulary.
VARIABLES: context_text
OUTPUT_SCHEMA: {"type": "object", "required": ["role"], "properties": {"role": {"type": "string", "enum": ["advertising", "booking", "dj", "general", "host", "media", "music_director", "music_programmer", "music_submission", "other", "program_director", "programming", "station_manager", "unknown"]}, "reason": {"type": "string"}}}
EXAMPLE: {"role": "music_director", "reason": "snippet places 'Music Director' directly beside the address"}
---
You classify ONE contact for a music-submission intelligence engine.

Contact snippet (extracted facts only):
"""
{{context_text}}
"""

Return EXACTLY one JSON object and nothing else:
{"role": "<one allowed role>", "reason": "<short evidence-based reason>"}

Rules:
- Use ONLY evidence present in the snippet. Never guess.
- If the snippet carries no usable evidence, answer "unknown".
- "other" means a clear non-music role outside the vocabulary.
