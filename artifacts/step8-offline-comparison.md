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
| workflow / known_total_tokens | 0 | 0 |
| workflow / known_prompt_tokens | 0 | 0 |
| workflow / known_completion_tokens | 0 | 0 |
| workflow / mean_elapsed_seconds | 0.0362156862745098 | 0.03658823529411765 |
| workflow / unknown_usage_attempts | 48 | 48 |
| workflow / diagnostics | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 12, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 15, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} |
| workflow / false_route_fraction | 0.0 | 0.0 |
| workflow / justified_handoff_fraction | 0.8 | 1.0 |
| agent / passed | 48 | 51 |
| agent / launched | 51 | 51 |
| agent / planned | 51 | 51 |
| agent / complete_groups | 17 | 17 |
| agent / cases | 17 | 17 |
| agent / pass_at_3 | 0.9411764705882353 | 1.0 |
| agent / pass_power_3 | 0.9411764705882353 | 1.0 |
| agent / model_calls | 234 | 231 |
| agent / unknown_call_count_attempts | 0 | 0 |
| agent / known_total_tokens | 0 | 0 |
| agent / known_prompt_tokens | 0 | 0 |
| agent / known_completion_tokens | 0 | 0 |
| agent / mean_elapsed_seconds | 0.043529411764705886 | 0.04309803921568627 |
| agent / unknown_usage_attempts | 48 | 48 |
| agent / diagnostics | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 12, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} | {'blocked_attempts': 0, 'budget_stops': 0, 'false_handoffs': 0, 'false_refusals': 0, 'false_routes': 0, 'forbidden_content_effects': 0, 'human_handoffs': 15, 'invalid_calls': 0, 'justified_handoffs': 15, 'planned_technical_retries': 6, 'repeated_argument_calls': 3, 'routes': 36} |
| agent / false_route_fraction | 0.0 | 0.0 |
| agent / justified_handoff_fraction | 0.8 | 1.0 |

Исправились: [["order-missing-shipping", "agent", 1], ["order-missing-shipping", "agent", 2], ["order-missing-shipping", "agent", 3], ["order-missing-shipping", "workflow", 1], ["order-missing-shipping", "workflow", 2], ["order-missing-shipping", "workflow", 3]]
Регрессии: []
Без пары: []

Provider model alias may change; this comparison is observational, not proof of causality.
