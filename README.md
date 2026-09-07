# Music Intelligence Engine

Find a radio station → find a **real** way to send it your music → submit or
start an outreach campaign → move to the next station.

The engine discovers radio stations, records what the station itself publishes
(website, location, genres, submission pages, contact emails), and **never
invents** an email address, contact person, or submission route. If a route
isn't published, the app says so plainly.

---

## INSTALL

Python 3.11+ is required. Everything else is pinned in `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

No `npm install` is needed — the frontend is dependency-free (no build step).

## RUN

From this directory:

```powershell
npm run dev
```

which is exactly the same as:

```powershell
python -m backend.webapp --db data\music-intelligence.db
```

The server runs on `127.0.0.1` on port **8788**.

## OPEN

    http://127.0.0.1:8788/

From there:

1. **Stations** — search and open a radio station.
2. On a station page — see its website, location, genres, and the real
   submission/contact pages and emails the engine found.
3. **Send music** opens the station's own submission page; **Add to campaign**
   stages a verified route for outreach.
4. **My music** uploads the MP3 you want to pitch; **Outreach** drafts a
   message and hands it to your own email client (the app never sends email).

## Tests

```powershell
npm test
# or
python -m unittest discover -s tests -v
```

---

## Environment variables (all optional)

| Variable           | Purpose                                                          |
|--------------------|------------------------------------------------------------------|
| `MIE_DATABASE_PATH`| Path to a SQLite database (default used by `npm run dev`).      |
| `MIE_PG_DSN`       | PostgreSQL/Supabase DSN when running against Postgres.           |
| `MIE_WEB_HOST`     | Bind host (default `127.0.0.1`).                                |
| `MIE_WEB_PORT`     | Port (default `8788`).                                          |
| `MIE_API_BASE_URL` | Remote backend origin substituted at deploy time for the frontend (Vercel). |

See `.env.example`. Copy it to `.env` only if you need to override defaults;
the local app works with no `.env` at all.

## Discovery / enrichment CLI (optional)

```powershell
python -m discovery.radio.pipeline --request data\request.json --seed data\seeds.json
python -m discovery.radio.enrich --input <result.json> --output enriched.json
```

## About unrelated projects in this workspace

This is a **Python** project — its frontend is plain HTML/JS served by the
Python backend. If `npm run dev` once started a server on **port 3000**
("RecoverX API Server", using `C:\Users\user\database.json`), that came from a
**different project in another folder** (the Node app in
`C:\Users\user\recovery service file`). That app is not part of the Music
Intelligence Engine and is left untouched.

## Layout

| Directory    | Purpose                                                  |
|--------------|----------------------------------------------------------|
| `backend/`   | API + web server (`backend.webapp`)                      |
| `frontend/`  | Dependency-free browser UI (served same-origin)          |
| `database/`  | SQLite + PostgreSQL persistence and migrations           |
| `discovery/` | Radio station discovery pipeline                         |
| `enrichment/`| Contact / submission extraction and normalization        |
| `outreach/`  | Campaign records and email-provider abstraction          |
| `crawler/`   | Bounded, robots-respecting HTTP retrieval                |
| `tests/`     | Test suite (stdlib `unittest`)                           |