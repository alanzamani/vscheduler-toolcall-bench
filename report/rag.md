# RAG track: retrieval over the vScheduler docs (20261110T082309Z)

26 answerable questions and 6 the docs do not cover; embeddings from `nomic-embed-text` (local, Ollama); exact search in pgvector. Chunks per strategy: fixed 38, heading 51.

## Retrieval (answerable questions)

| strategy   | mode    | recall@1 (95% CI)   | recall@3 (95% CI)   | recall@5 (95% CI)   |   MRR@10 | recall@3 direct   | recall@3 paraphrase   |
|:-----------|:--------|:--------------------|:--------------------|:--------------------|---------:|:------------------|:----------------------|
| fixed      | vector  | 65% (46–81)         | 77% (58–89)         | 81% (62–91)         |     0.74 | 81%               | 70%                   |
| fixed      | keyword | 35% (19–54)         | 58% (39–74)         | 77% (58–89)         |     0.5  | 50%               | 70%                   |
| fixed      | hybrid  | 54% (35–71)         | 69% (50–83)         | 81% (62–91)         |     0.66 | 62%               | 80%                   |
| heading    | vector  | 69% (50–83)         | 73% (54–86)         | 88% (71–96)         |     0.75 | 75%               | 70%                   |
| heading    | keyword | 38% (22–57)         | 62% (43–78)         | 69% (50–83)         |     0.52 | 50%               | 80%                   |
| heading    | hybrid  | 58% (39–74)         | 65% (46–81)         | 85% (66–94)         |     0.66 | 56%               | 80%                   |

Recall@k: the share of questions with a chunk containing the answer in the top k. MRR: mean of 1/rank of the first such chunk (0 if not in the top 10). Intervals are 95% Wilson intervals over questions.

## Abstention (vector top similarity, leave-one-out threshold)

| strategy   |   threshold (all questions) | abstained when not covered   | answered when covered   |   mean top similarity, covered |   mean top similarity, not covered |
|:-----------|----------------------------:|:-----------------------------|:------------------------|-------------------------------:|-----------------------------------:|
| fixed      |                       0.686 | 83% (44–97)                  | 65% (46–81)             |                          0.705 |                              0.647 |
| heading    |                       0.69  | 83% (44–97)                  | 73% (54–86)             |                          0.718 |                              0.654 |

## Misses (answer not in the top 3)

| strategy   | mode    | questions                                             |
|:-----------|:--------|:------------------------------------------------------|
| fixed      | hybrid  | q01, q02, q06, q12, q13, q18, q19, q23                |
| fixed      | keyword | q01, q02, q04, q06, q12, q13, q14, q18, q19, q23, q26 |
| fixed      | vector  | q02, q06, q08, q12, q13, q18                          |
| heading    | hybrid  | q01, q02, q06, q12, q13, q18, q19, q22, q23           |
| heading    | keyword | q01, q02, q06, q07, q09, q12, q13, q19, q22, q23      |
| heading    | vector  | q02, q08, q16, q18, q19, q22, q26                     |
