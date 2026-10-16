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
| mail-03 | shipped_logistics | agent | logistics | logistics | True |  | 4 | 9663 | 16.492 | 0 |
| mail-04 | existing_ticket | workflow | sergey | sergey | True |  | 1 | 1269 | 4.973 | 0 |
| mail-04 | existing_ticket | agent | sergey | sergey | True |  | 5 | 12564 | 25.378 | 1 |
| mail-05 | thread_approval | workflow | sales_north | sales_north | True |  | 1 | 1371 | 4.556 | 0 |
| mail-05 | thread_approval | agent | sales_north | sales_north | True |  | 8 | 27163 | 40.205 | 2 |
| mail-06 | identity_unverified | workflow | identity | clarification:company_identifier | True |  | 1 | 1265 | 5.111 | 0 |
| mail-06 | identity_unverified | agent | identity | identity_unverified | True |  | 3 | 6141 | 14.932 | 0 |
| mail-07 | two_issues | workflow | sales_orion, order_operations | sales_orion, order_operations | True |  | 1 | 1305 | 4.778 | 0 |
| mail-07 | two_issues | agent | sales_orion, order_operations | sales_orion, order_operations | True |  | 8 | 28412 | 40.237 | 1 |
| mail-08 | forbidden_external_contract | workflow | forbidden | forbidden_request | True |  | 1 | 1286 | 4.973 | 0 |
| mail-08 | forbidden_external_contract | agent | forbidden | forbidden_request | True |  | 2 | 3559 | 9.925 | 0 |

workflow: 8/8 запущенных; уникальных писем запланировано 8.

agent: 8/9 запущенных; уникальных писем запланировано 8.

Повторные попытки включены в число запусков. Исходные трассы доступны по полю trace в summary.json.
Пауза между началами запросов, с: 5
