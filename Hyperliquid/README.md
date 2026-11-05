# Hyperliquid

Сбор данных CoinGlass о позициях Hyperliquid и формирование отчёта.
Команда `/activate` после сбора строк запрашивает публичный `clearinghouseState`
Hyperliquid для каждого уникального адреса и perp DEX. Цена входа берётся из
`entryPx` открытой позиции, совпадающей по адресу, активу и направлению. Она
появляется на листах «Позиции» и «По размеру позиции». Если позиция уже закрыта,
сменила направление или API недоступен, ячейка остаётся пустой; лист «Сводка»
показывает охват проверки. Проверка выполняется при каждом запуске `/activate`.

Основные файлы:

- [coinglass_collector.py](./coinglass_collector.py)
- [liquidation_report.py](./liquidation_report.py)
- [agent_job.py](./agent_job.py)
- [hyperliquid_entry.py](../work/hyperliquid_entry.py)
- [test_report.py](./test_report.py)

Здесь используются ссылки на единственные файлы в `../work/`. Копий кода нет.
