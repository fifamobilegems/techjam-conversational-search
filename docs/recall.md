# BM25 recall gate — 2026-08-30T16:58:30.974117+00:00

`limit=all` · `select=stratified` · catalog `data/catalog.jsonl`

The **raw** columns feed the customer's own words to BM25; the **pipeline** columns feed `build_search_context()`. A large gap — and a high **empty%** — is the extractor discarding the query.

| dataset | simulator | phase | n | raw@100 | pipe@100 | raw@500 | pipe@500 | empty% |
|---|---|---|--:|--:|--:|--:|--:|--:|
| public200 | official | turn1 | 200 | 0.520 | 0.560 | 0.855 | 0.905 | 0.0% |
| public200 | realistic | turn1 | 200 | 0.715 | 0.345 | 0.945 | 0.495 | 9.5% |
| synth800 | official | turn1 | 800 | 0.588 | 0.614 | 0.853 | 0.885 | 0.0% |
| synth800 | realistic | turn1 | 800 | 0.637 | 0.334 | 0.826 | 0.522 | 14.6% |
| esci1000 | official | turn1 | 1000 | 0.573 | 0.616 | 0.833 | 0.859 | 0.0% |
| esci1000 | realistic | turn1 | 1000 | 0.624 | 0.343 | 0.828 | 0.545 | 13.1% |
| esci1000 | esci | turn1 | 1000 | 0.607 | 0.008 | 0.834 | 0.032 | 86.7% |

## Gold-only raw recall (ESCI human-labelled rows)

| dataset | simulator | phase | raw@100 gold | raw@500 gold |
|---|---|---|--:|--:|
| esci1000 | official | turn1 | 0.637 | 0.889 |
| esci1000 | realistic | turn1 | 0.744 | 0.906 |
| esci1000 | esci | turn1 | 0.658 | 0.799 |

