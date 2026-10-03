# GPT 6.1 Sol baseline (2026-10-03)

**128 requests recommended.** The 32- and 64-request tiers distinguish 6.1 Sol from Astra less reliably and may return insufficient evidence more often. All three tiers remain available; 128 requests do not guarantee accuracy.

Punctuation / English country / integer 17–83 allocations: 20/4/8 (32), 32/8/24 (64), 64/16/48 (128).

Candidates: 6.1 Sol, 6 Astra, 5.6 Terra, 6 Luna. `other` references: 6 Sol, 5.6 Sol, 5.6 Luna, GLM 5.3. No GPT 5.4 or 5.5. Scoring, 60% sample eligibility, 98% threshold cap, retry policy and Claude baselines are unchanged.

Worst resplit correct direction / maximum insufficient evidence / maximum full-run wrong direction: 32 requests 33.06% / 66.92% / 0.14%; 64 requests 87.96% / 12.00% / 0.06%; 128 requests 99.92% / 0.04% / 0.04%. Extremes may come from different groups and need not sum to 100%.

These are reused-data development simulations, not independent blind validation or live accuracy. Lower tiers missed the original correct-direction goals and are retained by explicit maintainer choice; false-direction risk limits are unchanged. The legacy loading contract tests full-fit replay (70% low, 99% medium/high), not resplit acceptance. Both evidence scopes remain in the packages.

Native: 4.5.4-predictive.20261003.1; Chat: 4.5.4-chat.20261003.1. Chat inherits native observations, without separate Chat sampling. Application version remains 4.5.4; historical reports are not recalculated.
