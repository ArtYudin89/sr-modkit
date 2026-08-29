# -*- coding: utf-8 -*-
"""Разбор деклараций `.rsm` и проверка их по машинному описанию языка.

Описание — `data/rsm-dsl.json`, вынутое из самого `rsmc.exe`
(`tools/build_dsl_schema.py`): поля деклараций, обязательные поля, формы
элементов массивов и значения перечислений, добытые опросом живого компилятора.
Здесь ничего не захардкожено — модуль только сверяет текст со схемой.

Зачем это в Python, а не в расширении: `srmod lint` работает и в CLI (и в CI),
а расширение обязано оставаться тонкой обёрткой над ним (docs/EXTENSION.md).

Разделение важности:
- поле помечено `validated: true` — rsmc сам отвергнет негодное значение,
  расхождение со схемой значит «сборка упадёт» ⇒ `ошибка`;
- `validated: false` (см. `unvalidatedFields`) — rsmc принимает ЛЮБУЮ строку и
  молчит, ломается уже игра ⇒ `внимание`: список значений тут не исчерпывающий,
  выдавать его за истину нельзя.
"""
import difflib
import json
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'data' / 'rsm-dsl.json'

# Метки важности печатаются в строку замечания (`путь:строка: error: текст`) и
# разбираются problemMatcher'ом VS Code — тот понимает только английские
# `error`/`warning`, поэтому метки английские, а текст остаётся русским.
ERROR = 'error'
WARN = 'warning'

_IDENT_CALL_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')
# Без `^`: паттерн применяется через .match(text, pos, endpos), а `^` в середине
# строки не совпадает — с ним объект опций разбирался в пустоту, и линт ругался
# «нет обязательного поля» на совершенно здоровых декларациях.
_KEY_RE = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*:')
_NUMBER_RE = re.compile(r'^[-+]?\d+(\.\d+)?$')

_schema_cache = {}


def load_schema(path=None):
    """Схема языка из data/rsm-dsl.json. Отсутствие файла — не повод падать:
    линт продолжает работать своими регэксповыми проверками."""
    key = str(path or SCHEMA_PATH)
    if key not in _schema_cache:
        try:
            data = json.loads(Path(key).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            data = None
        _schema_cache[key] = data
    return _schema_cache[key]


def mask(text):
    """Содержимое строк и комментариев -> пробелы, длина и переводы строк целы.

    Та же логика, что в extension/src/dsl.js: строки языка ОДНОСТРОЧНЫЕ
    (незакрытая гаснет на конце строки), кавычки-ограничители остаются на месте,
    поэтому по маске можно резать текст, а значения читать из оригинала.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                out[i] = ' '
                i += 1
        elif ch == '/' and i + 1 < n and text[i + 1] == '*':
            out[i] = out[i + 1] = ' '
            i += 2
            while i < n and not (text[i] == '*' and i + 1 < n and text[i + 1] == '/'):
                if text[i] != '\n':
                    out[i] = ' '
                i += 1
            if i < n:
                out[i] = ' '
                if i + 1 < n:
                    out[i + 1] = ' '
                i += 2
        elif ch in '"\'':
            i += 1
            while i < n and text[i] != ch and text[i] != '\n':
                if text[i] == '\\':
                    out[i] = ' '
                    i += 1
                if i < n:
                    out[i] = ' '
                    i += 1
            i += 1
        else:
            i += 1
    return ''.join(out)


def _depths(masked):
    """depth[i] — глубина вложенности (), {}, [] ПЕРЕД символом i."""
    depth = 0
    res = []
    for ch in masked:
        if ch in ')]}':
            depth -= 1
        res.append(depth)
        if ch in '([{':
            depth += 1
    return res


def _match_close(masked, open_pos):
    """Позиция парной закрывающей скобки или None."""
    pairs = {'(': ')', '[': ']', '{': '}'}
    close = pairs[masked[open_pos]]
    depth = 0
    for i in range(open_pos, len(masked)):
        c = masked[i]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
            if depth == 0:
                return i if c == close else None
    return None


def _split_top(masked, start, end):
    """Разбить masked[start:end] по запятым нулевой глубины -> [(a, b), ...]."""
    parts, depth, cur = [], 0, start
    for i in range(start, end):
        c = masked[i]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append((cur, i))
            cur = i + 1
    if masked[cur:end].strip():
        parts.append((cur, end))
    return parts


def _trim(masked, a, b):
    while a < b and masked[a].isspace():
        a += 1
    while b > a and masked[b - 1].isspace():
        b -= 1
    return a, b


def line_of(text, pos):
    return text.count('\n', 0, pos) + 1


class Value(object):
    """Значение поля: строковый литерал, массив, код или «прочее»."""

    def __init__(self, kind, text, pos, items=None):
        self.kind = kind          # 'string' | 'array' | 'code' | 'object' | 'other'
        self.text = text          # содержимое литерала / исходный кусок
        self.pos = pos
        self.items = items or []  # для array — список Value; для object — [(key, Value, pos)]


def _parse_value(text, masked, a, b):
    a, b = _trim(masked, a, b)
    if a >= b:
        return Value('other', '', a)
    ch = masked[a]
    if ch in '"\'':
        close = masked.find(ch, a + 1)
        if close == -1 or close >= b:
            close = b
        return Value('string', text[a + 1:close], a)
    if ch == '[':
        close = _match_close(masked, a)
        close = close if close is not None else b
        items = [_parse_value(text, masked, s, e)
                 for s, e in _split_top(masked, a + 1, close)]
        return Value('array', text[a:b], a, items)
    if ch == '{':
        close = _match_close(masked, a)
        close = close if close is not None else b
        return Value('object', text[a:b], a, _parse_object(text, masked, a + 1, close))
    if masked.startswith('function', a):
        return Value('code', text[a:b], a)
    return Value('other', text[a:b].strip(), a)


def _parse_object(text, masked, a, b):
    """Пары `key: value` объекта опций -> [(key, Value, key_pos), ...]."""
    out = []
    for s, e in _split_top(masked, a, b):
        m = _KEY_RE.match(masked, s, e)
        if not m:
            continue
        out.append((m.group(1), _parse_value(text, masked, m.end(), e), m.start(1)))
    return out


class Decl(object):
    def __init__(self, name, pos, args, options, options_pos):
        self.name = name
        self.pos = pos
        self.args = args                  # позиционные Value (без объекта опций)
        self.options = options            # [(key, Value, key_pos)] или None
        self.options_pos = options_pos


def parse_declarations(text, names):
    """Декларации ВЕРХНЕГО уровня (внутри function(){…} те же имена — обычные
    вызовы, их трогать нельзя)."""
    masked = mask(text)
    depth = _depths(masked)
    decls = []
    for m in _IDENT_CALL_RE.finditer(masked):
        if m.group(1) not in names or depth[m.start(1)] != 0:
            continue
        open_pos = masked.index('(', m.end(1) - 1)
        close = _match_close(masked, open_pos)
        if close is None:
            continue
        args, options, options_pos = [], None, None
        for s, e in _split_top(masked, open_pos + 1, close):
            v = _parse_value(text, masked, s, e)
            if v.kind == 'object':
                options, options_pos = v.items, v.pos
            else:
                args.append(v)
        decls.append(Decl(m.group(1), m.start(1), args, options, options_pos))
    return decls


def _values_of(value):
    """Плоский список строковых значений поля (`owner: ["Fei","Gaal"]` — тоже)."""
    if value.kind == 'string':
        return [(value.text, value.pos)]
    if value.kind == 'array':
        return [(v.text, v.pos) for v in value.items if v.kind == 'string']
    if value.kind == 'other' and value.text and not _NUMBER_RE.match(value.text) \
            and value.text not in ('true', 'false'):
        return [(value.text, value.pos)]
    return []


def _hint(name, known):
    near = difflib.get_close_matches(name, known, n=1, cutoff=0.7)
    return f'; похоже на "{near[0]}"' if near else ''


def _check_enum_value(add, label, key, value, en):
    """Значение перечисления: сперва ФОРМА, потом само значение.

    Форма решает, смотрит ли rsmc на значение вообще (проверено живым
    компилятором, `kind`/`stringIgnored` в схеме генерятся пробой):
    флаговое поле (`race`, `owner`, `type`, …) он проверяет только внутри
    `[...]`, а голую строку глотает молча и теряет — `race: "People"` даёт
    ровно тот же .scr, что `race: "Zzz"`. Скалярное (`move`, `mainType`,
    `place.type`) — наоборот, массив для него синтаксическая ошибка.
    """
    allowed = en.get('values', [])
    validated = en.get('validated', False)
    kind = en.get('kind')

    if kind == 'flags' and value.kind == 'string':
        lost = (' и ТЕРЯЕТ его — в .scr значение не попадёт'
                if en.get('stringIgnored') else '')
        add(value.pos, ERROR,
            f'{label}: {key}="{value.text}" задано строкой, а поле флаговое — '
            f'rsmc проверяет его только в форме массива, голую строку глотает '
            f'молча{lost}; пишите {key}: ["{value.text}"]')
        return
    if kind == 'scalar' and value.kind == 'array':
        add(value.pos, ERROR,
            f'{label}: {key} задано массивом, а поле скалярное — rsmc ответит '
            f'"expected a string, boolean or number literal"')
        return

    for value_text, pos in _values_of(value):
        if value_text in allowed:
            continue
        if validated:
            add(pos, ERROR,
                f'{label}: {key}="{value_text}" — rsmc такое значение не примет '
                f'(допустимы: {", ".join(allowed)}){_hint(value_text, allowed)}')
        else:
            add(pos, WARN,
                f'{label}: {key}="{value_text}" — rsmc это поле НЕ проверяет, '
                f'опечатка молча доедет до игры (встречались: '
                f'{", ".join(allowed)}){_hint(value_text, allowed)}')


def check_declarations(text, schema=None):
    """Проверки по схеме языка -> [(line, severity, message)]."""
    schema = schema if schema is not None else load_schema()
    if not schema:
        return []
    decls_schema = schema.get('declarations', {})
    var_types = schema.get('varTypes', [])
    problems = []

    def add(pos, severity, msg):
        problems.append((line_of(text, pos), severity, msg))

    for d in parse_declarations(text, set(decls_schema)):
        spec = decls_schema[d.name]
        fields = spec.get('fields', [])
        positional = spec.get('positional', [])
        enums = spec.get('enums', {})
        shapes = spec.get('entryShapes', {})
        label = d.name
        if d.args and d.args[0].kind == 'string':
            label = f'{d.name}("{d.args[0].text}")'

        # Позиционные: сверх описанных допустимы только поля, переданные по
        # порядку (`localVar("i", "int", "0")` — init третьим аргументом),
        # и только когда объекта опций нет вовсе.
        limit = len(positional) if d.options is not None else len(positional) + len(fields)
        if len(d.args) > limit:
            add(d.pos, ERROR,
                f'{label}: позиционных аргументов {len(d.args)} при максимуме {limit} '
                f'({d.name}({", ".join(positional) or "…"})'
                + (', {…})' if fields else ')') + ')')

        # Тип переменной — второй позиционный у globalVar/localVar.
        if var_types and len(positional) > 1 and positional[1] == 'type' and len(d.args) > 1:
            for value, pos in _values_of(d.args[1]):
                if value not in var_types:
                    add(pos, ERROR,
                        f'{label}: тип "{value}" неизвестен rsmc '
                        f'(допустимы: {", ".join(var_types)}){_hint(value, var_types)}')

        if d.options is None:
            continue

        strict = spec.get('strictFields')
        seen = set()
        for key, value, key_pos in d.options:
            seen.add(key)
            if key not in fields:
                if strict is False:
                    tail = ('rsmc его молча проигнорирует — поле не действует, '
                            'а сборка пройдёт')
                elif strict:
                    tail = 'rsmc откажется собирать'
                else:
                    tail = 'rsmc такого поля не знает'
                add(key_pos, ERROR,
                    f'{label}: поле "{key}" не известно ({tail}; есть: '
                    f'{", ".join(fields) or "нет полей"}){_hint(key, fields)}')
                continue
            en = enums.get(key)
            if en:
                _check_enum_value(add, label, key, value, en)
            shape = shapes.get(key)
            if shape and value.kind == 'array':
                for entry in value.items:
                    if entry.kind != 'object':
                        continue
                    for k, _v, k_pos in entry.items:
                        if k not in shape:
                            add(k_pos, ERROR,
                                f'{label}: в элементе "{key}" ключ "{k}" не известен rsmc '
                                f'(ожидаются: {", ".join(shape)}){_hint(k, shape)}')

        # Обязательные поля: считаем, что «лишние» позиционные закрывают их по
        # порядку fields (тот же случай localVar-init).
        by_position = set(fields[:max(0, len(d.args) - len(positional))])
        for req in spec.get('required', []):
            if req not in seen and req not in by_position:
                add(d.pos, ERROR, f'{label}: нет обязательного поля "{req}" — rsmc откажется собирать')
    return problems
