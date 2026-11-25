# Decision: production model for article analysis

> **Superseded 2026-11-24:** the owner compared Qwen 3.5 9B (local) with Gemma 4 31B
> (Gemini API, free tier) on the same articles and chose Gemma 4 31B. See architecture
> section 5.5.

**Date:** 2026-11-23 · **Decision:** `qwen/qwen3.5-9b` (LM Studio, MLX 4-bit) · **Status:** superseded 2026-11-24 (architecture 5.5)

## Context

The owner requires **one** model in production, chosen by measured accuracy on our own
articles (docs/analysis.md section 2). Both candidates were already installed in LM
Studio on the owner's Mac (Apple M2 Pro, 16 GB).

## Method

- **Articles:** the same 10 real articles, chosen by rule to cover Politics, Economy,
  International, Society and Sports, a regional story, a court case with allegations,
  relative dates, a geography-coverage case and a long article.
- **Prompt and schema:** both at version 3, with `reasoning_effort: none`, temperature
  0.1 and JSON-schema output.
- **Checks in code:** every evidence quote found in its cited paragraph; every person and
  place name found in the article; event-date quotes containing date words. Then a
  manual review of report types, dates, places and roles.
- **Practical note:** the two models cannot be loaded together on 16 GB. LM Studio
  refused to load the second for lack of resources, so they were run one after the other.

## Results

| | Qwen3.5-9B | Gemma-4-E4B |
| --- | --- | --- |
| Valid structured output | **10/10** | 9/10 |
| Evidence quotes verified | **99%** (198/200) | 95% (121/127) |
| **Fabricated quotes** | **0** | **6** |
| Code-detected issues (names not in article, undated dates) | **5** | 12 |
| Claims extracted | **85** | 28 |
| Main events dated | **9/10** | 0/9 |
| Key actors labelled correctly (manual review) | mostly | often marked "mentioned" in their own story |
| Output errors | none | the role word "mentioned" emitted as a place name |
| Average time per article | 90 s | **57 s** |

## Decision

**Qwen3.5-9B.** The selection rule puts fewest hallucinations first, then completeness,
with speed only as a tie-breaker. Gemma is 37% faster but fabricates quotes, extracts a
third as many claims, never dates the main event, and misclassifies actors.

## Consequences

- `LLM_MODEL` defaults to `qwen/qwen3.5-9b`. No other model is referenced by the code.
  Removing Gemma's files from LM Studio is the owner's choice.
- **Known weaknesses to address in prompts:** actor involvement (central vs mentioned)
  and place roles. Event-date evidence and renamed names are caught by code checks.
- **Production load settings:** LM Studio loaded Qwen with an 8,192-token context. That
  fits typical articles, but the longest stored article (~44k characters, ~11k tokens)
  needs a larger context, set when the pipeline is built (stage 9.2).
- **Re-evaluate** when a newer model or a new prompt version is considered, using the
  same 10 articles and the `news lab` command.
