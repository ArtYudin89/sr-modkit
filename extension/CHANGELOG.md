# История изменений

## 0.1.0

Первая версия.

- Подсветка `.rsm` (грамматика собирается из схемы RScript и лексикона
  `tools/build_grammar.py`).
- Заготовки кода для всех деклараций языка.
- Подсказки полей и значений по `data/rsm-dsl.json` — данные вынуты из самого
  `rsmc` (`tools/build_dsl_schema.py`), с пометкой полей, которые он не
  проверяет.
- Команды поверх CLI: build, deploy, build+deploy, lint, watch, new, open,
  verify, doctor.
- Замечания `srmod lint` в панели «Проблемы»; задачи VS Code с
  problem matcher `$srmod-lint`.
- Проверка `srmod.json` по JSON-схеме.
