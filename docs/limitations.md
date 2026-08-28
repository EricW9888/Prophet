# Current Limitations

Prophet is a personal research system under active development. This document
records durable boundaries a reader should understand before running or relying
on it. Concrete improvement work belongs in GitHub Issues rather than in this
file.

## Local-Only Security Model

Prophet is designed for one user on a trusted machine. The backend enforces a
loopback and allowed-origin boundary, but the application does not implement a
complete multi-user authentication or authorization system. It should not be
published to the internet or used as a shared service without a separate threat
model and security design.

Runtime settings, credentials, portfolio data, mailbox data, evidence, media,
and backups are local sensitive state. Repository checks reduce the chance of
committing them; they do not replace careful secret handling or credential
rotation after exposure.

The frontend can be installed as a PWA from a secure origin, but this does not
change Prophet's security model or make the application safe to expose publicly.
Its service worker caches only a static offline notice and app icons; it does not
cache portfolio, evidence, chat, source, or API responses. Private access from
another device is supported only through the loopback-preserving, identity-gated
Tailscale Serve boundary documented in [Private access](private-access.md). This
remains owner-only access rather than a multi-user authorization system.

## Data and Connector Coverage

Gmail or IMAP ingestion depends on the configured mailbox scope and the formats
of broker messages received. Optional Plaid support depends on institution and
product coverage. Neither path guarantees that every transaction, correction,
or corporate action will be available or normalized correctly; reconciliation
against broker records remains important.

Research discovery depends on external services, websites, and rate limits.
Prophet supports an explicitly configured SearXNG endpoint and Tavily fallback;
it does not operate a search index or start SearXNG itself. Metasearch results
can be incomplete or noisy, while direct source pages can block automated
retrieval. Search is source discovery, not a complete market-data or retrieval
solution. Market prices and public web results may be delayed, revised,
unavailable, or unsuitable for point-in-time backtesting.

Opportunity discovery currently inspects an operator-approved security universe
in bounded batches. Its run history records inspected subjects, skips, failures,
provider attempts, and estimated metered credits; it does not establish that the
whole investable market was searched. Candidates remain provisional until a
person reviews them or explicitly starts a paper-only shadow experiment.
The universe can be built additively from manual entries, holdings/watchlists,
active equity securities with entity research profiles, and each stored
benchmark's latest constituent snapshot. Import previews expose eligible,
already-present, missing, and skipped counts. Imports do not remove, re-enable,
or reprioritize existing members, and they are not a claim of complete exchange
or global-market coverage.
Each new discovery observation fixes its expected relative direction, horizon,
evidence packet, and evaluation policy before the outcome is known. Later
evaluation uses stored adjusted daily closes, the configured broad-market
benchmark, and cash as controls. This measures one return outcome; it does not
prove that the causal thesis was correct, that the trade was executable, or that
the historical price provider will never revise its data.

YouTube ingestion uses available captions first and may request a deeper pass
when the representation leaves a material question unresolved. An operator may
explicitly enable a free local audio adapter backed by separately installed
`yt-dlp`, `ffmpeg`, and the OpenAI Whisper CLI. Tool availability, model download,
source access, duration, size, timeout, language, and speech quality can all
prevent or degrade extraction. A separately enabled Tesseract adapter can read
text from bounded frame samples, but it does not interpret chart geometry,
expressions, product demonstrations, motion, or every intervening frame. A
requested visual pass that cannot run is reported as unavailable rather than
silently treated as covered.

Periodic channel review is bounded, depends on `yt-dlp`, and applies only to
operator-trusted YouTube sources. Upload metadata is provisional discovery, not
evidence. Automatic transcript ingestion is separately bounded, can fail or be
disabled, and never implies that every upload was discovered, watched, or fully
understood.

## Analytical Reliability

The model can misclassify a subject, omit a material driver, misuse context, or
produce an incorrect synthesis. Deterministic promotion and corroboration rules
reduce unsupported state changes but cannot guarantee that an accepted view is
complete or economically correct.

Historical analogies, pattern hypotheses, source scores, sentiment, ownership
signals, and inferred relationships are investigation aids. They are not
predictions or independent proof. Material decisions require inspection of the
underlying dated sources and contrary evidence.

Opportunity discovery covers only the operator-defined investable universe and
the subjects reached within each bounded run. A candidate rank is a provisional
comparison among inspected subjects, not proof that Prophet surveyed the whole
market or found the best available investment. Provider limits, stale sources,
failed fetches, model omissions, and the configured revisit interval can all
leave relevant opportunities unseen. The Ideas workspace exposes those coverage
limits and keeps assumptions separate from source-backed evidence.

Search-result snippets are retained as provisional discovery history, not as
evidence. They can be incomplete, stale, or misleading and cannot corroborate a
claim until Prophet fetches attributable source content. Likewise, a source
marked trusted by the operator is still fallible: Prophet may recommend a trust
review when later outcomes degrade, but it does not silently replace that
explicit choice.

Prophet does not expose a provider's hidden chain-of-thought. It stores concise
reasoning summaries, assumptions, evidence references, challenges, and action
traces that can be inspected without presenting private model reasoning as an
audit log.

## Portfolio and Simulation Boundary

Prophet is not a broker and never places real orders. Shadow experiments and
paper accounts are local simulations. Their fill, timing, liquidity, fee,
corporate-action, and market-impact models are intentionally simpler than a real
venue. A profitable simulated result is not evidence that the same trade was
executable or would have produced the same return.

Portfolio attribution is only as reliable as the transaction ledger, cash
flows, corporate actions, price history, and selected measurement window.

## Verification Depth

The backend has a substantial synthetic regression suite and a branch-coverage
floor, but external connector behavior still requires credentialed integration
checks. The frontend currently relies on linting, production builds, and manual
browser verification rather than a comprehensive automated interaction and
visual-regression suite.

Several central backend services and frontend pages remain large. Their tests
protect behavior, but concentrated modules increase review cost and make
incremental decomposition worthwhile.

## Project Support

The public repository is the canonical development repository, but Prophet is
not a supported distribution or hosted product. The current `main` branch is the
only maintained code line. See `SECURITY.md` for reporting and deployment
boundaries and `CONTRIBUTING.md` for the source-availability and contribution
policy.
