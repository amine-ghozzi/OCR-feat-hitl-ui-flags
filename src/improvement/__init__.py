"""Self-improving control & correction loop (dev-time, on-demand).

Detect quality issues on the current build (`signals`), validate candidate fixes behind
a regression gate (`regression_gate`), and apply reversible fixes with rollback
(`applier`). Post-deployment triggers/monitoring are deferred (see the plan).
"""
