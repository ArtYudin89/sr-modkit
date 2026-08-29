# История изменений

## 0.2.0

- Подсказки и hover по 977 встроенным именам языка (функции движка, функции
  UtilityFunctionsPack, константы) — из `data/lexicon.json`.
- Переход к определению (F12): строковая ссылка на декларацию (`ChangeState("StX")`,
  `state: "StX"`) и `import from './файл.rsm'`.
- Подсветка датниковых `.txt` — язык `rangers-dat` для `Lang.txt`, `Main.txt`,
  `CacheData.txt`, `ModuleInfo.txt` и всего, что лежит в `CFG/`.
- Замечания `srmod lint` теперь с важностью: ошибки показываются красным,
  предупреждения жёлтым (в строке линта появился префикс `error:`/`warning:`,
  problem matcher `$srmod-lint` его читает).
- Флаговые поля (`race`, `owner`, `type`…) вставляются сразу списком
  `race: ["…"]`, а hover предупреждает, что строкой значение теряется.
- Исправлены списки значений в заготовках `place` и `state`: там были
  выдуманные `inSpace`/`nearStar`/`attack`, которых компилятор не знает.

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
