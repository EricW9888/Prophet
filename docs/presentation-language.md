# Presentation language and source translations

Settings retains a presentation preference: English (`en`), Spanish (`es`), French
(`fr`) or Simplified Chinese (`zh-Hans`). Unsupported preferences are rejected by
the API; old settings default to English. Save the setting to apply it. Navigation,
language controls and the evidence translation panel use i18next/react-i18next
resources with English fallback. Other UI copy and deterministic fallback answers
remain English. This is explicitly partial UI coverage, not full localization.

Supported new model-generated chat answers, operating answers, research thesis
summaries and independent critique summaries receive a scoped presentation
instruction. Schema keys, enum values, tool arguments, search queries, identifiers,
evidence quotations, citations, dates, numbers and financial units keep their
contracts. The reasoning cache separates languages. Existing answers and accepted
conclusions are not rewritten by changing the preference. A generation instruction
is not a guarantee that every provider follows it; deterministic/untranslated text
and provider failures remain visible.

The evidence receipt offers a clearly labeled translated view and the unchanged
stored original text. Reading or generating this view never runs extraction,
corroboration or belief promotion. A successful derivative is retained in
`evidence_translations`, keyed by evidence ID, SHA-256 of the actual UTF-8 source
text, target language and translation recipe version. It retains its provider and
creation time. Source changes leave old derivatives in place but mark the current
view stale until a new derivative is requested. A late response for another
language cannot be displayed as the selected language.

GET only reads a current cached derivative; POST explicitly requests generation.
Cached views make no model request and remain useful during provider outages.
Simultaneous generation requests converge under a PostgreSQL transaction lock and
unique version/language constraint. Each request is bounded to 24,000 source
characters and 45 seconds; oversized or non-UTF-8 material is disclosed instead of
silently truncated. Failures are safe status codes, not private provider diagnostics.

The request protects exact URL/citation, identifier and numeric/financial spans
with markers, then checks marker multiplicity and restored protected-span counts.
Damaged output is rejected and never cached. This conservative check may reject
otherwise useful prose and does not certify semantic equivalence, paragraph
coverage or financial interpretation. Compare machine translations with the
original before relying on meaning. The original text and provenance remain
available independently.

No translation subscription or second provider is required. Existing cached
translations are free to read; generating one uses the configured provider's
existing account, free credits where available, rate limits and possible charges.
The UI discloses that the source text is sent to that provider. There is no implicit
paid or local fallback, and the durable provider circuit defers unavailable work.
Changing the configured provider remains an explicit Settings action.

The localization library's [React integration](https://react.i18next.com/latest/using-with-hooks)
and [fallback behavior](https://www.i18next.com/principles/fallback) own library use;
the coverage and evidence boundaries above are Prophet's product contract.
