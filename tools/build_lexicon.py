#!/usr/bin/env python3
"""Собирает единый lexicon.json для тулинга SR HD из трёх источников.

    1. RangersCode.json (схема подсветки RScript 4.12f) -- авторитетный список
       имён и их категорий: ключевые слова, встроенные функции, кастомные
       функции (UtilityFunctionsPack), константы.
    2. "Script functions list.txt" (мануал по моддингу, UTF-16) -- описания
       функций и их аргументов, включая пометку "Опционально".
    3. Корпус декомпилированных скриптов (*.rson) -- наблюдаемая арность
       вызовов и живые примеры использования.

Результат кормит сразу три вещи: TextMate-грамматику, автодополнение LSP и
линтер арности.

    python tools/build_lexicon.py --report

Пути к источникам по умолчанию берутся относительно этого файла
(../../rson_decompiler), любой можно переопределить ключом.
"""

import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, 'reconfigure'):          # консоль Windows иначе рубит кириллицу
    sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SANDBOX = os.path.dirname(ROOT)
DEFAULT_TOOLS = os.path.join(SANDBOX, 'rson_decompiler')

DEFAULT_SCHEME = os.path.join(DEFAULT_TOOLS, 'RScript_4.12f', 'Schemes', 'RangersCode.json')
DEFAULT_MANUAL = os.path.join(DEFAULT_TOOLS, 'Modding_Manual', 'Script functions list.txt')
DEFAULT_CORPUS = os.path.join(DEFAULT_TOOLS, 'decompile_result')
DEFAULT_OUT = os.path.join(ROOT, 'data', 'lexicon.json')

# Element в схеме -> наш kind
KIND_BY_ELEMENT = {
    'ReservedWord': 'keyword',
    'Method': 'function',
    'MethodCustom': 'function_custom',
    'Attribute': 'constant',
}

# Маркеры мануала (отступ 2 пробела) -- всё, что после них, аргументами не является.
RE_MANUAL_HEADER = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*) - (.*?):?\s*$')
RE_MANUAL_PARAM = re.compile(r'^\s{2,6}(\d+)(?:\s*[-–]\s*|\s*\+\s*[-–]\s*)(.*?)[;:.]?\s*$')
# "2, 3, 4... - сектора для проверки" -- вариадический хвост.
RE_MANUAL_VARIADIC = re.compile(
    r'^\s{2,6}(\d+)(?:\s*,\s*\d+)+\s*(?:\.\.\.|…)?\s*[-–]\s*(.*?)[;:.]?\s*$')
RE_MANUAL_NOARGS = re.compile(r'^\s{2,6}[Бб]ез аргументов\b(.*)$')
RE_MANUAL_OPTIONAL = re.compile(r'^\s{1,4}Опционально\s*:?\s*$')
RE_MANUAL_NOTE = re.compile(r'^\s{1,8}(Примечание|Важно|Пример|Пояснение|Внимание)\s*:?\s*(.*)$')
# Любой другой подзаголовок ("Номера программ:", "Типы кораблей:") закрывает
# список аргументов: дальше идёт перечисление допустимых ЗНАЧЕНИЙ, а его номера
# неотличимы от номеров аргументов.
RE_MANUAL_SUBHEADER = re.compile(r'^\s{1,3}\S.*:\s*$')

RE_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
RE_CALL = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')
RE_STRING = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
RE_LINE_COMMENT = re.compile(r'//[^\n]*')

MAX_EXAMPLES = 3
MAX_EXAMPLE_LEN = 110
# Сколько раз функция должна встретиться в корпусе, чтобы по одним наблюдениям
# разрешить жёсткую диагностику арности (без подтверждения мануалом).
CORPUS_TRUST_CALLS = 20


# --------------------------------------------------------------------------- схема

def load_scheme(path):
    """RangersCode.json -> {name: kind}. Символы и скобки отбрасываем."""
    with io.open(path, encoding='utf-8-sig') as fh:
        scheme = json.load(fh)
    rules = scheme['Highlighter']['MainRules']['SubRules']
    names = {}
    for key_list in rules.get('KeyList', []):
        element = key_list.get('Attributes', {}).get('Element')
        kind = KIND_BY_ELEMENT.get(element)
        if kind is None:                        # Symbol / Brace
            continue
        # KeyList 'C' помечен как ReservedWord, но это именно ключевые слова языка;
        # остальные ReservedWord-списки схемы отличаются Element'ом, не Type'ом.
        for word in key_list.get('Words', []):
            if not word or not (word[0].isalpha() or word[0] == '_'):
                continue
            names.setdefault(word, kind)
    return names


# --------------------------------------------------------------------------- мануал

def _read_manual(path):
    """Мануал лежит в UTF-16; на всякий случай пробуем и cp1251."""
    for encoding in ('utf-16', 'utf-8-sig', 'cp1251'):
        try:
            with io.open(path, encoding=encoding) as fh:
                text = fh.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        if 'Опционально' in text or ' - возвращает' in text:
            return text.splitlines()
    raise SystemExit('не удалось определить кодировку мануала: %s' % path)


def parse_manual(path):
    """Разбирает блоки вида:

        FuncName - что делает:
            1 - первый аргумент;
          Опционально:
            2 - второй аргумент;
          Примечание: ...

    Возвращает {name: {'summary', 'params', 'notes'}}.
    """
    entries = {}
    current = None
    optional_from_here = False
    params_closed = False

    for line in _read_manual(path):
        header = RE_MANUAL_HEADER.match(line)
        if header and not line.startswith(' '):
            name, summary = header.group(1), header.group(2).strip()
            current = {'summary': summary, 'params': [], 'notes': []}
            # дубликаты в мануале есть (функция описана дважды) -- берём первое
            # вхождение, оно полнее, но подклеиваем параметры второго, если у
            # первого их не было.
            if name in entries and entries[name]['params']:
                current = None
            else:
                entries[name] = current
            optional_from_here = False
            params_closed = False
            continue

        if current is None:
            continue

        if RE_MANUAL_OPTIONAL.match(line):
            optional_from_here = True
            params_closed = False
            continue

        note = RE_MANUAL_NOTE.match(line)
        if note:
            body = note.group(2).strip()
            if body:
                current['notes'].append('%s: %s' % (note.group(1), body))
            continue

        noargs = RE_MANUAL_NOARGS.match(line)
        if noargs:
            current['params'] = []
            current['no_args'] = True
            continue

        variadic = RE_MANUAL_VARIADIC.match(line)
        if variadic and not params_closed:
            index = int(variadic.group(1))
            if index == len(current['params']) + 1:
                current['params'].append({
                    'index': index,
                    'optional': optional_from_here,
                    'variadic': True,
                    'description': variadic.group(2).strip(),
                })
            continue

        param = RE_MANUAL_PARAM.match(line)
        if param:
            index = int(param.group(1))
            text = param.group(2).strip()
            # Нумерация с нуля -- это перечисление допустимых значений
            # ("0 - пираты; 1 - разброс цен"), а не список аргументов:
            # настоящие аргументы в мануале всегда нумеруются с единицы.
            if index == 0 and not current['params']:
                params_closed = True
                continue
            # Настоящий аргумент идёт строго следом за предыдущим; всё
            # остальное -- вложенные перечисления внутри описания.
            if params_closed or index != len(current['params']) + 1:
                continue
            current['params'].append({
                'index': index,
                'optional': optional_from_here,
                'description': text,
            })
            continue

        if RE_MANUAL_SUBHEADER.match(line):
            params_closed = True
    return entries


# --------------------------------------------------------------------------- корпус

def _strip_code(line):
    """Убирает строковые литералы и комментарии, чтобы не считать их за код."""
    return RE_LINE_COMMENT.sub('', RE_STRING.sub("''", line))


def _count_args(text, open_paren):
    """Число аргументов верхнего уровня для вызова, у которого '(' на позиции
    open_paren. None -- скобка не закрылась в пределах текста.

    Индексация массивов здесь двумерная -- arr[1,6] -- поэтому запятая считается
    разделителем аргументов только вне квадратных и фигурных скобок.
    """
    depth = 0
    nested = 0
    count = 1
    for pos in range(open_paren, len(text)):
        char = text[pos]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return 0 if not text[open_paren + 1:pos].strip() else count
            if depth < 0:
                return None
        elif char in '[{':
            nested += 1
        elif char in ']}':
            nested -= 1
        elif char == ',' and depth == 1 and nested == 0:
            count += 1
    return None


def _iter_code_blocks(node):
    """Все значения "Code": [...] в дереве rson."""
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key == 'Code' and isinstance(value, list):
                    yield [s for s in value if isinstance(s, str)]
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def scan_corpus(corpus_dir, known, skip):
    """Обходит *.rson и собирает арность и примеры вызовов.

    known -- всё, что есть в схеме (чтобы не считать за неизвестное);
    skip  -- имена, для которых арность бессмысленна (ключевые слова: if, for...).

    Возвращает (arity, examples, unknown_calls, stats).
    """
    arity = defaultdict(Counter)
    examples = defaultdict(list)
    unknown_calls = Counter()
    stats = Counter()

    for dirpath, _dirnames, filenames in os.walk(corpus_dir):
        for filename in filenames:
            if not filename.lower().endswith('.rson'):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with io.open(path, encoding='utf-8-sig') as fh:
                    doc = json.load(fh)
            except (ValueError, OSError, UnicodeDecodeError):
                stats['files_failed'] += 1
                continue
            stats['files'] += 1

            for block in _iter_code_blocks(doc):
                stats['code_lines'] += len(block)
                # Вызов может переноситься на следующую строку, поэтому арность
                # считаем по склеенному блоку, а примеры берём построчно.
                # Комментарии и строки режем ДО склейки: и то и другое в этом
                # языке заканчивается на конце строки (CloseOnEndOfLine в схеме),
                # а после склейки первый же '//' съел бы весь остаток блока.
                joined = ' '.join(_strip_code(line) for line in block)
                for match in RE_CALL.finditer(joined):
                    name = match.group(1)
                    stats['calls'] += 1
                    if name not in known:
                        unknown_calls[name] += 1
                        continue
                    if name in skip:
                        continue
                    count = _count_args(joined, match.end() - 1)
                    if count is not None:
                        arity[name][count] += 1

                for line in block:
                    stripped = line.strip()
                    if not stripped or stripped.startswith('//'):
                        continue
                    if len(stripped) > MAX_EXAMPLE_LEN:
                        continue
                    for match in RE_CALL.finditer(_strip_code(stripped)):
                        name = match.group(1)
                        if name not in known or name in skip:
                            continue
                        bucket = examples[name]
                        if len(bucket) < MAX_EXAMPLES and stripped not in bucket:
                            bucket.append(stripped)
    return arity, examples, unknown_calls, stats


# --------------------------------------------------------------------------- сигнатура

def make_signature(entry):
    """Сводит мануал и корпус в один диапазон арности для линтера.

    max = None означает "без верхней границы" (вариадическая функция).
    lintable = можно ли ругаться на выход за диапазон, или это только подсказка.
    """
    params = entry.get('params')
    arity = entry.get('arity')

    manual_min = manual_max = None
    if params:
        manual_min = sum(1 for p in params if not p['optional'])
        manual_max = None if any(p.get('variadic') for p in params) else len(params)
    elif entry.get('no_args'):
        manual_min = manual_max = 0

    if manual_min is not None and not (arity and arity.get('conflicts_with_manual')):
        return {'min': manual_min, 'max': manual_max,
                'source': 'manual', 'lintable': True}

    if manual_min is not None and arity:
        # Источники разошлись: мануал неполон (частый случай -- незадокументи-
        # рованные опциональные аргументы). Берём объединение и не линтуем.
        low = min(manual_min, arity['min'])
        high = None if manual_max is None else max(manual_max, arity['max'])
        return {'min': low, 'max': high, 'source': 'merged', 'lintable': False}

    if arity:
        calls = sum(arity['observed'].values())
        trusted = arity['fixed'] and calls >= CORPUS_TRUST_CALLS
        return {'min': arity['min'], 'max': arity['max'],
                'source': 'corpus', 'lintable': trusted, 'calls': calls}
    return None


# --------------------------------------------------------------------------- сборка

def build(scheme_path, manual_path, corpus_dir):
    scheme = load_scheme(scheme_path)
    manual = parse_manual(manual_path)
    keywords = {n for n, k in scheme.items() if k == 'keyword'}
    arity, examples, unknown_calls, stats = scan_corpus(corpus_dir, set(scheme), keywords)

    entries = {}
    for name, kind in sorted(scheme.items()):
        entry = {'kind': kind, 'sources': ['scheme']}
        doc = manual.get(name)
        if doc:
            entry['sources'].append('manual')
            entry['summary'] = doc['summary']
            if doc['params']:
                entry['params'] = doc['params']
            if doc.get('no_args'):
                entry['no_args'] = True
            if doc['notes']:
                entry['notes'] = doc['notes']

        observed = arity.get(name)
        if observed:
            entry['sources'].append('corpus')
            entry['arity'] = {
                'min': min(observed),
                'max': max(observed),
                'fixed': len(observed) == 1,
                'observed': {str(k): v for k, v in sorted(observed.items())},
            }
        if examples.get(name):
            entry['examples'] = examples[name]

        # Кросс-проверка двух независимых источников. Расхождение значит либо
        # неполный разбор мануала, либо недокументированные аргументы -- в обоих
        # случаях линтеру нельзя ругаться на арность этой функции.
        if 'params' in entry and 'arity' in entry:
            required = sum(1 for p in entry['params'] if not p['optional'])
            variadic = any(p.get('variadic') for p in entry['params'])
            total = float('inf') if variadic else len(entry['params'])
            if entry['arity']['min'] < required or entry['arity']['max'] > total:
                entry['arity']['conflicts_with_manual'] = True
                entry['arity']['manual_range'] = [required,
                                                  None if variadic else total]

        if kind != 'keyword':
            signature = make_signature(entry)
            if signature:
                entry['signature'] = signature

        entries[name] = entry

    # Мануал описывает и то, чего нет в схеме (параметры датников, читы, теги).
    # Такие записи не годятся для автодополнения кода, но полезны как справка.
    manual_only = sorted(set(manual) - set(scheme))

    lexicon = {
        'schema_version': 1,
        'language': 'rangers-code',
        'sources': {
            'scheme': os.path.basename(scheme_path),
            'manual': os.path.basename(manual_path),
            'corpus': os.path.basename(corpus_dir.rstrip('\\/')),
        },
        'entries': entries,
        'manual_only': manual_only,
        'stats': {
            'entries': len(entries),
            'by_kind': dict(Counter(e['kind'] for e in entries.values())),
            'documented': sum(1 for e in entries.values() if 'summary' in e),
            'with_params': sum(1 for e in entries.values() if 'params' in e),
            'with_arity': sum(1 for e in entries.values() if 'arity' in e),
            'arity_fixed': sum(1 for e in entries.values()
                               if e.get('arity', {}).get('fixed')),
            'arity_cross_checked': sum(1 for e in entries.values()
                                       if 'arity' in e and 'params' in e),
            'arity_conflicts': sum(1 for e in entries.values()
                                   if e.get('arity', {}).get('conflicts_with_manual')),
            'with_signature': sum(1 for e in entries.values() if 'signature' in e),
            'lintable': sum(1 for e in entries.values()
                            if e.get('signature', {}).get('lintable')),
            'corpus_files': stats['files'],
            'corpus_files_failed': stats['files_failed'],
            'corpus_code_lines': stats['code_lines'],
            'corpus_calls': stats['calls'],
            'corpus_calls_unknown': sum(unknown_calls.values()),
            'corpus_unknown_names': len(unknown_calls),
        },
        # Имена, которых нет в схеме: пользовательские функции модов и библиотек.
        # Линтеру нужны как whitelist-подсказка "объявлено в проекте, не опечатка".
        'corpus_unknown_top': [
            {'name': n, 'calls': c} for n, c in unknown_calls.most_common(100)
        ],
    }
    return lexicon


def report(lexicon):
    st = lexicon['stats']
    print('источники : %s | %s | %s' % (lexicon['sources']['scheme'],
                                        lexicon['sources']['manual'],
                                        lexicon['sources']['corpus']))
    print('записей   : %d  %s' % (st['entries'], st['by_kind']))
    print('с описанием              : %d' % st['documented'])
    print('с разобранными аргументами: %d' % st['with_params'])
    print('с арностью из корпуса     : %d (жёстко фиксирована у %d)'
          % (st['with_arity'], st['arity_fixed']))
    print('сверено мануал<->корпус   : %d, расхождений %d'
          % (st['arity_cross_checked'], st['arity_conflicts']))
    print('корпус: %d файлов, %d строк кода, %d вызовов'
          % (st['corpus_files'], st['corpus_code_lines'], st['corpus_calls']))
    covered = st['corpus_calls'] - st['corpus_calls_unknown']
    if st['corpus_calls']:
        print('покрытие лексиконом      : %.1f%% (вне словаря %d имён)'
              % (covered * 100.0 / st['corpus_calls'], st['corpus_unknown_names']))
    print('готовых сигнатур          : %d, из них проверяемых линтером %d'
          % (st['with_signature'], st['lintable']))
    print('только в мануале (справка): %d' % len(lexicon['manual_only']))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--scheme', default=DEFAULT_SCHEME, help='RangersCode.json')
    parser.add_argument('--manual', default=DEFAULT_MANUAL, help='Script functions list.txt')
    parser.add_argument('--corpus', default=DEFAULT_CORPUS, help='каталог с *.rson')
    parser.add_argument('--out', default=DEFAULT_OUT, help='куда писать lexicon.json')
    parser.add_argument('--report', action='store_true', help='печатать сводку')
    args = parser.parse_args()

    for path in (args.scheme, args.manual):
        if not os.path.isfile(path):
            raise SystemExit('нет файла: %s' % path)
    if not os.path.isdir(args.corpus):
        raise SystemExit('нет каталога корпуса: %s' % args.corpus)

    lexicon = build(args.scheme, args.manual, args.corpus)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(args.out, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(lexicon, fh, ensure_ascii=False, indent=1, sort_keys=False)
        fh.write('\n')

    if args.report:
        report(lexicon)
        print('записано  : %s (%.1f КБ)'
              % (args.out, os.path.getsize(args.out) / 1024.0))


if __name__ == '__main__':
    main()
