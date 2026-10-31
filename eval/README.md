# Evaluation set

`questions.yaml` is the yardstick for every change to the assistant. Each
number you put on your CV or in the project README comes from running the
system against this file.

## Workflow

1. **Write questions first.** Aim for the target mix in `targets` before
   building the router or tuning retrieval. Questions written after you have
   seen the system's failures end up testing what it already does well.
2. **Fill gold answers from the source, not from the system.**
   - SQL questions: run `python eval/validate_eval_set.py --run-sql --show`,
     check the rows make sense, then write `expected_answer` and
     `must_include` from them.
   - Docs questions: read the official TfNSW page, ingest it, then record its
     `doc_id` in `gold_doc_ids`. Note in `notes` the date you checked it;
     fares and policies change.
3. **Keep the test split honest.** About 20% of questions are `split: test`.
   Tune on `dev` only. Run `test` when you want a number to report, and do
   not change prompts because of an individual test failure.
4. **Re-run the validator before every commit.** It exits non-zero on
   structural errors, so it can sit in CI.

## What makes a good question

- **Phrase it like a rider would**: lowercase, abbreviations ("stn",
  "the 901"), a typo now and then. Router and stop resolution must handle it.
- **Name the trap in `tags` and `notes`.** Most of the value is in edge cases:
  station versus stand (`parent_station`), times after midnight,
  public-holiday `calendar_dates`, routes with the same number in different
  regions, and policy pages that use different words from the question.
- **Include questions the system should not answer.** `behaviour: clarify`
  for ambiguous ones ("next bus from Hurstville"), `behaviour: refuse` for
  out-of-scope or predictive ones. A system that never declines will score
  badly here, which is the point.
- **One fact per `must_include` item, short.** "05:12" or "free for children
  under 4" rather than a sentence. These are matched against the answer text
  for a cheap automatic check alongside LLM-judged faithfulness.

## Metrics this file will feed (week 2 onwards)

| Stage | Metric | Uses |
|---|---|---|
| Router | accuracy, confusion matrix | `route` |
| Text-to-SQL | execution accuracy: generated result matches gold result | `gold_sql` |
| Retrieval | Recall@k, MRR | `gold_doc_ids` |
| Generation | faithfulness, `must_include` hit rate | `expected_answer`, `must_include` |
| Behaviour | correct clarify/refuse rate | `behaviour` |
