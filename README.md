# Sydney Transit Assistant

A question-answering assistant for Sydney public transport that routes each
question to the right tool:

- **Timetable questions** ("first T4 from Hurstville on a weekday?") go to
  **text-to-SQL** over the TfNSW GTFS feed in Postgres.
- **Policy questions** ("can I take my bike on the train?") go to **RAG** over
  official TfNSW pages (hybrid keyword and vector search, then a reranker).
- **Mixed questions** use both, and every answer cites its SQL or its sources.

The project is measured against a hand-labelled evaluation set from day one
(`eval/`).

> Status: week 1 scaffold. Database, GTFS loader and evaluation template are
> in place; ingestion, router and API come next (see Roadmap).

## Architecture

```
question ──> router ──┬──> text-to-SQL ──> Postgres (gtfs schema, read-only role) ──┐
                      │                                                             ├──> answer with citations
                      └──> hybrid retrieval (tsvector + pgvector) ──> reranker ─────┘
                                       docs schema
```

Design decisions:

- **One Postgres for everything.** GTFS tables, document chunks and embeddings
  (pgvector) live in one database, so there is one service to run and back up.
- **Generated SQL runs as `transit_reader`.** That role can only `SELECT` from
  the `gtfs` schema, every transaction is read-only, and statements time out
  after 5 seconds. Even if the SQL validator misses something, the database
  refuses it.
- **Live service alerts are not embedded.** They go stale within hours, so the
  app will fetch them from the TfNSW API at question time. Only stable policy
  pages are chunked and embedded.
- **Schema comments are prompt material.** The `COMMENT ON` text in
  `db/init/01_gtfs_schema.sql` explains GTFS quirks (stations versus stands,
  times after midnight) and will be fed to the text-to-SQL prompt.

## Quick start

Requirements: Docker Desktop, Python 3.11+.

```bash
# 1. Configure
cp .env.example .env          # then set both passwords; add TFNSW_API_KEY later

# 2. Start Postgres + pgvector (init scripts run automatically on first start)
docker compose up -d
docker compose ps             # wait for "healthy"

# 3. Python environment
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 4. Load the small synthetic fixture to check everything works
python scripts/load_gtfs.py --dir tests/fixtures/mini_gtfs

# 5. Validate the eval set and run its gold SQL as the read-only role
python eval/validate_eval_set.py --run-sql --show

# 6. Tests (the integration test runs when TEST_DATABASE_URL is set)
pytest -q
```

### Loading the real TfNSW timetable

1. Register at the [TfNSW Open Data Hub](https://opendata.transport.nsw.gov.au),
   create an application and API key, and give it access to
   **Public Transport - Timetables - Complete - GTFS**.
2. Put the key in `.env` as `TFNSW_API_KEY`.
3. Run:

```bash
python scripts/load_gtfs.py --download
```

The full feed is large and `stop_times` runs to many millions of rows, so the
first load takes a while. The loader:

- runs the whole load in one transaction, so a failed load leaves the previous
  timetable untouched
- maps columns by header name and reports columns it ignores or cannot find
- converts GTFS times past `24:00:00` to seconds (`departure_secs`) for correct
  sorting
- builds indexes after loading, including a trigram index for fuzzy stop names
- runs integrity checks and records the load in `gtfs.feed_load`, so answers
  can say how fresh the timetable is

Skip `shapes.txt` unless you need route geometry (`--with-shapes` adds it).

## Development workflow

`main` is protected: it only changes through pull requests that pass CI.

1. Branch from an up-to-date `main`: `git switch main`, `git pull`, then
   `git switch -c feat/ingest-docs` (prefixes: `feat/`, `fix/`, `docs/`,
   `chore/`, `eval/`).
2. Commit in small steps with messages that say what changed, e.g.
   `Add heading-aware chunker for TfNSW pages`.
3. Push and open a pull request: `git push -u origin feat/ingest-docs`, then
   `gh pr create --fill` (or use the link Git prints).
4. CI runs on the pull request (`.github/workflows/ci.yml`):
   - **Lint**: `ruff check`
   - **Test**: starts the database with the same `docker compose` setup used
     locally, runs `pytest` including the integration test, loads the fixture
     feed, and runs every eval gold query as the read-only role
5. When CI is green, squash-merge and delete the branch.

Run the same checks locally before pushing:

```bash
ruff check .
pytest -q
python eval/validate_eval_set.py --run-sql
```

Dependabot (`.github/dependabot.yml`) opens monthly pull requests for
dependency and GitHub Actions updates; merge them when CI passes.

Deployment (CD) is added once there is a service to deploy (see Roadmap).

## Repository layout

```
.github/
  workflows/ci.yml          lint and test on every push and pull request
  dependabot.yml            monthly dependency update pull requests
  pull_request_template.md
pyproject.toml              project metadata, ruff and pytest settings
docker-compose.yml          Postgres 16 + pgvector
.env.example                settings and connection strings
db/
  init/                     run once by the container on first start
    00_extensions.sql       pgvector, pg_trgm, schemas
    01_gtfs_schema.sql      GTFS tables with descriptive comments
    02_docs_schema.sql      documents and chunks (embedding + tsvector)
    03_readonly_role.sh     transit_reader role for generated SQL
  post_load_indexes.sql     indexes built after each GTFS load
scripts/
  load_gtfs.py              download and load the GTFS feed
eval/
  questions.yaml            evaluation set (start here)
  validate_eval_set.py      structure, coverage and gold-SQL checks
  README.md                 labelling guide
tests/
  fixtures/mini_gtfs/       tiny synthetic feed (not real timetable data)
  test_load_gtfs.py
```

## Notes

- **Port 5433 and 127.0.0.1.** The database listens on 5433 so it does not
  clash with a PostgreSQL installed directly on your computer, and the
  connection URLs use `127.0.0.1` because on Windows `localhost` can resolve
  to IPv6 and reach a different process. If you see "password authentication
  failed" with the right password, check what else is listening:
  `netstat -ano | findstr :543` (Windows) or `lsof -i :5433` (macOS/Linux).
- **Resetting the database.** The init scripts only run on an empty volume.
  After changing them (for example the embedding dimension), run
  `docker compose down -v` and `docker compose up -d`. This deletes all loaded
  data.
- **Embedding dimension.** `docs.chunks.embedding` is `vector(1024)`. Change it
  in `02_docs_schema.sql` to match your embedding model before first start.
- **Fixture data is synthetic.** `tests/fixtures/mini_gtfs` uses real place
  names but invented routes, times and IDs.

## Roadmap

- [x] Postgres + pgvector, GTFS schema, read-only role
- [x] GTFS loader with integrity checks
- [x] Evaluation set template and validator
- [x] CI: lint, tests, eval validation on every pull request
- [ ] Write the full evaluation set (60 questions) and fill gold answers
- [ ] Ingest TfNSW policy pages: fetch, clean, chunk by heading, embed
- [ ] Baseline RAG and its scores
- [ ] Add historical on-time performance tables from the bus on-time analysis project
- [ ] Text-to-SQL with schema comments, examples, validator; execution accuracy
- [ ] Router and its accuracy
- [ ] Hybrid search + reranker with before/after Recall@5
- [ ] Live service alerts from the TfNSW API
- [ ] FastAPI service, simple chat UI, results table in this README
- [ ] CD: build the service image on merge to `main` and deploy it

## Data and licence

Timetable data: Transport for NSW Open Data, licensed under Creative Commons
Attribution. Attribute TfNSW wherever the data or answers derived from it are
shown.
