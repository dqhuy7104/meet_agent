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
mapping from case ID to `[{"supported": false, "usefulness": 1}]`.
An invented claim citing a valid source scores source membership correctly but
must score unsupported in human review. Missing human scores stay null.
The report omits question/answer content, records latency and settings, and
marks unavailable provider versions, device, or cost explicitly. A synthetic
smoke run is not a quality result. No baseline quality measurement is shipped.
