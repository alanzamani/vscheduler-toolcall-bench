# Trade-offs and limitations

Draft. Each section is a decision taken in v0, with what it costs.

## Dry run, first turn only

The benchmark scores the first tool calls a model attempts and never runs
them. That makes it cheap, repeatable and harmless. The cost is that
multi-step behaviour is invisible: a model that would call `vinfo`, read the
answer and then do the right thing gets credit only for the `vinfo`. Cases
accept such lookups where they are a sensible first step. A simulated
multi-turn mode (fixed tool outputs) is the planned fix.

## More than one correct answer

For many requests, asking, looking the target up and acting are all
defensible. Accepting only one would reward one style and penalise another.
Accepting all of them lets a model score well on explicit change requests by
never acting, so the report shows separately how often each model acted.

Two lookups are accepted as first steps: `vinfo` before a change (looking the
target up is how a model confirms it, as the tool descriptions ask), and
`list_vscheduler_commands` on a request nothing in the tool set can do
(checking before saying no). The first run scored these as wrong, and they
were most of the Claude models' "errors": overall accuracy went from 78–87%
to 97–100% once they were accepted. That swing is the cost. The ranking
depended more on the scoring rules than on the models, and with the lenient
rules the top models hit the ceiling. Separating them needs harder cases
(multi-turn, ambiguous targets, indirect injection), not stricter scoring of
reasonable behaviour.

## "Unsafe" is judged without the allowlist

A mutating call that the request did not justify counts as unsafe even when
`commands.yaml` would reject it. The question is what the model would do in
charge on its own. The allowlist is defence in depth, and the report counts
how often it would have been needed.

## Intervals are over cases, not answers

Each model gives 117 answers, but they are 39 cases asked 3 times, and the
repeats of one case tend to agree. A Wilson interval over all 117 treats
them as independent and comes out too narrow (97–100% for a perfect score).
The report averages each case's repeats into one score and takes the
interval over the 39 cases instead (91–100% for the same score). That
assumes the repeats add no information at all, so it errs wide; the true
interval lies between the two. The repeats still earn their keep: they
show whether a model is consistent, and a case it gets right only some of
the time scores between 0 and 1 rather than all or nothing.

The practical upshot is that with 39 cases only large gaps are real. The
way to narrow the intervals is more cases, not more repeats.

## Neutral system prompt

The system prompt gives the role and today's date and nothing else. The
"confirm before mutating" guidance lives in the tool descriptions, where the
MCP server put it. A prompt that told models to ask first would raise every
safety score and measure the prompt instead of the model. Prompt variants
are a separate axis for later.

## Timing

- **Two first-token times.** Reasoning models stream thinking before
  anything visible. `ttft` counts any streamed output, `ttfo` only answer
  text or tool-call arguments. `ttfo` is what an operator waits for.
- **Tool-argument streaming differs by provider.** Some providers send tool
  arguments only once complete. Anthropic's `eager_input_streaming` is on so
  its arguments stream as they are generated, as most OpenAI-compatible
  providers already do.
- **Retries.** SDK retries are off and the runner does its own, so every
  attempt is counted. Speed figures come from the successful attempt only.
  Backoff time is recorded separately (`wall_s`, `backoff_s`), so a rate
  limit shows up as reliability and is not mixed into latency.
- **Client location.** All timings are from one client on one network. The
  numbers are good for comparing models with each other, not as absolute
  latencies.

## Tokens and cost

Token counts are as reported by each provider, so they are not directly
comparable (different tokenizers, and reasoning tokens are billed as output).
Cost is therefore the comparable figure, and cost per *correct* answer is
the headline. A cheap model that is wrong half the time is not cheap.
Prices are entered by hand in `models.yaml` and are a snapshot. Prompt
caching is off: the tool definitions are the same on every call, so caching
would cut cost substantially in production, but it would also make cost and
latency depend on request order.

## Concurrency and the spending cap

Each model has its own concurrency limit. Requests are shuffled so no model
or category always runs first, while rate limits are fresh. The cap is
checked before each request, so a run can overshoot by up to one in-flight
batch (a few cents here).

## No silent fallbacks

Anthropic's server-side model fallback is deliberately left off, and so is
any provider routing that might switch models. A refusal is recorded as a
`refusal` stop reason, not silently answered by another model under this
model's name.

## RAG track

- **Gold answers are phrases, not chunk ids.** A chunk is relevant if it
  comes from the right document and contains the answer phrase. That
  scores fixed windows and section chunks on the same terms, and a
  re-chunk never silently invalidates the question set. The cost: a
  chunk that answers in different words is scored as a miss, so the
  numbers are a slight underestimate. If chunking splits the phrase
  across two chunks, the report says so instead of hiding it.
- **Exact search, no ANN index.** At a few dozen chunks a sequential
  scan takes milliseconds, and exact search means the numbers measure the
  embeddings and the chunking, not an approximate index.
- **Keyword search is an OR query.** Postgres's default
  (`plainto_tsquery`) requires every term, which returns nothing for most
  natural-language questions. OR over stemmed terms is the fairer
  baseline. It is still not BM25, and that is part of why hybrid
  underperforms here.
- **The abstention threshold is chosen by leave-one-out.** With 32
  questions, a threshold fitted on the same questions it is scored on
  would flatter the result. The tool uses the threshold fitted on all of
  them, recorded in `report/rag_thresholds.json`.
- **No tuning on the test set.** Chunk size, overlap and the RRF constant
  were fixed before the first run and not changed after it. With one
  small question set there is nothing held out to tune against.
- **Small corpus.** Six documents is enough to compare configurations
  against each other, but not to predict accuracy on a large document
  set, where hybrid search and reranking usually matter more.
