### Changed

- `li agent` on an `anthropic/<model>` profile now moves to `claude_code/<model>` when the account router refuses the run's first request with `budget_exhausted` or `account_blocked`, before anything was sent.
  The run prints one warning naming the refusal code and the new model, records the original model, the model used and the refusal code in `subscription_fallback` in `run.json`, and records `claude_code` and its model as the run's provider and model.
  A refusal after a reply, any other router code and any other provider still fail with the routing error, and a run moves at most once.
