# Milestone 9: AI analysis (proposal)

Status: **proposal, awaiting the owner's approval.** Nothing here is implemented yet.

## Goal

Turn the collected articles (metadata plus private full text, see architecture §5.4) into
structured, queryable knowledge: what each article is about, who and what it mentions,
and how topics and stories evolve over time. The constraints from the architecture still
hold: **$0, local, private.**

## Constraints

| Constraint | Consequence |
| --- | --- |
| $0, no paid APIs, no free-tier dependency | A local open-weight model on the owner's Mac |
| Article text is private | It never leaves the machine; analysis results are stored locally |
| Mac: Apple M2 Pro, 16 GB RAM, ~70 GB free (checked 2026-11-23) | Comfortable: ~7–9 B parameter instruction models at 4-bit (~5 GB). Possible but tight: ~12–14 B. |
| The Mac is not always on | Analysis is a resumable command run by hand, like ingestion |

## Runtime: an OpenAI-compatible local server

Both mainstream local runtimes expose the same HTTP API (`/v1/chat/completions`,
`/v1/embeddings`), so the code targets that API and works with either:

| Runtime | Status on this Mac | Notes |
| --- | --- | --- |
| **LM Studio** | **Installed** | GUI to download and try models; built-in server (default `http://localhost:1234/v1`); `lms` CLI |
| Ollama | Not installed | CLI-first; server at `http://localhost:11434/v1`; models pulled by name |

**Recommendation: start with LM Studio, since it is already installed.** Settings:
`LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`. Switching runtime is a config change, not a
code change. We use the existing `httpx` client, so no new SDK dependency.

**Model choice is made by measurement, not by name.** The owner's LM Studio already has
two suitable candidates (checked 2026-11-23):

| Model | Size | Context | Format |
| --- | --- | --- | --- |
| Qwen3.5-9B | 5.6 GB | 262k | MLX 4-bit (Apple-silicon optimized) |
| gemma-4-E4B-it | 6.4 GB | 128k | MLX 4-bit |

- Both go through the evaluation below; the better one becomes the default, recorded
  with the date and the scores.
- **Still needed:** a small text-embedding model (download in LM Studio) for story
  grouping, step 4.
- **The LM Studio server must be running** (Developer tab, then Start server, or
  `~/.lmstudio/bin/lms server start`). It was not running on 2026-11-23.

## What gets extracted (per article, one call, validated JSON)

| Field | Example | Used for |
| --- | --- | --- |
| `topics` | `["migration", "Morocco-Spain relations"]` (from a controlled list plus free tags) | Trends, filtering; replaces the vague "News"/"General" categories |
| `entities` | people, organizations, places, each with a type | "Who is in the news", cross-referencing |
| `event_type` | `policy_announcement`, `court_ruling`, `sports_result`, … | Event tracking |
| `tone` | `neutral` / `critical` / `positive` | Coverage analysis |
| `key_facts` | short, factual statements in the model's own words | Research notes (private) |
| `summary` | 2–3 sentences | Private reading aid |

**Rules:**
- **Output is validated** against a Pydantic schema; invalid output is retried, then
  recorded as failed. Nothing malformed is stored.
- **Results are versioned:** `analyses(article_id, analyzer, analyzer_version, model, result
  jsonb, created_at)` with one row per (article, analyzer, version). Changing the prompt
  or model means a new version, and re-analysis is explicit.
- **Rights:** `summary` and `key_facts` are derived from private text and stay private (the
  API never serves them). `topics`, `entities`, `event_type` and `tone` are facts about
  the article and may be served.

## Cross-article analysis (no extra AI calls)

- **Trends:** SQL over `analyses`, for topic and entity counts per day or week.
- **Story grouping:** embed each article's title and summary, then group articles whose
  vectors are close within a time window: "the same story over several days". At this
  scale (thousands of articles) cosine similarity in Python is enough. pgvector would
  mean changing the database image, so it waits until it is needed.
- **Normalization:** author and agency names (e.g. "With AFP" and "with AFP") and entity
  aliases.

## Running it

```bash
uv run news analyze -v              # analyse articles not yet analysed with the current version
uv run news analyze --limit 50      # bounded run (e.g. to try a model)
```

- **Resumable and idempotent,** like `news ingest`: stop at any time, and the next run
  continues.
- **Expected speed (to be measured):** an 8 B model on an M2 Pro takes very roughly
  5–15 s per article, so the existing ~1,800 articles take a few hours, best run
  overnight in batches. New articles (~20 a day) take a few minutes.
- **Readiness check:** it fails fast with a clear message if the local model server is
  not running.

## Evaluation (before trusting the output)

- **Gold set:** 30 articles hand-checked by the owner, covering topics, entities and
  event type.
- **Measures:** entity precision and recall, topic agreement, and the rate of invalid
  JSON, per candidate model.
- **Tests:** CI and the test suite never call a real model. They use a fake server
  that returns recorded responses, plus synthetic articles.

## Build order

1. `analyses` table, settings, and an LLM client with schema-validated output against a
   fake server.
2. `news analyze` (resumable, bounded, readiness check) plus the first analyzer
   (topics, entities, event type, tone, facts, summary).
3. Model trial on 50 articles with 2–3 candidates; gold-set evaluation; pick and record.
4. Embeddings and story grouping.
5. API: `/api/v1/articles/{id}/analysis` (non-private fields), `/api/v1/topics`,
   `/api/v1/trends`.

## Decisions for the owner

1. Runtime: **LM Studio** (installed) or Ollama?
2. Are the extracted fields above the right ones for your research? What questions do
   you want answered?
3. Will you hand-check ~30 articles to build the gold set? It is what makes the output
   trustworthy.
