# Conversational Shopping Agent: Research-Led Architecture Decisions

## Executive decision

The agent should be built as a **stateful conversational retrieval system**, not
as a general chatbot and not as a single-label intent classifier.  Each user
message can simultaneously convey a shopping mission, product constraints,
feedback on existing results, and a request to revise earlier preferences.
The system must preserve each of those signals independently, use them to plan
retrieval, and ask a question only when its expected improvement is greater
than the cost of spending a turn.

This matters especially in this challenge: a correct Top-10 result ends the
session, while every withheld recommendation sacrifices an opportunity to hit
early.  The competition’s 40% Buying / 40% Browsing / 15% Intent Override / 5%
Boundary split should inform evaluation and policies, but it is not sufficient
as the whole intent model.

## Research findings

### Consumer search behaviour is multi-dimensional

Baymard’s e-commerce research identifies twelve overlapping query types:
exact, product type, feature, thematic, relational, compatibility, symbol,
subjective, symptom, implicit, non-product, and natural-language queries.
Most importantly for this system, a feature query is normally a qualifier of
another query type, rather than the shopper’s entire intent.  For example,
“black waterproof hiking boots under $100” combines product type, feature,
use case and price.  The architecture must therefore preserve all recognised
facets rather than route on a single class.

Baymard also documents that users commonly search using problems or outcomes
when they do not know the product category.  In clothing, that maps directly
to occasion, environment, activity and fit needs (“something for a wedding”,
“for cold weather”, “comfortable for walking”).  These terms should be first
class retrieval signals, not discarded as conversational filler.

### Clarification is an optimisation problem, not a scripted questionnaire

Conversational-search research characterises a complete system as the joint
operation of query reformulation, search clarification, conversational
retrieval and response generation.  Research on search clarification further
shows that user interaction varies with query and question properties, and is
subject to presentation and position bias.  The implication is straightforward:
asking every attribute in a fixed order is unjustified.  The next question
should be selected for its expected value for the current candidate set.

Conversational recommender-system research reaches the same conclusion:
dialogue enables explicit preference elicitation, and attribute-selection
methods that prioritise ranking benefit reduce turns and improve
recommendations.  The system should use a deterministic, measurable proxy for
this value rather than claim that an LLM intuitively knows the best question.

### People value a useful outcome over conversation

In Qualtrics’ 2025 global study of nearly 24,000 consumers in 23 countries,
automated chat was the least popular of the studied channels and trust was a
central priority.  Gartner’s 2026 consumer survey likewise found users expect
GenAI to complete tasks, not merely answer questions.  This rules out a
chat-first agent that repeatedly says it is “learning preferences.”  It should
return useful candidates early, explain the salient match, and ask at most one
short, decision-relevant question.

## Architecture

```text
message + prior state + low-weight profile prior
                    |
                    v
  multi-axis parser: mission, dialogue act, constraints, polarity
                    |
      +-------------+-------------+
      |                           |
      v                           v
event-sourced state          retrieval/action planner
      |                           |
      +-------------> query plan <-+
                               |
                               v
 lexical retrieval + structured constraint scoring + optional dense retrieval
                               |
                               v
                   constraint-aware reranker
                               |
                               v
       ranked Top-10 + concise explanation + optional next question
```

The LLM, if used, belongs only in constrained extraction/reformulation or
reranking.  It must not be the source of truth for state, must return a typed
schema, and must have an offline deterministic fallback.  The competition
explicitly permits organisers to disable network access during scoring.

## Decision log

| Decision | Recommendation | Reason |
|---|---|---|
| Unit of intent | Use a multi-axis representation, not one label. | A single utterance combines mission, facets and dialogue action. |
| Scenario model | Keep `buying`, `browsing`, `intent_override`, `boundary` as an evaluation/policy field only. | It matches the benchmark mix but cannot express actual query content. |
| Mission labels | `known_item`, `constrained_buying`, `exploration`, `use_case_problem`, `comparison`; use `unknown` only before enough evidence. | These labels change retrieval and clarification behaviour. |
| Query semantics | Extract `product_type`, `brand/exact`, `attributes`, `use_case`, `style/theme`, `subjective preference`, and `budget`. | These are overlapping e-commerce query types. |
| Dialogue acts | Support `set`, `soft_set`, `negate`, `no_preference`, `clear`, `override`, `feedback`, `compare`, and `information_exhausted`. | Appending text to a query cannot represent corrections safely. |
| Constraint state | Store value, attribute, polarity, strength, source turn, confidence, and superseded status; retain raw spans. | It prevents stale constraints and allows debuggable query construction. |
| User profile | Use only as a small tie-breaker/priors feature after explicit constraints. | The supplied profile is aggregate and should never override a stated need. |
| First-turn recommendations | Recommend from turn 1 whenever category retrieval yields credible candidates. | Every ranked turn can score a hit; delaying is an unnecessary scoring loss. |
| Clarification | Ask one question only when the candidate set is broad/ambiguous and the expected gain exceeds the cost of a turn. | Research supports active preference elicitation; the metric rewards early hits. |
| Question selection | Score missing attributes using candidate reduction, coverage, answerability, and novelty. | This is measurable and better than a fixed priority such as always asking `other`. |
| No preference | Record it as an explicit unconstrained attribute and never re-ask it. | “No preference” is not missing information. |
| Override | Atomically supersede affected earlier constraints, rebuild the query, and reretrieve. | Intent Override is 15% of evaluation; additive state will retain invalid terms. |
| Retrieval | Use hybrid lexical + structured matching; add local dense retrieval only if it wins in ablation. | Exact metadata matching is crucial in a frozen 50k-item catalog; semantic retrieval helps paraphrases but should not replace filters. |
| Ranking | Prioritise hard-constraint coverage, then lexical/semantic relevance, then soft preferences; use diversity only in exploration. | A diverse list is helpful while browsing but can reduce precision for a hard buying request. |
| Output language | State what was matched and ask a concise question if needed. Avoid generic conversational filler. | Consumers need credible, action-oriented support rather than chat. |
| Offline operation | Treat the deterministic path as the production baseline; APIs are optional enrichment. | The final environment may have no network or credentials. |

## Canonical state schema

```json
{
  "mission": "constrained_buying",
  "scenario": "buying",
  "constraints": [
    {"attribute": "category", "value": "hiking boots", "polarity": "must", "strength": "hard", "turn": 1},
    {"attribute": "color", "value": "black", "polarity": "prefer", "strength": "soft", "turn": 1},
    {"attribute": "budget", "value": {"max": 100}, "polarity": "must", "strength": "hard", "turn": 1}
  ],
  "no_preference": ["brand"],
  "asked": ["size"],
  "candidate_summary": {"count": 84, "entropy": 1.7},
  "events": ["set(category)", "set(color)", "set(budget)", "no_preference(brand)"]
}
```

`events` are append-only; the effective state is computed by replaying them.
An override adds a superseding event rather than silently mutating history.

## Pipeline policy by shopper situation

### 1. Known item / exact product

Use title, brand/store, model-like tokens and spelling-tolerant lexical search.
Rerank exact title and structured-field matches heavily.  Return products at
once; clarification is normally unnecessary unless the item name maps to many
parents.

### 2. Constrained buying

Extract hard, soft and negative facets.  Filter/rerank by category,
department, material, size, colour and price where catalog metadata is
reliable.  Return Top-10 immediately.  Ask the highest-value missing field
only when the candidate set remains large or conflicts are present.

### 3. Exploratory browsing

Infer a broad category/use case, then return a deliberately diverse but
relevant set across styles or subtypes.  Ask a question that splits the
candidate set substantially, usually use case, category, fit/size or budget;
do not reflexively ask material.

### 4. Use-case or problem-led shopping

Map occasion/activity/environment language to category and feature expansions
but keep the original phrase in the query.  Search across plausible product
types; only ask for category if the mapping is genuinely ambiguous.  This is
where semantic retrieval or a local synonym map is valuable.

### 5. Comparison and feedback

Keep the displayed candidate set and requested decision criteria.  Retrieve
nearby alternatives and rank by the requested contrast (cheaper, warmer,
more durable) rather than restarting a global query.  This is outside the
official simulator but is required for a coherent real-product system.

### 6. Revision and boundary handling

For corrections such as “actually, blue instead,” remove the superseded colour
constraint before retrieval.  For “I do not care about brand,” mark brand as
unconstrained and do not ask it again.  For “I have no additional preference,”
stop questioning and recommend the best current list.

## Clarification-score specification

For each eligible attribute `a`, estimate:

```text
question_value(a) =
  0.45 * expected_candidate_reduction(a)
  + 0.30 * catalog_coverage(a)
  + 0.15 * answerability(a, mission)
  + 0.10 * ranking_instability(a)
  - repeated_or_declined_penalty(a)
```

Ask only if the best score clears a tuned threshold and the session has enough
remaining turns.  In the evaluator, attribute replies are deterministic; use
public-set ablations to tune attribute weights by product category and
scenario.  In a real interface, add friction cost more aggressively because
users do not owe the agent a long interview.

## Evaluation plan

Report the official Hit Rate@10, MRR, MTTC and Technical Score, broken down by
Buying, Browsing, Intent Override and Boundary.  Also track the internal
diagnostics below.  Aggregate score alone will hide a brittle override parser
or an overly inquisitive browsing policy.

| Diagnostic | What it reveals |
|---|---|
| First-turn Hit@10 | Whether retrieval is strong before elicitation. |
| Question rate / turns to first result | Whether the agent is wasting scoring opportunities. |
| Constraint extraction precision and recall | Whether retrieval failure starts in NLU. |
| Override reset accuracy | Whether stale preferences remain active. |
| Re-asked declined attributes | Boundary-state correctness. |
| Hard-constraint coverage in Top-10 | Reranker faithfulness. |
| Metrics by scenario and category | Whether one aggregate score hides systematic failure. |

Run ablations in this order: lexical baseline; structured constraint reranking;
state/event handling; adaptive clarification; dense retrieval; optional LLM
extraction.  Do not add an LLM before proving it improves paraphrase handling
without degrading exact constraint precision or offline reproducibility.

## Scope boundaries

Gift shopping, support/policy questions, order tracking and post-purchase
service are legitimate consumer intents.  They should be separate routes in a
real production assistant, not shoehorned into catalog search.  They are not
part of this benchmark’s hidden-target protocol, so building them before core
retrieval and state management would be scope creep with no evaluation return.

## References

1. Baymard Institute. *Ecommerce Search UX Best Practices: The 8 Search Query
   Types* (updated 2026). https://baymard.com/blog/ecommerce-search-query-types
2. Baymard Institute. *E-Commerce Search UX* research overview. The programme
   reports 25 qualitative test rounds, 4,400+ participant/site sessions and
   20,240 quantitative-study participants. https://baymard.com/research/eCommerce-search
3. Mo, F. et al. *A Survey of Conversational Search* (2024).
   https://arxiv.org/abs/2410.15576
4. Zamani, H. et al. *Analyzing and Learning from User Interactions for Search
   Clarification*, SIGIR 2020. https://arxiv.org/abs/2006.00166
5. Gao, C. et al. *Advances and Challenges in Conversational Recommender
   Systems: A Survey* (2021). https://arxiv.org/abs/2101.09459
6. Deng, Z. et al. *Enhancing User Personalization in Conversational
   Recommenders* (2023). https://arxiv.org/abs/2302.06656
7. Qualtrics XM Institute. *Consumer Channel Preferences and Priorities, 2025*.
   https://www.qualtrics.com/research/consumer-channel-preferences-priorities-2025/
8. Gartner. *Customers Are 3x More Likely to Use Third-Party GenAI Than
   Company-Provided Chatbots for Customer Service* (2026).
   https://www.gartner.com/en/newsroom/press-releases/2026-07-08-gartner-survey-finds-customers-are-three-times-more-likely-to-use-third-party-genai-than-company-provided-chatbots-for-customer-service
