# temporal_context plugin

Neutral, configurable ephemeral temporal context injection for Hermes.

What it does:
- registers a `pre_llm_call` hook;
- appends current local time context to the current user message only at API-call time;
- does not persist the injected context to session history;
- does not modify the cached system prompt;
- uses no hardcoded user name, tenant name, timezone, language, or AIWerk-specific policy.

Enable in `config.yaml`:

```yaml
plugins:
  enabled:
    - temporal_context

temporal_context:
  enabled: true
  timezone: America/New_York  # set per user/tenant/agent during onboarding
  display_name: "Operator"  # optional; omit for neutral wording
  relative_time_warning: true
```

Default behavior when enabled without config:
- timezone: `UTC`
- no display name
- relative-time warning enabled

Example injected block:

```text
[Temporal context: current local time is 2026-05-17 17:35 UTC (+0000). Time zone: UTC. Relative time/daypart claims require an explicit timestamp from tools, messages, or provided context.]
```

Optional output guard:

```yaml
temporal_context:
  output_guard:
    enabled: true
    replacements:
      - pattern: "I'll stop here"
        replacement: "I will pause here"
      - pattern: "(?i)earlier today"
        replacement: "earlier today, if supported by the available timestamped context"
        regex: true
```

The output guard is intentionally no-op by default and entirely config-driven so public plugin code does not encode personal language policy.
