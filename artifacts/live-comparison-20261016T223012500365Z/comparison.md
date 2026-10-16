# Сравнение: development

Реальная модель: True

| Письмо | Путь | Режим | Ожидается | Фактически | Успех | Ошибки | Вызовы модели | Токены | Время, с | Блокировки |
|---|---|---|---|---|---|---|---:|---:|---:|---:|
| mail-01 | thread_contract_expired | workflow | marina | marina | True |  | 1 | 1362 | 1.807 | 0 |
| mail-01 | thread_contract_expired | agent | marina | marina | True |  | 9 | 30731 | 13.699 | 2 |
| mail-02 | paid_not_shipped | workflow | order_operations | order_operations | True |  | 1 | 1271 | 1.622 | 0 |
| mail-02 | paid_not_shipped | agent | order_operations | order_operations | True |  | 4 | 9636 | 6.12 | 0 |
| mail-03 | shipped_logistics | workflow | logistics | logistics | True |  | 1 | 1272 | 1.393 | 0 |
| mail-03 | shipped_logistics | agent | logistics | unsupported_case | False | missing_expected_action, unexpected_action_or_category, wrong_issue_or_mail_count, wrong_recipients_or_action_count, wrong_ticket_count | 1 | — | 0.856 | 0 |

workflow: 3/3 запущенных; запланировано 8.

agent: 2/3 запущенных; запланировано 8.
