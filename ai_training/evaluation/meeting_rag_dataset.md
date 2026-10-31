# Meeting RAG evaluation data

`--context-weight` selects cosine weight (0–1, default 0.8); keyword weight is its
complement. The evaluator sends the same value to queries and evaluation-state
and records the actual weights in reports.

Keep datasets and reports outside Git. Use synthetic or explicitly approved
meetings; keep a versioned held-out split independent of threshold/prompt tuning.
The JSON dataset has `schema_version: 1`, `consent: true`, `provenance`,
`split: "held_out"`, `meeting_revisions` mapping IDs to transcript SHA-256,
optional `device` and `provider_versions`, and nonempty `cases`.

Each case needs `id`, `question`, `category`, `expected_meeting_ids` and
`expected_source_ids` containing annotated stable chunk IDs. Include categories
`vietnamese`, `mixed_language`, `unanswerable`, `contradictory`, and `injection`.
Empty expected lists are allowed for unanswerable cases. Preserve annotations
and revisions; regenerate them explicitly after transcript/chunk changes.

Run from the repository with API/worker ready:

```bash
uv run --env-file .env python ai_training/evaluation/evaluate_meeting_rag.py \
  --dataset /private/held-out.json --chat-provider-id local \
  --chat-model-id qwen3 --embedding-profile-id local-embed-v1 \
  --email evaluator@example.com \
  --output ai_training/results/rag-held-out.json
```

The account must have access only to the approved evaluation library. The CLI
prompts for its password without echo, keeps cookies in memory, sends CSRF on
writes and logs out on completion (including errors). Do not pass passwords in
arguments or store them in reports. The API remains loopback-only.

Schema-v2 reports add meeting MRR/nDCG/precision, categories, error counts and
answer/abstention rates. These retrieval metrics use context-selected meeting
IDs, not the raw candidate list; they do not measure chunk ranking directly.
`--compare-report /private/baseline.json` attaches a comparison on the exact
dataset/case sequence and compatible server state. Failed cases score zero on
the fixed corpus. Only chunk-profile ID/revision and retrieval version may
differ; model, threshold, context budget, corpus and prompt changes are rejected.
Chunk source-ID annotations change with chunking, so use the segment-labeled
retrieval benchmark below for cross-chunk comparisons.

### Synthetic retrieval baseline

`benchmark_meeting_retrieval.py` generates versioned `short-v1` or `long-v1`
development fixtures (8 meetings, 26 queries). The long variant adds meeting-like
discussion to exercise multiple chunks. Run the four combinations of
`utf8-v1`/`turns-v2` and `hybrid-v1`/`lexical-v2` on the same corpus, embedding
model and digest, K and context budget. `TEST_DATABASE_URL` must point to a
disposable PostgreSQL database with pgvector; each run owns and drops a random
schema. Use local Ollama only. Outputs record corpus hash, model digest, database
versions, chunk count, meeting Recall/MRR/nDCG, segment recall and latency.

Segment recall counts whether any part of an annotated original segment reaches
context; it does not establish that the relevant fact survives a boundary.
The single unanswerable fixture is a diagnostic, not a calibrated rejection
benchmark. Timing includes query embedding and retrieval, excludes answer
generation, and is not a load test. Candidate order/cache effects require
repeated runs before drawing latency conclusions. Fixtures are development
data, never the held-out split for selecting production settings.

### Relevance filter comparison

The runtime now ranks with 0.8 cosine/0.2 keyword and filters evidence using the
selected chat model before the final K meeting selection. Evaluation state records
the ranking weights and fixed relevance prompt version/hash. The answer field is
plain text; citation objects remain authoritative. `selected_meetings` now means
cited meetings, so answer-evaluator meeting metrics measure citation selection,
not all context candidates. Use the candidate/segment benchmarks for retrieval.
Reports from different filter prompts or weights are not interchangeable.

`evaluate_source_filter.py` compares the frozen 0.7/0.3/no-filter baseline against
0.8/0.2 plus filtering on a fixed candidate set. It intentionally does not measure
the complete hybrid search pipeline or answer generation. Use only approved
development text; an invented distractor must be labeled synthetic, never passed
off as the original stored transcript. The fixture JSON needs `consent: true`,
`split: "development"`, `provenance`, `sources` and `cases`. Each source has `id`,
`meeting_id`, `title`, `text`, `start`, `end` and optional `speaker`; each case has
`id`, `question`, `expected_source_ids`. Keep fixtures and reports outside Git.

```bash
PYTHONPATH=apps/api uv run python ai_training/evaluation/evaluate_source_filter.py \
  --dataset /private/approved-development-fixture.json \
  --embedding-model YOUR_LOCAL_EMBEDDING_MODEL --chat-model YOUR_LOCAL_CHAT_MODEL \
  --output ai_training/results/source-filter-comparison.json
```

The harness permits loopback Ollama only, records model digests and evidence
retention/distractor rejection/latency. Invalid filter output receives no quality
credit. The filter uses short request-local labels internally and maps validated
labels back to unchanged source IDs. It is an LLM relevance classifier, not a
calibrated probability or entailment check. Small models may retain unrelated
passages or omit evidence. Keep failed runs when tuning and use a separate
held-out set before claiming quality improvements.

Recall@K uses unique retrieved meeting IDs; citation precision/recall use unique
source IDs. Zero denominators return zero; do not interpret that as answer
quality on unanswerable cases. Score factual support and usefulness separately
with human claim-level ratings: `--human-ratings /private/ratings.json`, a
mapping from case ID to output-hash-bound claim ratings (format below).
An invented claim citing a valid source scores source membership correctly but
must score unsupported in human review. Missing human scores stay null.
The report omits question/answer content, records latency and settings, and
marks unavailable provider versions, device, or cost explicitly. A synthetic
smoke run is not a quality result. No baseline quality measurement is shipped.

The evaluator checks `/api/chat/evaluation-state` before and after each query.
Use a dedicated evaluation library: its completed meeting IDs and revisions must
exactly match the manifest. Added/changed meetings or changing server metadata
abort the run. Actual server prompt hash, profile definition and threshold are
recorded; optional dataset `server_state_hash` pins a previous configuration.
Local shell model settings are not treated as evidence of the server's state.

Capture private generated answers with `--answers-output
ai_training/results/answers.json`. Reports contain `output_hash` per case;
ratings must be keyed by case ID as
`{"output_hash":"matching SHA-256","claims":[{"supported":false,"usefulness":1}]}`.
Attach ratings to the original report without calling the model again:

```bash
uv run python ai_training/evaluation/evaluate_meeting_rag.py \
  --score-report ai_training/results/rag-held-out.json \
  --human-ratings /private/ratings.json \
  --output ai_training/results/rag-rated.json
```

A mismatched output hash rejects the rating. Reports and optional answer captures
are private files under ignored results; keep them out of source control.
