#!/usr/bin/env python3
"""Генерирует TextMate-грамматику языка скриптов SR HD и DSL .rsm.

Списки имён (ключевые слова, функции, константы) берутся из data/lexicon.json,
который собирает build_lexicon.py из схемы подсветки RScript 4.12f. Структура
контекстов (однострочные строки с эскейпами, комментарии // и /* */, директивы
#, фолдинг {}, операторы) срисована с той же схемы (Range/Set/Symbol) и с
dab.sublime-syntax из мануала по моддингу -- у обоих строки и // закрываются
концом строки (CloseOnEndOfLine).

Поверх языка игры грамматика знает конструкции DSL .rsm (RScript 4.14f/rsmc):
scriptName/import from/export function и декларации star/planet/.../dialogMsg.

    python tools/build_grammar.py --report
    python tools/build_grammar.py --check          # смоук по корпусу .rsm

Смоук-проверка -- упрощённый TextMate-токенизатор: каждый непробельный символ
корпуса обязан попасть в какой-нибудь паттерн грамматики; непокрытые регионы
печатаются и дают ненулевой код возврата.
"""

import argparse
import io
import json
import os
import re
import sys

if hasattr(sys.stdout, 'reconfigure'):          # консоль Windows иначе рубит кириллицу
    sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SANDBOX = os.path.dirname(ROOT)

DEFAULT_LEXICON = os.path.join(ROOT, 'data', 'lexicon.json')
DEFAULT_OUT = os.path.join(ROOT, 'syntaxes', 'rangers-script.tmLanguage.json')
DEFAULT_CHECK_DIR = os.path.join(
    SANDBOX, 'rson_decompiler', '_rsm_regress_out', 'work',
    'decompile_result_Mod_RevDiplomat_Mod_RevDiplomat.rson', 'mod')

SCOPE = 'rangers-script'

# Разбиение 16 ключевых слов схемы на TextMate-категории. Сверяется с лексиконом
# при сборке: новое слово в схеме обязано попасть в одну из групп.
TYPE_KEYWORDS = ['int', 'float', 'dword', 'str', 'unknown', 'ref']
CONTROL_KEYWORDS = ['break', 'continue', 'else', 'for', 'if', 'exit', 'while',
                    'result', 'throw']
# 'function' оформляется отдельным паттерном (имя после него -- entity.name).

# Декларации DSL .rsm (docs/STAGE0.md, README rsmc). Матчатся только перед '(',
# чтобы не красить одноимённые ключи опций и переменные.
RSM_DECLARATIONS = [
    'scriptName', 'globalVar', 'localVar', 'constellation', 'star', 'starLink',
    'planet', 'ship', 'group', 'groupLink', 'place', 'item', 'state',
    'dialog', 'dialogMsg', 'dialogAnswer', 'ether',
]
RSM_IMPORT_KEYWORDS = ['import', 'from', 'export']


def names_by_kind(lexicon):
    kinds = {}
    for name, entry in lexicon['entries'].items():
        kinds.setdefault(entry['kind'], []).append(name)
    return kinds


def alternation(names):
    """Альтернатива для oniguruma: длинные имена первыми, чтобы не полагаться
    на бэктрекинг при общих префиксах (Ship / ShipType)."""
    return '|'.join(sorted(names, key=lambda n: (-len(n), n)))


def scoped(name):
    return '%s.%s' % (name, SCOPE)


def build_grammar(lexicon):
    kinds = names_by_kind(lexicon)

    keywords = set(kinds.get('keyword', []))
    expected = set(TYPE_KEYWORDS) | set(CONTROL_KEYWORDS) | {'function'}
    if keywords != expected:
        raise SystemExit('ключевые слова лексикона разошлись с разбиением: '
                         'лишние %s, потерянные %s'
                         % (sorted(keywords - expected) or '--',
                            sorted(expected - keywords) or '--'))

    string_rule = lambda quote: {
        'name': scoped('string.quoted.single' if quote == "'"
                       else 'string.quoted.double'),
        'begin': quote,
        # Строки в этом языке однострочные (CloseOnEndOfLine в схеме RScript):
        # незакрытая кавычка гаснет на конце строки, а не красит весь файл.
        'end': "%s|$" % quote,
        'patterns': [
            {'name': scoped('constant.character.escape'), 'match': r'\\.'},
        ],
    }

    repository = {
        'comment-block': {
            'name': scoped('comment.block'),
            'begin': r'/\*',
            'end': r'\*/',
        },
        'comment-line': {
            'name': scoped('comment.line.double-slash'),
            'match': '//.*',
        },
        'string-single': string_rule("'"),
        'string-double': string_rule('"'),
        'directive': {
            'name': scoped('meta.preprocessor'),
            'match': '#.*',
        },
        'rsm-import': {
            'name': scoped('keyword.control.import'),
            'match': r'\b(?:%s)\b' % alternation(RSM_IMPORT_KEYWORDS),
        },
        'function-definition': {
            'match': r'\b(function)\s+([A-Za-z_][A-Za-z0-9_]*)',
            'captures': {
                '1': {'name': scoped('storage.type.function')},
                '2': {'name': scoped('entity.name.function')},
            },
        },
        'function-keyword': {
            'name': scoped('storage.type.function'),
            'match': r'\bfunction\b',
        },
        # Ключи в объектах опций DSL: {star: "Star1", count: 1}. Раньше списков
        # имён, иначе ключ count красился бы как встроенная функция count().
        'property-key': {
            'name': scoped('variable.other.property'),
            'match': r'\b[A-Za-z_][A-Za-z0-9_]*(?=\s*:)',
        },
        'rsm-declaration': {
            'name': scoped('storage.type.declaration'),
            'match': r'\b(?:%s)\b(?=\s*\()' % alternation(RSM_DECLARATIONS),
        },
        'keyword-control': {
            'name': scoped('keyword.control'),
            'match': r'\b(?:%s)\b' % alternation(CONTROL_KEYWORDS),
        },
        'storage-type': {
            'name': scoped('storage.type'),
            'match': r'\b(?:%s)\b' % alternation(TYPE_KEYWORDS),
        },
        'boolean': {
            'name': scoped('constant.language.boolean'),
            'match': r'\b(?:true|false)\b',
        },
        'builtin-function': {
            'name': scoped('support.function.builtin'),
            'match': r'\b(?:%s)\b' % alternation(kinds.get('function', [])),
        },
        'custom-function': {
            'name': scoped('support.function.custom'),
            'match': r'\b(?:%s)\b' % alternation(kinds.get('function_custom', [])),
        },
        'builtin-constant': {
            'name': scoped('support.constant'),
            'match': r'\b(?:%s)\b' % alternation(kinds.get('constant', [])),
        },
        'number': {
            'name': scoped('constant.numeric'),
            'match': r'\b[0-9]+\.?[0-9]*\b',
        },
        'operator': {
            'name': scoped('keyword.operator'),
            'match': '[-+*/%=!&|^~<>]',
        },
        'punctuation': {
            'name': scoped('punctuation.section'),
            'match': r'[\[\](){},;.:]',
        },
        # Всё непойманное выше: пользовательские переменные, имена деклараций,
        # групп, состояний. Без этого паттерна смоук-проверка была бы слепа.
        'identifier': {
            'name': scoped('variable.other'),
            'match': r'\b[A-Za-z_][A-Za-z0-9_]*\b',
        },
    }

    order = [
        'comment-block', 'comment-line',
        'string-single', 'string-double',
        'directive',
        'rsm-import', 'function-definition', 'function-keyword',
        'property-key', 'rsm-declaration',
        'keyword-control', 'storage-type', 'boolean',
        'builtin-function', 'custom-function', 'builtin-constant',
        'number', 'operator', 'punctuation', 'identifier',
    ]
    assert sorted(order) == sorted(repository), 'order и repository разошлись'

    return {
        'name': 'Rangers Script (Space Rangers HD)',
        'scopeName': 'source.%s' % SCOPE,
        'fileTypes': ['rsm'],
        'foldingStartMarker': r'\{',
        'foldingStopMarker': r'\}',
        'patterns': [{'include': '#%s' % key} for key in order],
        'repository': repository,
    }


DAT_SCOPE = 'rangers-dat'
DAT_OUT = os.path.join(ROOT, 'syntaxes', 'rangers-dat.tmLanguage.json')


def build_dat_grammar(lexicon):
    """Грамматика текстовых исходников датников (Lang.txt, Main.txt, CacheData.txt).

    Формат — BlockPar: блоки `Имя ^{ … }` (и `~{` — сортированный вариант),
    записи `ключ=значение` и комментарии, которые BPE 1.9 честно вырезает при
    txt→dat. Значения бывают кодом (`CodeBeforeRun` в ML-панелях), поэтому
    ключевые слова языка подсвечиваем теми же списками, что и в .rsm — они из
    схемы RScript, а не выдуманы здесь.
    """
    kinds = names_by_kind(lexicon)
    dat = lambda name: '%s.%s' % (name, DAT_SCOPE)
    repository = {
        'comment-block': {'name': dat('comment.block'), 'begin': r'/\*', 'end': r'\*/'},
        'comment-line': {'name': dat('comment.line.double-slash'), 'match': '//.*'},
        'block-open': {
            # Имя необязательно: в Lang.dat реальных модов есть безымянные
            # блоки — строка вида "    ~{" (проверено на корпусе).
            'match': r'^\s*([^=\s]*)\s*([\^~])(\{)',
            'captures': {
                '1': {'name': dat('entity.name.section')},
                '2': {'name': dat('keyword.operator.block')},
                '3': {'name': dat('punctuation.section.block.begin')},
            },
        },
        'block-close': {
            'name': dat('punctuation.section.block.end'),
            'match': r'^\s*\}\s*$',
        },
        'entry': {
            'match': r'^\s*([^=\r\n]+?)\s*(=)(.*)$',
            'captures': {
                '1': {'name': dat('variable.other.property')},
                '2': {'name': dat('keyword.operator.assignment')},
                '3': {'patterns': [{'include': '#value'}]},
            },
        },
        'value': {
            'patterns': [
                {'name': dat('keyword.control'),
                 'match': r'\b(%s)\b' % alternation(CONTROL_KEYWORDS)},
                {'name': dat('storage.type'),
                 'match': r'\b(%s)\b' % alternation(TYPE_KEYWORDS + ['function'])},
                {'name': dat('support.function'),
                 'match': r'\b(%s)\b' % alternation(kinds.get('function', []))},
                {'name': dat('constant.language'),
                 'match': r'\b(%s)\b' % alternation(kinds.get('constant', []))},
                {'name': dat('constant.numeric'),
                 'match': r'\b\d+(\.\d+)?\b'},
                {'name': dat('string.unquoted'), 'match': r'[^\r\n]+'},
            ],
        },
    }
    order = ['comment-block', 'comment-line', 'block-open', 'block-close', 'entry']
    return {
        'name': 'Rangers Dat (BlockPar text)',
        'scopeName': 'source.%s' % DAT_SCOPE,
        'fileTypes': [],
        'foldingStartMarker': r'[\^~]\{',
        'foldingStopMarker': r'^\s*\}',
        'patterns': [{'include': '#%s' % key} for key in order],
        'repository': repository,
    }


# --------------------------------------------------------------------------- смоук

class _Rule(object):
    """Скомпилированный паттерн грамматики для мини-токенизатора."""

    def __init__(self, node):
        self.name = node.get('name', '<captures>')
        self.begin = re.compile(node['begin']) if 'begin' in node else None
        self.end = re.compile(node['end']) if 'end' in node else None
        self.match = re.compile(node['match']) if 'match' in node else None
        self.inner = [re.compile(p['match']) for p in node.get('patterns', [])]


def _compile_rules(grammar):
    repo = grammar['repository']
    return [_Rule(repo[inc['include'][1:]]) for inc in grammar['patterns']]


def _consume_range(rule, line, start):
    """Тело begin/end-паттерна с позиции start (после begin).

    Возвращает (позиция после end | None если регион уходит на следующие строки).
    Как в TextMate: внутренние паттерны (эскейпы) съедаются раньше end, поэтому
    \\' не закрывает строку.
    """
    pos = start
    while True:
        closing = rule.end.search(line, pos)
        inner = None
        for pat in rule.inner:
            m = pat.search(line, pos)
            if m and (inner is None or m.start() < inner.start()):
                inner = m
        if inner and (closing is None or inner.start() < closing.start()):
            pos = max(inner.end(), pos + 1)
            continue
        if closing:
            return max(closing.end(), pos)      # '$' даёт пустой матч на конце
        return None


def check(grammar, check_dir):
    rules = _compile_rules(grammar)
    problems = []
    files = lines_total = 0

    for dirpath, _dirnames, filenames in os.walk(check_dir):
        for filename in sorted(filenames):
            if not filename.lower().endswith('.rsm'):
                continue
            path = os.path.join(dirpath, filename)
            with io.open(path, encoding='utf-8-sig') as fh:
                text = fh.read()
            files += 1
            open_block = None                   # (_Rule) многострочного региона
            for lineno, line in enumerate(text.splitlines(), 1):
                lines_total += 1
                pos = 0
                if open_block is not None:
                    closing = open_block.end.search(line)
                    if closing is None:
                        continue
                    pos = closing.end()
                    open_block = None
                while pos < len(line):
                    if line[pos].isspace():
                        pos += 1
                        continue
                    best = best_rule = None
                    for rule in rules:
                        regex = rule.begin or rule.match
                        m = regex.search(line, pos)
                        if m and (best is None or m.start() < best.start()):
                            best, best_rule = m, rule
                            if m.start() == pos:
                                break
                    if best is None:
                        problems.append((path, lineno, pos + 1, line[pos:]))
                        break
                    if best.start() > pos:
                        problems.append((path, lineno, pos + 1,
                                         line[pos:best.start()]))
                    if best_rule.begin is not None:
                        after = _consume_range(best_rule, line, best.end())
                        if after is None:
                            open_block = best_rule
                            break
                        pos = after
                    else:
                        pos = max(best.end(), best.start() + 1)
    return files, lines_total, problems


# --------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--lexicon', default=DEFAULT_LEXICON, help='lexicon.json')
    parser.add_argument('--out', default=DEFAULT_OUT, help='куда писать грамматику')
    parser.add_argument('--dat-out', default=DAT_OUT,
                        help='куда писать грамматику датниковых .txt')
    parser.add_argument('--check', nargs='?', const=DEFAULT_CHECK_DIR, default=None,
                        metavar='DIR', help='смоук-токенизация *.rsm в каталоге')
    parser.add_argument('--report', action='store_true', help='печатать сводку')
    args = parser.parse_args()

    if not os.path.isfile(args.lexicon):
        raise SystemExit('нет файла: %s (сначала build_lexicon.py)' % args.lexicon)
    with io.open(args.lexicon, encoding='utf-8') as fh:
        lexicon = json.load(fh)

    grammar = build_grammar(lexicon)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(args.out, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(grammar, fh, ensure_ascii=False, indent=1)
        fh.write('\n')

    dat_grammar = build_dat_grammar(lexicon)
    with io.open(args.dat_out, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(dat_grammar, fh, ensure_ascii=False, indent=1)
        fh.write('\n')

    if args.report:
        kinds = names_by_kind(lexicon)
        print('лексикон  : %s' % ', '.join(
            '%s=%d' % (k, len(v)) for k, v in sorted(kinds.items())))
        print('деклараций .rsm: %d' % len(RSM_DECLARATIONS))
        print('паттернов : %d' % len(grammar['patterns']))
        print('записано  : %s (%.1f КБ)'
              % (args.out, os.path.getsize(args.out) / 1024.0))
        print('датники   : %s (%.1f КБ)'
              % (args.dat_out, os.path.getsize(args.dat_out) / 1024.0))

    if args.check is not None:
        if not os.path.isdir(args.check):
            raise SystemExit('нет каталога для проверки: %s' % args.check)
        files, lines, problems = check(grammar, args.check)
        print('смоук     : %d файлов, %d строк, непокрытых регионов %d'
              % (files, lines, len(problems)))
        for path, lineno, col, snippet in problems[:20]:
            print('  %s:%d:%d  %r' % (path, lineno, col, snippet[:60]))
        if problems:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
