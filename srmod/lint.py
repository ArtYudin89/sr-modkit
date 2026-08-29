"""`srmod lint` — статические проверки .rsm на грабли rsmc из docs/STAGE0.md (б):

1. `DText(...)` внутри `code:` у `dialogMsg`. rsmc его НЕ выбрасывает, но
   (сверено побайтово и через RScript --cli -d, 2026-08-04): строковый литерал
   внутри CT(...) перекеивается как ТЕКСТ, получается двойной лукап
   `DText(CT(CT("Script.X.M")))`, а из text: (даже пустого) генерится ещё один
   DText первым оператором. Работает это только пока таблица rsmc не слита в ту
   же секцию Lang (перетрёт оригинальные ключи) — для авторского мода текст
   должен жить в text:, для импортированного — сборка rson-веткой.
2. `state("12", ...)` — цифровое имя состояния: `ChangeState(<число>)`
   резолвится ПО ИМЕНИ, если состояние с таким именем существует, и такое имя
   молча подменяет резолв по индексу (проверено на `Mod_RevDiplomat`).

Оба факта добыты руками прогоном корпуса ([[project_scr_decompiler]]),
здесь — только их статическое обнаружение, не переоткрытие.

Поверх этого — проверки по машинному описанию языка (`srmod/dsl.py`,
`data/rsm-dsl.json` из самого rsmc): неизвестные поля деклараций, отсутствие
обязательных, негодные значения перечислений. Отключаются `--no-dsl`, если
чужой мод пользуется тем, чего в описании нет.
"""
import re
from pathlib import Path

from .dsl import ERROR, WARN, check_declarations

_DIALOGMSG_RE = re.compile(r'dialogMsg\s*\(\s*"([^"]*)"\s*,\s*\{')
_DECL_DIGIT_RE = re.compile(r'\bstate\s*\(\s*"(\d+)"')
_DTEXT_RE = re.compile(r'\bDText\s*\(')


def _extract_braced(text, open_brace_pos):
    """text[open_brace_pos] == '{'; вернуть substring до парной '}' (включительно)."""
    depth = 0
    for i in range(open_brace_pos, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_brace_pos:i + 1]
    return text[open_brace_pos:]  # несбалансировано — вернуть до конца, само по себе повод для варнинга


def _line_of(text, pos):
    return text.count('\n', 0, pos) + 1


def lint_rsm_text(path, text, use_dsl=True):
    """-> [(line, severity, message)] (severity: 'ошибка' | 'внимание')."""
    problems = []
    for m in _DIALOGMSG_RE.finditer(text):
        dmsg_name = m.group(1)
        body = _extract_braced(text, m.end() - 1)
        if not body.endswith('}') or body.count('{') != body.count('}'):
            problems.append((_line_of(text, m.start()), ERROR,
                             f'dialogMsg("{dmsg_name}"): не удалось сбалансировать {{}} '
                             f'(несбалансированные скобки rsmc тоже не парсит)'))
            continue
        code_m = re.search(r'\bcode\s*:\s*function\s*\(\s*\)\s*\{', body)
        if code_m:
            code_body = _extract_braced(body, code_m.end() - 1)
            dt = _DTEXT_RE.search(code_body)
            if dt:
                line = _line_of(text, m.start() + code_m.end() - 1 + dt.start())
                problems.append((line, WARN,
                                 f'dialogMsg("{dmsg_name}"): DText(...) внутри code: — rsmc '
                                 f'перекеит CT-аргумент как текст (двойной CT(CT(...))) и '
                                 f'добавит лишний DText из text:; текст пишите в text:'))
    for m in _DECL_DIGIT_RE.finditer(text):
        problems.append((_line_of(text, m.start()), WARN,
                         f'state("{m.group(1)}"): цифровое имя — ChangeState({m.group(1)}) '
                         f'после сборки резолвится по ИМЕНИ этого состояния, а не по индексу'))
    if use_dsl:
        problems.extend(check_declarations(text))
    problems.sort(key=lambda p: p[0])
    return problems


def lint_project(cfg, use_dsl=True):
    """Возвращает список (path, line, severity, message). Пусто = чисто."""
    script_dir = cfg.src_dir / 'DATA' / 'Script'
    all_problems = []
    if script_dir.is_dir():
        for rsm in sorted(script_dir.rglob('*.rsm')):
            text = rsm.read_text(encoding='utf-8', errors='replace')
            for line, severity, msg in lint_rsm_text(rsm, text, use_dsl=use_dsl):
                all_problems.append((rsm, line, severity, msg))
    return all_problems


def run_lint(cfg, use_dsl=True):
    problems = lint_project(cfg, use_dsl=use_dsl)
    if not problems:
        print('OK: замечаний нет')
        return 0
    for path, line, severity, msg in problems:
        try:
            rel = path.relative_to(cfg.root)
        except ValueError:
            rel = path
        # Формат строки — контракт с расширением (problemMatcher $srmod-lint):
        # `путь:строка: важность: текст`.
        print(f'{rel}:{line}: {severity}: {msg}')
    errors = sum(1 for p in problems if p[2] == ERROR)
    print(f'--- {len(problems)} замечание(й), из них ошибок: {errors}')
    return 1
