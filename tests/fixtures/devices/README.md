Fixtures derive from user-provided REC API observations (2026-09-27).
usage_all.json preserves the observed overview total and representative historical
heatmap/model entries. Arrays are deliberately shortened; they are not a complete
sum reconciliation fixture. Tests must read overview.total_usd for the aggregate.
No real authorization states, cookies, emails, or machine identifiers are stored.

`usage_fable_30d.json` retains the complete five-model amount breakdown supplied on
2026-11-10. Its API total is 963.0020359000 and its Fable subtotal is 798.2841590000;
the subscription-equivalent total is 1761.2861949000. The `30d` range is used only
for the pure billing parser test; production ledger sampling still requests `all`.
