# Сравнение: development

Реальная модель: False

| Письмо | Путь | Режим | Ожидается | Фактически | Успех | Ошибки | Вызовы модели | Токены | Время, с | Блокировки |
|---|---|---|---|---|---|---|---:|---:|---:|---:|
| mail-01 | thread_contract_expired | workflow | marina | marina | True |  | 1 | — | 0.005 | 0 |
| mail-01 | thread_contract_expired | agent | marina | marina | True |  | 7 | — | 0.011 | 0 |
| mail-02 | paid_not_shipped | workflow | order_operations | order_operations | True |  | 1 | — | 0.006 | 0 |
| mail-02 | paid_not_shipped | agent | order_operations | order_operations | True |  | 5 | — | 0.008 | 0 |
| mail-03 | shipped_logistics | workflow | logistics | logistics | True |  | 1 | — | 0.004 | 0 |
| mail-03 | shipped_logistics | agent | logistics | logistics | True |  | 5 | — | 0.008 | 0 |
| mail-04 | existing_ticket | workflow | sergey | sergey | True |  | 1 | — | 0.004 | 0 |
| mail-04 | existing_ticket | agent | sergey | sergey | True |  | 4 | — | 0.006 | 0 |
| mail-05 | thread_approval | workflow | sales_north | sales_north | True |  | 1 | — | 0.004 | 0 |
| mail-05 | thread_approval | agent | sales_north | sales_north | True |  | 5 | — | 0.009 | 0 |
| mail-06 | identity_unverified | workflow | identity | clarification:company_identifier | True |  | 1 | — | 0.003 | 0 |
| mail-06 | identity_unverified | agent | identity | clarification:company_identifier+equipment_details | True |  | 3 | — | 0.004 | 0 |
| mail-07 | two_issues | workflow | sales_orion, order_operations | sales_orion, order_operations | True |  | 1 | — | 0.006 | 0 |
| mail-07 | two_issues | agent | sales_orion, order_operations | sales_orion, order_operations | True |  | 8 | — | 0.014 | 0 |
| mail-08 | forbidden_external_contract | workflow | forbidden | forbidden_request | True |  | 1 | — | 0.002 | 0 |
| mail-08 | forbidden_external_contract | agent | forbidden | forbidden_request | True |  | 2 | — | 0.003 | 0 |

workflow: 8/8 запущенных; запланировано 8.

agent: 8/8 запущенных; запланировано 8.
