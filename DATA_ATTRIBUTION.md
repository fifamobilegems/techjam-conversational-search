# Data Attribution and Use

This competition package is derived from **Amazon Reviews 2023**, published by McAuley Lab at UCSD.

- Project page: https://amazon-reviews-2023.github.io/
- Selected category: `Clothing_Shoes_and_Jewelry`
- Product join key: `parent_asin`
- Competition modality: text and structured product metadata only

The competition package does not contain images, videos, account credentials, private organizer labels, or the private holdout sessions.

Participants must follow the source dataset's applicable terms and use the data only for the competition, research, and other permitted purposes. The competition organizer does not claim ownership of the underlying Amazon review or product content.


## Amazon ESCI / Shopping Queries Dataset

Two artifacts derive from the public **Shopping Queries Dataset** (Reddy et al.,
2022, arXiv:2206.06588), released by Amazon Science under Apache 2.0:

- Project page: https://github.com/amazon-science/esci-data
- Locale used: `us`
- Fields used: `query`, `product_id`, `product_title`, `esci_label`, `split`

`data/esci_set_1000.jsonl` uses ESCI query text as the opening turn of the
`esci` customer simulator. `data/esci_gold_relevance.jsonl`, built by
`scripts/build_esci_gold.py`, additionally carries the human E/S/C/I relevance
labels for the 770 (query, product) pairs that join onto the frozen competition
catalog.

The join runs in one direction only: ESCI judgments are attached to catalog
products that already exist. Two paths are used — exact `product_id` to
`parent_asin`, and normalized `product_title` to a catalog title that is unique
within the catalog, which recovers ESCI child ASINs whose parent is in the
catalog. **No ESCI product enters the catalog, no ESCI metadata is written onto
a catalog row, and no identifier outside the frozen catalog reaches the agent.**
The labels are used offline to fit reranker weights (`scripts/train_reranker.py`);
the agent's runtime never reads them.

The raw ESCI parquet release (~1.2 GB) is cached under `data/esci_raw/` and is
gitignored. Nothing here reconstructs or approximates the organizer's unreleased
evaluation labels.
