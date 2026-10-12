# Private shopper configuration

Keep preferences outside the repository. The default file is
`%LOCALAPPDATA%/DealForge/preferences.json` on Windows, or
`$XDG_CONFIG_HOME/DealForge/preferences.json` (default `~/.config/DealForge`) on
other systems. Override its path with `DEALFORGE_PREFERENCES_FILE`; paths inside
the project are rejected. The file is optional when callers pass explicit values.
No private preference values are included in this repository.

```json
{
  "cvs": {
    "zip_code": "00000",
    "store_id": "10001",
    "store_timezone": "UTC",
    "profile": {"return_days": [7, 30]}
  },
  "heb": {
    "zip_code": "00000",
    "store_id": "101",
    "name": "Example H-E-B",
    "address": "Example store address"
  }
}
```

These are fictional placeholders, not real store defaults. Replace them in the
external file with your preferences. ZIP must be five digits; store IDs are
numeric strings. CVS timezone must be an IANA timezone. CVS `profile` supports
`return_days`, `future_eligible_spend`, `cash_budget` and `new_reward_value` as
documented in the credit optimizer guide. An explicit profile replaces the local
profile. Without a profile, generic fallback scenarios are 7/30 days; without a
timezone, expiry calculations use UTC. Use the actual store timezone for shopping.

Agents may return ZIP/store/account observations during private tool use. Do not
paste those results into public PRs, issues, documentation or fixtures. Keep
shopping lists, receipts, account snapshots, browser state and diagnostic output
outside the repository. Only synthetic or location-redacted fixtures are tracked.

Browser sessions and account bindings remain under the existing external app-state
directory. Preferences contain no password or card number. Tests point to isolated
fictional configuration and do not read the developer's actual preferences.
