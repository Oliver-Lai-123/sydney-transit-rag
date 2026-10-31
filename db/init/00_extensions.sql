-- Runs first on a fresh database volume.
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector: embeddings for the docs path
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- trigram similarity: fuzzy stop-name resolution

CREATE SCHEMA IF NOT EXISTS gtfs;  -- structured timetable data (text-to-SQL path)
CREATE SCHEMA IF NOT EXISTS docs;  -- TfNSW policy documents and chunks (RAG path)
