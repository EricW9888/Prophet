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

Opportunity discovery currently inspects an operator-selected security universe
in bounded batches. Its run history records inspected subjects, skips, failures,
provider attempts, and estimated metered credits; it does not establish that the
whole investable market was searched. Candidates remain provisional until a
person reviews them or explicitly starts a paper-only shadow experiment.

YouTube ingestion uses available captions or an explicitly supplied transcript.
Prophet does not currently transcribe arbitrary no-caption audio or interpret a
complete video from frames alone.

## Analytical Reliability

The model can misclassify a subject, omit a material driver, misuse context, or
produce an incorrect synthesis. Deterministic promotion and corroboration rules
reduce unsupported state changes but cannot guarantee that an accepted view is
complete or economically correct.

Historical analogies, pattern hypotheses, source scores, sentiment, ownership
signals, and inferred relationships are investigation aids. They are not
predictions or independent proof. Material decisions require inspection of the
underlying dated sources and contrary evidence.

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
