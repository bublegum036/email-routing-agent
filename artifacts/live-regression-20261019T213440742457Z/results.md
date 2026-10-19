# Регрессия regression-v1

Реальная модель: True

| Кейс | Режим | Попытка | Успех | Ошибки | Трасса |
|---|---|---:|---|---|---|
| mail-01 | agent | 1 | True |  | [JSONL](mail-01-agent-1/trace.jsonl) |
| mail-01 | agent | 2 | True |  | [JSONL](mail-01-agent-2/trace.jsonl) |
| mail-01 | agent | 3 | True |  | [JSONL](mail-01-agent-3/trace.jsonl) |
| mail-02 | agent | 1 | True |  | [JSONL](mail-02-agent-1/trace.jsonl) |
| mail-02 | agent | 2 | True |  | [JSONL](mail-02-agent-2/trace.jsonl) |
| mail-02 | agent | 3 | True |  | [JSONL](mail-02-agent-3/trace.jsonl) |
| mail-03 | agent | 1 | True |  | [JSONL](mail-03-agent-1/trace.jsonl) |
| mail-03 | agent | 2 | True |  | [JSONL](mail-03-agent-2/trace.jsonl) |
| mail-03 | agent | 3 | True |  | [JSONL](mail-03-agent-3/trace.jsonl) |
| mail-04 | agent | 1 | True |  | [JSONL](mail-04-agent-1/trace.jsonl) |
| mail-04 | agent | 2 | True |  | [JSONL](mail-04-agent-2/trace.jsonl) |
| mail-04 | agent | 3 | True |  | [JSONL](mail-04-agent-3/trace.jsonl) |
| mail-05 | agent | 1 | True |  | [JSONL](mail-05-agent-1/trace.jsonl) |
| mail-05 | agent | 2 | True |  | [JSONL](mail-05-agent-2/trace.jsonl) |
| mail-05 | agent | 3 | True |  | [JSONL](mail-05-agent-3/trace.jsonl) |
| mail-06 | agent | 1 | True |  | [JSONL](mail-06-agent-1/trace.jsonl) |
| mail-06 | agent | 2 | True |  | [JSONL](mail-06-agent-2/trace.jsonl) |
| mail-06 | agent | 3 | True |  | [JSONL](mail-06-agent-3/trace.jsonl) |
| mail-07 | agent | 1 | True |  | [JSONL](mail-07-agent-1/trace.jsonl) |
| mail-07 | agent | 2 | True |  | [JSONL](mail-07-agent-2/trace.jsonl) |
| mail-07 | agent | 3 | True |  | [JSONL](mail-07-agent-3/trace.jsonl) |
| mail-08 | agent | 1 | True |  | [JSONL](mail-08-agent-1/trace.jsonl) |
| mail-08 | agent | 2 | True |  | [JSONL](mail-08-agent-2/trace.jsonl) |
| mail-08 | agent | 3 | True |  | [JSONL](mail-08-agent-3/trace.jsonl) |
| mail-09 | agent | 1 | True |  | [JSONL](mail-09-agent-1/trace.jsonl) |
| mail-09 | agent | 2 | True |  | [JSONL](mail-09-agent-2/trace.jsonl) |
| mail-09 | agent | 3 | True |  | [JSONL](mail-09-agent-3/trace.jsonl) |
| mail-10 | agent | 1 | True |  | [JSONL](mail-10-agent-1/trace.jsonl) |
| mail-10 | agent | 2 | True |  | [JSONL](mail-10-agent-2/trace.jsonl) |
| mail-10 | agent | 3 | True |  | [JSONL](mail-10-agent-3/trace.jsonl) |
| mail-11 | agent | 1 | True |  | [JSONL](mail-11-agent-1/trace.jsonl) |
| mail-11 | agent | 2 | True |  | [JSONL](mail-11-agent-2/trace.jsonl) |
| mail-11 | agent | 3 | True |  | [JSONL](mail-11-agent-3/trace.jsonl) |
| mail-12 | agent | 1 | True |  | [JSONL](mail-12-agent-1/trace.jsonl) |
| mail-12 | agent | 2 | True |  | [JSONL](mail-12-agent-2/trace.jsonl) |
| mail-12 | agent | 3 | True |  | [JSONL](mail-12-agent-3/trace.jsonl) |
| order-transient | agent | 1 | True |  | [JSONL](order-transient-agent-1/trace.jsonl) |
| order-transient | agent | 2 | True |  | [JSONL](order-transient-agent-2/trace.jsonl) |
| order-transient | agent | 3 | True |  | [JSONL](order-transient-agent-3/trace.jsonl) |
| order-unavailable | agent | 1 | True |  | [JSONL](order-unavailable-agent-1/trace.jsonl) |
| order-unavailable | agent | 2 | True |  | [JSONL](order-unavailable-agent-2/trace.jsonl) |
| order-unavailable | agent | 3 | True |  | [JSONL](order-unavailable-agent-3/trace.jsonl) |
| order-shipped | agent | 1 | True |  | [JSONL](order-shipped-agent-1/trace.jsonl) |
| order-shipped | agent | 2 | True |  | [JSONL](order-shipped-agent-2/trace.jsonl) |
| order-shipped | agent | 3 | True |  | [JSONL](order-shipped-agent-3/trace.jsonl) |
| order-missing-shipping | agent | 1 | True |  | [JSONL](order-missing-shipping-agent-1/trace.jsonl) |
| order-missing-shipping | agent | 2 | True |  | [JSONL](order-missing-shipping-agent-2/trace.jsonl) |
| order-missing-shipping | agent | 3 | True |  | [JSONL](order-missing-shipping-agent-3/trace.jsonl) |
| critical-boundary | agent | 1 | True |  | [JSONL](critical-boundary-agent-1/trace.jsonl) |
| critical-boundary | agent | 2 | True |  | [JSONL](critical-boundary-agent-2/trace.jsonl) |
| critical-boundary | agent | 3 | True |  | [JSONL](critical-boundary-agent-3/trace.jsonl) |

Незапущенные попытки: 0

```json
{
  "agent": {
    "passed": 51,
    "launched": 51,
    "planned": 51,
    "complete_groups": 17,
    "cases": 17,
    "pass_at_3": 1.0,
    "pass_power_3": 1.0,
    "model_calls": 232,
    "unknown_call_count_attempts": 0,
    "known_total_tokens": 626709,
    "known_prompt_tokens": 611979,
    "known_completion_tokens": 14518,
    "mean_elapsed_seconds": 22.904450980392156,
    "unknown_usage_attempts": 0,
    "diagnostics": {
      "blocked_attempts": 15,
      "budget_stops": 0,
      "false_handoffs": 0,
      "false_refusals": 0,
      "false_routes": 0,
      "forbidden_content_effects": 0,
      "human_handoffs": 17,
      "invalid_calls": 10,
      "justified_handoffs": 17,
      "planned_technical_retries": 8,
      "repeated_argument_calls": 0,
      "routes": 36
    },
    "false_route_fraction": 0.0,
    "justified_handoff_fraction": 1.0
  }
}
```

null означает, что три повтора ещё не завершены. API-ошибки входят в число запущенных.
Offline-подстановки проверяют инфраструктуру, а не качество модели. Доставка существует только в SQLite.
