# tests/

Test foundation. Phase 1 uses the Python **standard library `unittest`** so the suite
runs with zero installed dependencies; pytest may be adopted in a later phase if the
suite outgrows it.

Run:

```powershell
python -m unittest discover -s tests -v
```

Current coverage (Phase 1): repository structure integrity, configuration hygiene,
and secret-safety checks on `.env.example`.

Future targets per phase: normalization, deduplication, contact classification,
confidence scoring, source handling, API behavior, crawler behavior, outreach
preparation and approval gating.
