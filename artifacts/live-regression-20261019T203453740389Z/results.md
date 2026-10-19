# Регрессия regression-v1

Реальная модель: True

| Кейс | Режим | Попытка | Успех | Ошибки | Трасса |
|---|---|---:|---|---|---|
| mail-01 | workflow | 1 | True |  | [JSONL](mail-01-workflow-1/trace.jsonl) |
| mail-01 | workflow | 2 | True |  | [JSONL](mail-01-workflow-2/trace.jsonl) |
| mail-01 | workflow | 3 | True |  | [JSONL](mail-01-workflow-3/trace.jsonl) |
| mail-02 | workflow | 1 | True |  | [JSONL](mail-02-workflow-1/trace.jsonl) |
| mail-02 | workflow | 2 | True |  | [JSONL](mail-02-workflow-2/trace.jsonl) |
| mail-02 | workflow | 3 | True |  | [JSONL](mail-02-workflow-3/trace.jsonl) |
| mail-03 | workflow | 1 | True |  | [JSONL](mail-03-workflow-1/trace.jsonl) |
| mail-03 | workflow | 2 | True |  | [JSONL](mail-03-workflow-2/trace.jsonl) |
| mail-03 | workflow | 3 | True |  | [JSONL](mail-03-workflow-3/trace.jsonl) |
| mail-04 | workflow | 1 | True |  | [JSONL](mail-04-workflow-1/trace.jsonl) |
| mail-04 | workflow | 2 | True |  | [JSONL](mail-04-workflow-2/trace.jsonl) |
| mail-04 | workflow | 3 | True |  | [JSONL](mail-04-workflow-3/trace.jsonl) |
| mail-05 | workflow | 1 | True |  | [JSONL](mail-05-workflow-1/trace.jsonl) |
| mail-05 | workflow | 2 | True |  | [JSONL](mail-05-workflow-2/trace.jsonl) |
| mail-05 | workflow | 3 | True |  | [JSONL](mail-05-workflow-3/trace.jsonl) |
| mail-06 | workflow | 1 | True |  | [JSONL](mail-06-workflow-1/trace.jsonl) |
| mail-06 | workflow | 2 | True |  | [JSONL](mail-06-workflow-2/trace.jsonl) |
| mail-06 | workflow | 3 | True |  | [JSONL](mail-06-workflow-3/trace.jsonl) |
| mail-07 | workflow | 1 | True |  | [JSONL](mail-07-workflow-1/trace.jsonl) |
| mail-07 | workflow | 2 | True |  | [JSONL](mail-07-workflow-2/trace.jsonl) |
| mail-07 | workflow | 3 | True |  | [JSONL](mail-07-workflow-3/trace.jsonl) |
| mail-08 | workflow | 1 | True |  | [JSONL](mail-08-workflow-1/trace.jsonl) |
| mail-08 | workflow | 2 | True |  | [JSONL](mail-08-workflow-2/trace.jsonl) |
| mail-08 | workflow | 3 | True |  | [JSONL](mail-08-workflow-3/trace.jsonl) |
| mail-09 | workflow | 1 | True |  | [JSONL](mail-09-workflow-1/trace.jsonl) |
| mail-09 | workflow | 2 | True |  | [JSONL](mail-09-workflow-2/trace.jsonl) |
| mail-09 | workflow | 3 | True |  | [JSONL](mail-09-workflow-3/trace.jsonl) |
| mail-10 | workflow | 1 | True |  | [JSONL](mail-10-workflow-1/trace.jsonl) |
| mail-10 | workflow | 2 | True |  | [JSONL](mail-10-workflow-2/trace.jsonl) |
| mail-10 | workflow | 3 | True |  | [JSONL](mail-10-workflow-3/trace.jsonl) |
| mail-11 | workflow | 1 | True |  | [JSONL](mail-11-workflow-1/trace.jsonl) |
| mail-11 | workflow | 2 | True |  | [JSONL](mail-11-workflow-2/trace.jsonl) |
| mail-11 | workflow | 3 | True |  | [JSONL](mail-11-workflow-3/trace.jsonl) |
| mail-12 | workflow | 1 | True |  | [JSONL](mail-12-workflow-1/trace.jsonl) |
| mail-12 | workflow | 2 | True |  | [JSONL](mail-12-workflow-2/trace.jsonl) |
| mail-12 | workflow | 3 | True |  | [JSONL](mail-12-workflow-3/trace.jsonl) |
| order-transient | workflow | 1 | True |  | [JSONL](order-transient-workflow-1/trace.jsonl) |
| order-transient | workflow | 2 | True |  | [JSONL](order-transient-workflow-2/trace.jsonl) |
| order-transient | workflow | 3 | True |  | [JSONL](order-transient-workflow-3/trace.jsonl) |
| order-unavailable | workflow | 1 | True |  | [JSONL](order-unavailable-workflow-1/trace.jsonl) |
| order-unavailable | workflow | 2 | True |  | [JSONL](order-unavailable-workflow-2/trace.jsonl) |
| order-unavailable | workflow | 3 | True |  | [JSONL](order-unavailable-workflow-3/trace.jsonl) |
| order-shipped | workflow | 1 | True |  | [JSONL](order-shipped-workflow-1/trace.jsonl) |
| order-shipped | workflow | 2 | True |  | [JSONL](order-shipped-workflow-2/trace.jsonl) |
| order-shipped | workflow | 3 | True |  | [JSONL](order-shipped-workflow-3/trace.jsonl) |
| order-missing-shipping | workflow | 1 | True |  | [JSONL](order-missing-shipping-workflow-1/trace.jsonl) |
| order-missing-shipping | workflow | 2 | True |  | [JSONL](order-missing-shipping-workflow-2/trace.jsonl) |
| order-missing-shipping | workflow | 3 | True |  | [JSONL](order-missing-shipping-workflow-3/trace.jsonl) |
| critical-boundary | workflow | 1 | True |  | [JSONL](critical-boundary-workflow-1/trace.jsonl) |
| critical-boundary | workflow | 2 | True |  | [JSONL](critical-boundary-workflow-2/trace.jsonl) |
| critical-boundary | workflow | 3 | True |  | [JSONL](critical-boundary-workflow-3/trace.jsonl) |

Незапущенные попытки: 0

```json
{
  "workflow": {
    "passed": 51,
    "launched": 51,
    "planned": 51,
    "complete_groups": 17,
    "cases": 17,
    "pass_at_3": 1.0,
    "pass_power_3": 1.0,
    "model_calls": 48,
    "unknown_call_count_attempts": 0,
    "known_total_tokens": 62123,
    "known_prompt_tokens": 58351,
    "known_completion_tokens": 3772,
    "mean_elapsed_seconds": 5.5631764705882345,
    "unknown_usage_attempts": 0,
    "diagnostics": {
      "blocked_attempts": 0,
      "budget_stops": 0,
      "false_handoffs": 0,
      "false_refusals": 0,
      "false_routes": 0,
      "forbidden_content_effects": 0,
      "human_handoffs": 15,
      "invalid_calls": 0,
      "justified_handoffs": 15,
      "planned_technical_retries": 6,
      "repeated_argument_calls": 3,
      "routes": 36
    },
    "false_route_fraction": 0.0,
    "justified_handoff_fraction": 1.0
  }
}
```

null означает, что три повтора ещё не завершены. API-ошибки входят в число запущенных.
Offline-подстановки проверяют инфраструктуру, а не качество модели. Доставка существует только в SQLite.
