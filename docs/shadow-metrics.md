# Shadow performance measurements

Shadow risk statistics use captured paper-account equity, not terminal alpha
or terminal loss. `performance_metrics_json` records availability, reasons,
input counts, dates, a digest of the observations, and calculation conventions.
Raw observations remain in the experiment's `run_details.performance_observations`.

Maximum drawdown is the minimum of `equity / previous_peak - 1`, including
the starting account snapshot. It is a nonpositive fraction. It measures only
the captured path: sparse checkpoints can miss intervening declines. Irregular
observations can support this observed drawdown, but cannot support Sharpe.

Sharpe uses the arithmetic mean of successive simple equity returns divided
by their sample standard deviation (ddof 1). The comparator is paper cash,
which earns zero in the simulator. The ratio is **per observed interval,
not annualized**; it must not be compared across different cadences. It needs
at least two returns from equally spaced observations, allowing up to one second
or 1% of the interval, whichever is smaller, of
timestamp jitter. This is a mathematical minimum, not proof of statistically
reliable performance. No missing observations are interpolated. See
[Sharpe's definition and time-dependence discussion](https://web.stanford.edu/~wfsharpe/art/sr/sr.htm).

Zero variance, insufficient observations, invalid dates/equity, and unknown or
nonzero external cash flows produce unavailable values with explicit reasons.
Sample standard deviation at or below `1e-12` is treated as zero variance.
Evidence-triggered checkpoints and scheduling delays can make a live run's
cadence irregular; that run exposes unavailable Sharpe rather than an invented
regular return series.
Trades, fees, dividends and splits are internal account activity. Deposits and
withdrawals would require flow-adjusted valuation at the owning account layer;
they must not be treated as investment returns. See
[CFA Institute's explanation of external cash flows](https://blogs.cfainstitute.org/blog/2013/10/09/performance-reporting-winning-the-scoreless-game/).

The migration archives legacy proxy values in `legacy_proxy_values` and clears
the published risk statistics. Existing checkpoint histories remain unchanged;
they do not establish the missing cash-flow provenance. Correcting an account
history invalidates its captured metric path rather than silently recomputing
from inconsistent observations. A resumed legacy run measures only observations
captured after the upgrade, with first/last capture dates exposed.
The old schema cannot represent unavailable drawdown. Downgrade is deliberately
refused; using that schema requires restoring a verified pre-upgrade backup.

Reports, API responses, Shadow Lab and graph properties carry the same
availability contract. Lesson calibration still concerns terminal alpha;
its provenance explicitly distinguishes that comparison from risk-adjusted
performance and causal thesis validation. Missing Sharpe is not zero, and a
profitable paper path does not establish real execution or investment skill.
