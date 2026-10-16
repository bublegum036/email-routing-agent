# Сравнение: development

Реальная модель: False

| Письмо | Путь | Режим | Успех | Ошибки | Вызовы модели | Токены | Время, с | Блокировки |
|---|---|---|---|---|---:|---:|---:|---:|
| mail-01 | thread_contract_expired | workflow | True |  | 1 | — | 0.005 | 0 |
| mail-01 | thread_contract_expired | agent | True |  | 7 | — | 0.011 | 0 |
| mail-02 | paid_not_shipped | workflow | True |  | 1 | — | 0.004 | 0 |
| mail-02 | paid_not_shipped | agent | True |  | 5 | — | 0.008 | 0 |
| mail-03 | shipped_logistics | workflow | True |  | 1 | — | 0.006 | 0 |
| mail-03 | shipped_logistics | agent | True |  | 5 | — | 0.008 | 0 |
| mail-04 | existing_ticket | workflow | True |  | 1 | — | 0.003 | 0 |
| mail-04 | existing_ticket | agent | True |  | 4 | — | 0.006 | 0 |
| mail-05 | thread_approval | workflow | True |  | 1 | — | 0.004 | 0 |
| mail-05 | thread_approval | agent | True |  | 5 | — | 0.008 | 0 |
| mail-06 | identity_unverified | workflow | True |  | 1 | — | 0.003 | 0 |
| mail-06 | identity_unverified | agent | True |  | 3 | — | 0.004 | 0 |
| mail-07 | two_issues | workflow | True |  | 1 | — | 0.006 | 0 |
| mail-07 | two_issues | agent | True |  | 8 | — | 0.014 | 0 |
| mail-08 | forbidden_external_contract | workflow | True |  | 1 | — | 0.003 | 0 |
| mail-08 | forbidden_external_contract | agent | True |  | 2 | — | 0.003 | 0 |

workflow: 8/8 запущенных; запланировано 8.

agent: 8/8 запущенных; запланировано 8.
