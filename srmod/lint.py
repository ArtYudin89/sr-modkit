"""`srmod lint` — статические проверки .rsm на грабли rsmc из docs/STAGE0.md (б):

1. `DText(...)` внутри `code:` у `dialogMsg` молча выбрасывается rsmc (текст
   диалога живёт только в `text:`) — не ошибка сборки, просто немой текст.
2. `state("12", ...)` — цифровое имя состояния: `ChangeState(<число>)`
   резолвится ПО ИМЕНИ, если состояние с таким именем существует, и такое имя
   молча подменяет резолв по индексу (проверено на `Mod_RevDiplomat`).

Оба факта добыты руками прогоном корпуса ([[project_scr_decompiler]]),
здесь — только их статическое обнаружение, не переоткрытие.
"""
import re
from pathlib import Path

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


def lint_rsm_text(path, text):
    problems = []
    for m in _DIALOGMSG_RE.finditer(text):
        dmsg_name = m.group(1)
        body = _extract_braced(text, m.end() - 1)
        if not body.endswith('}') or body.count('{') != body.count('}'):
            problems.append((_line_of(text, m.start()),
                             f'dialogMsg("{dmsg_name}"): не удалось сбалансировать {{}} '
                             f'(несбалансированные скобки rsmc тоже не парсит)'))
            continue
        code_m = re.search(r'\bcode\s*:\s*function\s*\(\s*\)\s*\{', body)
        if code_m:
            code_body = _extract_braced(body, code_m.end() - 1)
            dt = _DTEXT_RE.search(code_body)
            if dt:
                line = _line_of(text, m.start() + code_m.end() - 1 + dt.start())
                problems.append((line,
                                 f'dialogMsg("{dmsg_name}"): DText(...) внутри code: '
                                 f'молча выбрасывается rsmc — текст берётся только из text:'))
    for m in _DECL_DIGIT_RE.finditer(text):
        problems.append((_line_of(text, m.start()),
                         f'state("{m.group(1)}"): цифровое имя — ChangeState({m.group(1)}) '
                         f'после сборки резолвится по ИМЕНИ этого состояния, а не по индексу'))
    return problems


def lint_project(cfg):
    """Возвращает список (path, line, message). Пусто = чисто."""
    root = cfg.root
    script_dir = root / 'DATA' / 'Script'
    all_problems = []
    if script_dir.is_dir():
        for rsm in sorted(script_dir.rglob('*.rsm')):
            text = rsm.read_text(encoding='utf-8', errors='replace')
            for line, msg in lint_rsm_text(rsm, text):
                all_problems.append((rsm, line, msg))
    return all_problems


def run_lint(cfg):
    problems = lint_project(cfg)
    if not problems:
        print('OK: замечаний нет')
        return 0
    for path, line, msg in problems:
        try:
            rel = path.relative_to(cfg.root)
        except ValueError:
            rel = path
        print(f'{rel}:{line}: {msg}')
    print(f'--- {len(problems)} замечание(й)')
    return 1
