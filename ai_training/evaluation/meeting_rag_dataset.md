# Meeting RAG evaluation data

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
  --output ai_training/results/rag-held-out.json
```

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
