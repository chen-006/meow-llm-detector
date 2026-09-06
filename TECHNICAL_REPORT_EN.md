# meow LLM Detector 4.5.1 — Technical report

[中文](TECHNICAL_REPORT_CN.md) · [Getting started](README_EN.md) · [Evidence tables](docs/EVIDENCE_EN.md)

This report describes the release source, GPT benchmark `4.5.0-rc4`, and Claude benchmark `4.5.1-rc1`. Application and benchmark versions are independent. Benchmark versions and digests remain unchanged to preserve experiment identity. Numerical claims can be traced to the bundled JSON. User acceptance is not a mathematical or security certification.

## 1. Question and evidence chain

The task is to compare finite answers from a tested API against reference candidates, not inspect weights or authenticate a supplier. The chain is:

**Frozen request → short answer → deterministic normalization → windowed counts → reference distributions/drift → multi-probe likelihood → unique threshold crossing → report.**

[Bruckner's paper](https://arxiv.org/abs/2607.10252) motivates model-specific short-answer distributions. Its reported EER is approximately7.3% with40 cells and below11% with8. This project is not a reproduction of that experiment: it uses four candidates, five probes, different sample counts, and a custom aggregate decision rule. Our same-pool wrong-direction rates are not its EER, and the paper does not certify this implementation's accuracy.

A request model is a string sent to the provider; a reference candidate is a comparison target. A strong match is relative to that candidate set. OpenRouter provenance is not cryptographic authentication of model weights.

## 2. Request contract

Bundled probes use `system="."`, `low`, no history, no tools, streaming, and a128-token output limit. Temperature/top_p are not explicitly set, leaving provider defaults as possible sources of variation. Each probe receives4/10/20 requests at low/medium/high, totaling20/50/100. Default retries are bounded at2; the UI shows the maximum attempts. Failed attempts do not become valid samples, and retries do not add statistical observations beyond the frozen jobs.

| Mode | Suffix added to API base | Main payload | Appearance |
|---|---|---|---|
| GPT | `/responses` | system/user input, reasoning.effort, store=false, max_output_tokens | fixed GPT/Codex UA |
| Claude | `/messages` | system, messages, adaptive thinking, output_config.effort, max_tokens | claude-cli/2.1.251 (external, cli) |
| Other | `/chat/completions` | messages, reasoning_effort, streaming usage, selected token-limit field | standard |

[Request examples](docs/REQUEST_EXAMPLES.json) contain exact payloads rebuilt from the frozen package through the production payload builder. They are **reconstructions, not raw captures**. Authentication headers are excluded. The transport uses Bearer for OpenRouter and Anthropic-style authentication for direct Claude requests. An incompatible provider produces an explicit error; effort is not silently dropped while claiming equivalence.

Reasoning effort does not guarantee zero thinking. Output limits can include both visible answers and reasoning, as discussed in [OpenAI's reasoning documentation](https://developers.openai.com/api/docs/guides/reasoning). Occasional larger reasoning usage was not retrospectively excluded from formal collection. A period may override a particular relay's hidden prompt, but this is provider-dependent, not guaranteed. A UA does not prove routing behavior.

## 3. Package and provenance

An immutable `.meow.json` holds mode, candidate IDs and sampling aliases, exact probe/cell requests, normalizers, families/groups, per-model/per-window observations, quality counts, tier allocations, fitted values, thresholds, simulation seeds/batches/confusion matrices/contract hashes, provenance, transfer assumptions, validation scope, and a content digest. `benchmark.py` is the executable schema authority. Packages cannot contain executable plugins or arbitrary regex code.

GPT uses English country, English bird, B80 counting, ZH027 Chinese onomatopoeia, and ZH006 Chinese country. Claude uses CL011 invented-word selection, CL033 letter selection, CL039 word selection, CL057 seat selection, and CL085 terrain selection. [The evidence appendix](docs/EVIDENCE_EN.md) is generated from the packages and lists exact prompts, categories, pairwise JSD, and thresholds. B80 is an explicitly retained exception with an objectively correct answer; it is not an arbitrary-choice probe.

The2026-09-05 formal campaign collected2000 Claude and1100 GPT responses, costing$0.565265 and$0.4236336, with no errors/retries. Its four windows were separated by at least15 minutes after each whole window. The current GPT package subsequently replaced400 integer-probe observations with400 Chinese-country observations. Original campaign cost therefore is not the isolated cost of all observations retained in the current package.

Historical Sol/Terra/Luna builtins retain120 country,120 bird, and80 B80 observations per model. Their actual historical system was empty, whereas runtime uses a period. This is an explicitly accepted transfer assumption, not fresh collection. The ordinary-UA GPT formal pool also has a disclosed transfer to current Codex UA. These exceptions do not authorize relabeling old Chinese-probe samples.

Fresh Chinese-country collection:100 samples for each of Astra/Sol/Terra/Luna, four windows of25; actual gaps60.133/60.122/60.135 seconds,400 valid responses, no retries, cost$0.073706. The earlier64-response two-probe recheck informed selection but is not mixed into this pool. New generator windows require at least60 seconds. Short windows measure short-term variation, not cross-day stability.

One historical Sol English-country INVALID remains in raw counts. Fitting and simulation exclude it, while quality metadata retains it. This removes its influence on probabilities, not the evidence that it occurred.

## 4. Normalization and completion

Each cell names a deterministic normalizer. English behavioral labels trim edge punctuation, casefold, and collapse whitespace. Chinese arbitrary answers primarily trim/casefold without translation or synonym inference. B80 accepts integers and maps3 to exact_3, other integers to other_integer. Empty, overlong, or malformed answers become`__INVALID_OUTPUT__`; see`normalizers.py`.

An otherwise valid unseen category becomes`__OTHER__` at scoring time. Unknown is not invalid, and a rare answer must not be removed merely because the reference never saw it. Conversely, HTTP200 or a completed stream is not a valid-answer guarantee.

New 4.5.1 tasks require at least ceil(.6 × planned) valid answers overall and per enabled cell: 3 of 4, 6 of 10, and 12 of 20 per cell. Deficient cells cannot produce a strong direction; overall shortage is shown as insufficient valid requests. Zero allocation disables a cell. Old tasks retain the 90% rule and saved reports are not rescored. Parse failures and invalid answers share the finite retry budget. Earlier calibration used full valid batches; its 99% result is not guaranteed for partial samples. A lower coverage gate does not improve identification capability.

## 5. Fitting, JSD, and weights

For each cell, use the shared union of all candidates' valid categories plus OTHER, of sizeK. With effective counts n and additive smoothing alpha=.5:

`p(m,k) = (n(m,k)+.5) / (Σk n(m,k)+.5K)`.

INVALID enters neither the category space nor this denominator. Smoothing prevents zero likelihoods, but influences small-sample JSD. Values obtained with different sample counts/category spaces require qualification when compared.

`JSD(p,q)=.5 KL(p||(p+q)/2)+.5 KL(q||(p+q)/2)`, using base2 logarithms and range0–1. S is mean pairwise JSD between pooled candidate distributions. D_m is the mean pairwise JSD between a model's nonempty windows; D averages the available D_m values. Cell weight is`w=(S-D)/S` if S>D, otherwise0. Fewer than two windows do not establish stability; recommendation eligibility distinguishes unavailable drift.

JSD characterizes reference separation and contributes to weighting; it is not itself the runtime classifier. Mean S can conceal a weak model pair, motivating complementary selection.

## 6. Scoring and decisions

For test counts x in cell c, compute mean natural-log likelihood:

`L(c,m)=Σk x(c,k) ln p(c,m,k) / Σk x(c,k)`.

The mean prevents a heavily sampled cell from gaining linear voting power. Additional samples primarily reduce random variation in its mean. Within each family f, considering positive-weight cells:

`F(f,m)=max(w_c) × Σc(w_c L(c,m))/Σc w_c`; `score(m)=Σf F(f,m)`.

Stable softmax gives`match(m)=exp(score(m)-max(score))/Σj exp(score(j)-max(score))`. The values sum to1 but are **not identity posterior probabilities**, and98% does not mean98% of requests used that model. Unknown models are not comprehensively modeled.

After valid-count, reference-readiness, and calibration checks, form`W={m:match(m)>threshold(m)}`. Exactly one crossing yields a strong match: green if it equals the claimed model, otherwise red. Zero or multiple crossings yield yellow. Thresholds differ by model, so the unique crossing need not be the highest score; this is an explicit property of the current rule, not a hidden argmax substitution. Display rounding does not alter strict full-precision comparison.

Family aggregation limits simple accumulation within a family; it is not a learned correlation model. English and Chinese country currently belong to separate families despite related subject matter. Neither this grouping nor independent per-cell simulation proves independence or robustness to a shared hidden prompt shift.

## 7. Calibration and denominator

Scoring is`meow-fingerprint-v2`; simulation is`empirical-multinomial-pcg64-v5-valid-answers`. For each true candidate and enabled cell, multinomial draws use effective raw-count frequencies. Smoothing is used for scoring, not added to the empirical draw pool. Every simulated batch directly contains the planned number of valid answers.

Each published tier verifies10 million batches,2.5 million per model, with an additional100,000/model for selection. Seeds are GPT49106/49107/49108 and Claude47101/47102/47103. PCG64 uses SeedSequence[seed,0,model_index] for selection and[seed,1,model_index] for verification, in4096-batch blocks. A fixed category-reduction order and shared`numeric_matches` keep real and batched scoring aligned without BLAS shape-dependent rounding.

For selection coverage q=.999, sort own-model scores, take position`n-ceil(q*n)`, then choose the adjacent float toward negative infinity so boundary observations strictly cross. Replay the joint unique-crossing rule and retain selection_confusion rather than treating own crossing as success. Finally, independently seeded verification must reach at least.99 unique-correct fraction for every true model to mark target_met. There is no automatic lowering of that target.

**.999 is selection coverage; .99 is verification target. Neither is a simulation count, confidence level, or real-run success rate.** Even a provider with only1% valid responses could have excellent conditional simulation scores while rarely passing the real valid-count gate. Network/invalid-output failure is outside this metric's denominator.

Ten million resamples reduce Monte Carlo error conditional on the empirical pool; they do not turn100 collected answers into millions of independent observations. Independent random substreams are not an independent collection pool. This release has no complete same-contract independent four-model validation dataset and records independent_real_validation=false.

## 8. Results and probe replacement

| Group/tier | Minimum conditional correct rate | Wrong directions per10m |
|---|---:|---:|
| GPT low | 99.88276% | 43 |
| GPT medium | 99.89764% | 0 |
| GPT high | 99.87628% | 0 |
| Claude low | 99.88848% | 0 |
| Claude medium | 99.88344% | 0 |
| Claude high | 99.89092% | 0 |

Zero means none observed, not zero risk. Full thresholds, correct/wrong/insufficient matrices are in the appendix and calibration.tiers.

During acceptance, tested integer46/47 frequencies shifted away from the formal Astra pool; the cause was not established. A40,000-batch/tier removal diagnostic put low-tier Astra at95.65% and Sol at98.43%, below target. A bare four-probe deletion was therefore not deployed. A two-probe recheck favored Chinese country, then a fresh four-model pool was collected before replacement.

Chinese-country counts were led by Astra Brazil58/100 and Canada30/100; Sol Iceland86/100; Terra Iceland93/100; Luna Canada92/100. Astra/Sol JSD is.74061, with four window values approximately.5859/.5395/.6870/.6369. Sol/Terra is only.05070, complemented by English-country and bird JSD about.5725/.3557. Correlation/common drift remain limitations. Comparing old rc3 low-tier3872 wrong batches/10m with new43/10m does not establish the same factor of improvement on a real provider.

## 9. Custom benchmark generation

Freeze candidates, API aliases, protocol, prompts, and normalizers before collection. Input can be manual, imported, or AI-generated. One AI call produces at most10 candidates, drawing seed keywords from12 broad domains/120 topics and retaining seed/prompt versions and actual candidates. Instructions seek neutral arbitrary choices, not factual questions, optimal answers, or explanations. AI does not know discriminating power and does not invent distributions or thresholds. Humans still review semantics; duplicate checks are not semantic compliance guarantees.

Suggested screening counts are3/model/cell, then5 additional, then8 additional per later window. Each generator window is confirmed by the user and separated by at least1 minute. Recommendation considers separation, drift, valid rate, and cost. For pair(a,b), cell contribution is:

`E(c,a,b)=w_c × min_model(valid_rate) × max(0,JSD(a,b)-max(Dmax_a,Dmax_b))`.

Only eligible low-tier enabled cells with estimable drift contribute; multiple cells in a probe use max. Locks enter first; at most one probe per source group. Greedy selection lexicographically improves the ascending-sorted pair-coverage vector, breaking ties by request count, known median cost, and ID. It does not force filler probes or use a complex optimizer. The UI explains contributions, weakest pairs, and exclusions.

Near-duplicates use token-set Jaccard≥.90, showing the first100 pairs and total without deletion. Comparison uses the same valid pool, shared draws for common cells,2000 batches/model, seed45006, and unique-top-score accuracy, not strong-match accuracy. The mean-JSD comparator obeys identical eligibility/locks/source groups and recommendation's actual budget/probe-count limits. Unequal costs are disclosed, not forcibly matched. Preview never changes formal scoring.

Changing request semantics or candidate aliases breaks collection compatibility. Renaming or changing tier counts can reuse sampling, but changed counts require recalibration. Simulation tasks freeze input and retain checkpoints; changing browser drafts does not transfer ownership of an ongoing task.

## 10. Execution, security, and evidence retention

The app uses a local HTTP interface, shared finite executor, HTTPX, a single-writer SQLite store, and NumPy. It has no runtime Node/server-framework/ORM dependency. Historical report URLs come from the run's frozen endpoint, not the current form.

Model requests default to HTTPS; loopback HTTP is allowed and non-loopback plaintext needs explicit opt-in. Userinfo/query/fragment and model-request redirects are rejected. Exact OpenRouter HTTPS calls share at most4 in flight and response-header-driven limits, with default-port equivalence. Limit20 is not interpreted as20/minute. Environment and Windows manual proxy handling is shared; PAC is not executed.

The server binds127.0.0.1, checks Host/Origin and session token, serves an asset allowlist, and holds an OS data-directory lock. This is not a multi-user security boundary or public service. Bounded JSON, TLS, finite retries, cancellation, and deadlines remain. Frozen jobs and cumulative attempts survive restart; queued cancellation is not dispatch, sent cancellation still consumes an attempt. Resume does not erase charges.

Temporary keys live in page memory and active backend references, never localStorage. Page passwords clear on detected backend disconnect/session expiry; backend per-run references release after completion. Explicit presets use the OS vault. Schedules have independent credential references and produce separate runs, not rolling evidence.

Optional retention atomically stores logical request JSON, decoded UTF-8 responses, selected response headers, HTTP status, and completeness with attempt/result. It is not TLS wire data and omits authentication headers. Actual credential echo prevents response-body retention; field/pattern redaction adds protection but cannot recognize every encoding. User reports are not automatically public.

Published evidence consists of frozen requests, windowed normalized counts, and calibration results, not private SQLite or complete raw responses. This release does not publish raw network captures. Reconstructed examples must not be presented as original response evidence. Older v4.0 material remains traceable in historical releases but is not v4.5.0 independent validation.

## 11. Integrity, catalog, and reproduction

content_sha256 identifies canonical JSON semantics; catalog sha256 checks downloaded bytes. Neither is an author signature. Maintainer provenance comes from the release manifest/maintained catalog, and provider badges indicate collection origin only. New and resumed runs check cached security withdrawals; an offline client cannot know a newly published withdrawal.

`benchmarks/index.json` contains official packages and community packages submitted by PR. Validation does not execute package contents, access repository secrets, auto-endorse, or auto-merge. Source trust and calibration status remain separate. Unlisted models may lie outside the current candidate universe.

After installing dependencies, from the repository root:

```sh
python scripts/reproduce.py --check-only
python scripts/reproduce.py --mode gpt --quick
python scripts/reproduce.py --mode gpt
python scripts/reproduce.py --mode claude
python -m unittest discover -s tests -v
node tests/report_urls.cjs
python scripts/build_release.py --output dist
```

check-only validates packages; quick uses8000 batches/tier and is not a reproduction of published totals. Full mode uses the original three10m tier totals/seeds/targets, with no API calls. Results go to a separate directory and compare thresholds/confusion without changing official packages. `docs/REPRODUCTION_ENV.json` records the numerical runtime. Strict floating boundaries may vary across Python/NumPy/platforms; report discrepancies rather than re-signing historical numbers. The builder fixes file order and ZIP timestamps; language packages only change default locale and README entry, not benchmark bytes.

## 12. What remains unproven

Same-contract independent validation, cross-day/account/IP stability, systematic hidden-prompt/quantization/reasoning experiments, unknown-candidate rejection, and adversarial fixed-probe routing are not solved here. Paper evidence, user acceptance, agent review, unit tests, and same-pool simulation answer different questions. Use reports as repeatable anomaly signals alongside request IDs, timing, provider records, and independent retesting, not as a verdict on a provider by color alone.
