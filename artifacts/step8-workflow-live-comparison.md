# Сравнение версий

| Режим / метрика | До | После |
|---|---:|---:|
| workflow / passed | 48 | 51 |
| workflow / launched | 51 | 51 |
| workflow / planned | 51 | 51 |
| workflow / complete_groups | 17 | 17 |
| workflow / cases | 17 | 17 |
| workflow / pass_at_3 | 0.9411764705882353 | 1.0 |
| workflow / pass_power_3 | 0.9411764705882353 | 1.0 |
| workflow / model_calls | 48 | 48 |
| workflow / unknown_call_count_attempts | 0 | 0 |
| workflow / known_total_tokens | 62126 | 62123 |
| workflow / known_prompt_tokens | 58346 | 58351 |
| workflow / known_completion_tokens | 3780 | 3772 |
| workflow / mean_elapsed_seconds | 4.681450980392157 | 5.5631764705882345 |
| workflow / unknown_usage_attempts | 0 | 0 |
| workflow / diagnostics | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 12, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 15, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} |
| workflow / false_route_fraction | 0.0 | 0.0 |
| workflow / justified_handoff_fraction | 0.8 | 1.0 |

Исправились: [["order-missing-shipping", "workflow", 1], ["order-missing-shipping", "workflow", 2], ["order-missing-shipping", "workflow", 3]]
Регрессии: []
Без пары: []

Provider model alias may change; this comparison is observational, not proof of causality.
