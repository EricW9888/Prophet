# Test Data Boundary

All company names, tickers, holdings, prices, transactions, evidence, and market
events in this directory are synthetic regression fixtures. They do not describe
a real portfolio or claim to represent current market facts.

Use synthetic identifiers such as `MEMA`, `MEMB`, `AUTO`, `INFR`, and `EXMPL`
when adding tests. A real issuer may appear only when its public API syntax or
document format is itself the behavior under test, and the fixture must not
include private holdings or transaction data.
