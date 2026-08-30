# Competition Data

## `public_set.jsonl`

Contains 200 labeled development sessions: 80 Buying, 80 Browsing, 30 Intent Override, and 10 Boundary sessions.

Each session contains a safe aggregate `user_profile` and public labels for local development. Direct user identifiers, timestamps, free-text reviews, raw purchase history, hidden intent cards, and simulator-policy internals are not shipped in this participant file.

## `catalog.jsonl`

Download `catalog.jsonl.gz` from the GitHub Release and decompress it as `catalog.jsonl` in this directory. Expected row count: 50,000.

## `embeddings/`

Build products written by `python -m scripts.build_embeddings`, one directory
per encoder. Gitignored and reproducible from the frozen catalog -- never
committed, never edited by hand. See `docs/rag_foundation.md`.

Never place API keys, private evaluation data, or participant outputs in this directory.
