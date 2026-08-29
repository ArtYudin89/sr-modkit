# -*- coding: utf-8 -*-
"""Генератор `data/rsm-dsl.json` — машинного описания деклараций языка .rsm.

Откуда данные (ничего не выдумано, оба источника — сам компилятор):

1. **Списки полей** — из таблицы строк `rsmc.exe` (UTF-16LE). Парсер rsmc держит
   имена полей подряд между своими же сообщениями об ошибках: блок каждой
   декларации начинается со строки вида `state(...) needs a name string` и
   тянется до такого же сообщения следующей декларации. Обязательные поля видны
   по сообщениям `X(...) requires a "Y" field`.
2. **Значения перечислений** — опросом живого `rsmc build`: в заведомо рабочий
   минимальный скрипт подставляется негодное значение поля, и из ответа
   `(expected one of: ...)` читается список. Поля, которые rsmc принимает с любым
   значением, помечаются `"validated": false` — по ним подсказки давать можно,
   а ругаться нельзя (проверено 2026-08-04: `item.type`, `item.useless`,
   `ether.type`, `dialogAnswer.answerCommand`, `groupLink.rel1/rel2`,
   `starLink.hole` компилятор не проверяет вообще).

Запуск:  python tools/build_dsl_schema.py [--rsmc PATH] [--out data/rsm-dsl.json]
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, 'reconfigure'):          # консоль Windows иначе рубит кириллицу
    sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SANDBOX = os.path.dirname(ROOT)
DEFAULT_OUT = os.path.join(ROOT, 'data', 'rsm-dsl.json')
DEFAULT_RSMC = os.path.join(SANDBOX, 'rson_decompiler', 'rsmc', 'rsmc.exe')

# Порядок деклараций в таблице строк rsmc — он же порядок разбора в парсере.
DECLARATIONS = ('scriptName', 'constellation', 'star', 'planet', 'ship', 'state',
                'group', 'place', 'item', 'ether', 'groupLink', 'starLink',
                'dialog', 'dialogMsg', 'dialogAnswer', 'globalVar', 'localVar')

# `name` — первый позиционный аргумент, не поле объекта опций; true/false —
# литералы, попадающие в тот же прогон строк.
FIELD_STOPLIST = {'name', 'true', 'false', 'type: "..."', 'unknown'}

# Выравнивающий мусор в секции данных exe (`jj`, `jjjj`, …) выглядит как
# идентификатор и попадает в блоки ship/starLink — отсекаем по виду.
_PADDING_RE = re.compile(r'^j{2,}$')

# Поля-функции: значение — function() { ... }, а не литерал.
CODE_FIELDS = {'code', 'onActCode', 'onTalkCode'}

# Значения, которые rsmc НЕ проверяет, но которые реально встречаются в
# шиппенных модах (снято прогоном по корпусу декомпилированных .rsm 2026-08-04).
# Значения, которые rsmc не проверяет: собраны по корпусу .rsm (счётчики на
# 2026-08-28 — restart 137, exit 96, shop 14, planet 9, block 8, hangar 7,
# fastexit 1). Список наблюдаемый, а не исчерпывающий — линт ругается на
# незнакомое значение только предупреждением.
OBSERVED = {
    'dialogAnswer.answerCommand': ['restart', 'exit', 'shop', 'hangar', 'planet',
                                   'block', 'fastexit'],
}


# --------------------------------------------------------------- таблица строк

def utf16_strings(path):
    """[(offset, text)] — все печатные UTF-16LE строки exe.

    Порог длины — 2 символа, а не 3: иначе теряется поле `at` у item(...)
    (проверено — оно есть и rsmc его валидирует)."""
    data = io.open(path, 'rb').read()
    return [(m.start(), m.group().decode('utf-16le'))
            for m in re.finditer(br'(?:[\x20-\x7e]\x00){2,80}', data)]


_IDENT_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
_NEEDS_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\(\.\.\.\)')
_REQUIRES_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\(\.\.\.\) requires an? "([^"]+)" field')
_UNKNOWN_FIELD_RE = re.compile(r'^unknown ([A-Za-z][A-Za-z0-9_]*)\(\.\.\.\) field')
_ENTRY_REQ_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*) entry requires an? "([^"]+)"')
_ENTRY_ALT_RE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*) entry requires an? "([^"]+)" or "([^"]+)"')


def _positional(needs_msg):
    """`groupLink(...) needs (fromGroup, toGroup, {...}?)` -> [fromGroup, toGroup].

    Позиционные аргументы декларации перечислены в её же сообщении об ошибке,
    так что и они не выдумываются. Перечисление обрывается на первом `{` —
    дальше идёт объект опций (`group(...) needs (name, {planet, state, ...})`),
    и его ключи позиционными аргументами не являются."""
    # `needs (…)`, но и `needs at least (name, type)` у globalVar/localVar.
    m = re.search(r'needs[^(]*\(([^)]*)', needs_msg)
    if not m:
        return ['name'] if 'needs a name' in needs_msg else []
    args = []
    for part in m.group(1).split(','):
        part = part.strip()
        if part.startswith('{'):
            break
        if _IDENT_RE.match(part):
            args.append(part)
    return args


def parse_fields(strings):
    """Поля каждой декларации: идентификаторы между её первым сообщением и
    концом её блока (следующая декларация либо своё же `unknown X(...) field`)."""
    marks = []                                  # (индекс строки, имя, текст сообщения)
    unknown_at = {}                             # декларация -> индекс конца блока
    for i, (_, s) in enumerate(strings):
        m = _NEEDS_RE.match(s)
        if m and m.group(1) in DECLARATIONS:
            if not marks or marks[-1][1] != m.group(1):
                marks.append((i, m.group(1), s))
        u = _UNKNOWN_FIELD_RE.match(s)
        if u and u.group(1) in DECLARATIONS:
            unknown_at.setdefault(u.group(1), i)

    fields, required, positional = {}, {}, {}
    for pos, (i, decl, needs_msg) in enumerate(marks):
        end = marks[pos + 1][0] if pos + 1 < len(marks) else len(strings)
        if decl in unknown_at and i < unknown_at[decl] < end:
            end = unknown_at[decl]
        args = _positional(needs_msg)
        seen = []
        for _, s in strings[i:end]:
            rq = _REQUIRES_RE.match(s)
            if rq and rq.group(1) == decl:
                required.setdefault(decl, [])
                if rq.group(2) not in required[decl]:
                    required[decl].append(rq.group(2))
                continue
            if (_IDENT_RE.match(s) and s not in FIELD_STOPLIST
                    and not _PADDING_RE.match(s)
                    and s not in seen and s not in args):
                seen.append(s)
        # У декларации может быть несколько блоков сообщений (напр. проверки
        # связности в конце файла) — берём первый, он же блок парсера.
        fields.setdefault(decl, seen)
        positional.setdefault(decl, args)

    # globalVar и localVar разбираются одним куском кода: их сообщения стоят
    # вплотную, а имена полей лежат один раз — после второго. Блок globalVar
    # из-за этого пустой, хотя форма у них одна.
    if not fields.get('globalVar') and fields.get('localVar'):
        fields['globalVar'] = list(fields['localVar'])
    return fields, required, positional


def parse_entry_shapes(strings):
    """Форма элементов массивов-полей: `transitions` -> {to, when},
    `answers` -> {name|names, when}. Тоже из сообщений парсера."""
    shapes = {}
    for _, s in strings:
        alt = _ENTRY_ALT_RE.match(s)
        if alt:
            shapes.setdefault(alt.group(1), [])
            for key in (alt.group(2), alt.group(3)):
                if key not in shapes[alt.group(1)]:
                    shapes[alt.group(1)].append(key)
            continue
        req = _ENTRY_REQ_RE.match(s)
        if req:
            shapes.setdefault(req.group(1), [])
            if req.group(2) not in shapes[req.group(1)]:
                shapes[req.group(1)].append(req.group(2))
    return shapes


def parse_free_enums(strings):
    """Перечисления, которые rsmc печатает прямо в тексте ошибки: их можно
    достать без запуска (move/place type/тип переменной/имена экспортов)."""
    out = {}
    for _, s in strings:
        m = re.search(r'expected (?:one of )?([a-zA-Z]+(?:/[a-zA-Z0-9]+)+)\)?$', s)
        if not m:
            continue
        values = m.group(1).split('/')
        if 'move type' in s or s.startswith(' (expected none/move'):
            out['state.move'] = values
        elif 'place type' in s or 'nearPlanet' in s:
            out['place.type'] = values
        elif 'variable type' in s or 'dword' in s:
            out['var.type'] = values
        elif 'onGlobal' in s:
            out['exports'] = values
    return out


# --------------------------------------------------------------- опрос rsmc

_BASE = '''scriptName("Probe");

star("MainStar", {noKling: false, noComeKling: false});
star("MainStar2", {noKling: false, noComeKling: false});
planet("MainPlanet", {star: "MainStar", race: [RACE], owner: [POWNER], economy: [ECONOMY], government: [GOVERNMENT], rangeMin: 0, rangeMax: 100});
ship({star: "MainStar", player: true, count: 1, owner: [SOWNER], type: [STYPE], speedMin: 0, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, strengthMin: 0, strengthMax: 0, ruins: ""});
state("MainState", {move: MOVE, takeAllItem: false});
group("MainGroup", {planet: "MainPlanet", state: "MainState", owner: [GOWNER], type: [GTYPE], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: true, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""});
item("iProbe", {mainType: MAINTYPE, type: ITYPE, size: 10, level: 1, radius: 150, owner: IOWNER, useless: USELESS, at: "MainGroup"});
place("plProbe", {type: PLACETYPE, star: "MainStar", obj1: "MainPlanet", angle: 0, dist: 0.5, radius: 20});
ether("etProbe", {msg: "probe", type: ETHERTYPE, unique: false});
dialog("dProbe", {code: function() {
    exit;
}});
dialogMsg("MsgProbe", {text: "probe", code: function() {
    DAdd(0);
}});
dialogAnswer("AnsProbe", {text: "bye", answerCommand: ANSWERCMD});
group("MainGroup2", {planet: "MainPlanet", state: "MainState", owner: ["Maloc"], type: ["Ranger"], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: false, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""});
groupLink("MainGroup", "MainGroup2", {rel1: REL1, rel2: 50, warWeightMin: 0, warWeightMax: 100});
starLink("MainStar", "MainStar2", {distMin: 0, distMax: 100, hole: HOLE});

export function onGlobal() {
  //;
}
'''

_DEFAULTS = {
    'RACE': '"Maloc"', 'POWNER': '"Maloc"', 'ECONOMY': '"Mixed"',
    'GOVERNMENT': '"Anarchy"', 'SOWNER': '"Maloc"', 'STYPE': '"Ranger"',
    'GOWNER': '"Maloc"', 'GTYPE': '"Ranger"', 'MOVE': '"none"',
    'MAINTYPE': '"Useless"', 'ITYPE': '0', 'IOWNER': '"None"',
    'USELESS': '"Capsule"', 'PLACETYPE': '"nearPlanet"', 'ETHERTYPE': '"Message"',
    'ANSWERCMD': '"exit"', 'REL1': '50', 'HOLE': 'false',
}

_PROBES = {
    'planet.race': 'RACE', 'planet.owner': 'POWNER', 'planet.economy': 'ECONOMY',
    'planet.government': 'GOVERNMENT', 'ship.owner': 'SOWNER', 'ship.type': 'STYPE',
    'group.owner': 'GOWNER', 'group.type': 'GTYPE', 'state.move': 'MOVE',
    'item.mainType': 'MAINTYPE', 'item.type': 'ITYPE', 'item.owner': 'IOWNER',
    'item.useless': 'USELESS', 'place.type': 'PLACETYPE', 'ether.type': 'ETHERTYPE',
    'dialogAnswer.answerCommand': 'ANSWERCMD',
    # rel1 ждёт число, hole — флаг: подставляем строку, чтобы проверить, что
    # rsmc вообще смотрит на тип значения (не смотрит).
    'groupLink.rel1': 'REL1', 'starLink.hole': 'HOLE',
}


def _render(overrides):
    text = _BASE
    vals = dict(_DEFAULTS)
    vals.update(overrides)
    for key, value in vals.items():
        text = text.replace(key, value)
    return text


_SENTINEL = '@@SRMOD@@'


def _render_form(placeholder, literal, as_array):
    """То же, но значение подставляется в ЗАДАННОЙ форме — массивом или голым
    литералом, независимо от того, как плейсхолдер стоит в _BASE.

    Форма решает всё (проверено 2026-08-28 живым rsmc): флаговые поля
    (`race`, `owner`, `type`, `economy`, `government`) компилятор проверяет
    только внутри `[...]`, а голую строку глотает молча и ТЕРЯЕТ; скалярные
    (`move`, `mainType`, `place.type`) — наоборот, массив для них
    синтаксическая ошибка."""
    text = _render({placeholder: _SENTINEL})
    boxed = '[%s]' % _SENTINEL
    value = ('[%s]' % literal) if as_array else literal
    if boxed in text:
        return text.replace(boxed, value)
    return text.replace(_SENTINEL, value)


def _expected_values(msg):
    m = re.search(r'expected one of:?\s*([^\r\n)]*)', msg)
    if m:
        return [v.strip() for v in m.group(1).split(',') if v.strip()]
    m = re.search(r'\(expected ([a-zA-Z]+(?:/[a-zA-Z0-9]+)+)\)', msg)
    if m:
        return m.group(1).split('/')
    return None


def _run_rsmc(rsmc, text, tmp):
    src = os.path.join(tmp, 'main.rsm')
    io.open(src, 'w', encoding='utf-8', newline='\n').write(text)
    proc = subprocess.run([rsmc, 'build', src, '-o', os.path.join(tmp, 'out.scr')],
                          capture_output=True, cwd=tmp)
    blob = (proc.stdout or b'') + (proc.stderr or b'')
    try:
        return proc.returncode, blob.decode('utf-8')
    except UnicodeDecodeError:
        return proc.returncode, blob.decode('cp1251', 'replace')


def probe_enums(rsmc):
    """{'planet.race': {'values': [...], 'kind': 'flags'|'scalar', ...}} +
    список полей, для которых проверки нет ни в одной форме.

    Каждое поле пробуется ДВАЖДЫ — массивом и голой строкой, иначе форма
    подмешивается в вывод: `race: ["Zzz"]` даёт список допустимых значений, а
    `race: "Zzz"` собирается молча. Для флаговых полей дополнительно
    проверяется байтами, теряет ли rsmc голую строку: сборка с годным
    значением сравнивается со сборкой с мусором, и если .scr совпал — значение
    в этой форме не доезжает до мода вообще.
    """
    tmp = tempfile.mkdtemp(prefix='srmod_dsl_')
    out_scr = os.path.join(tmp, 'out.scr')
    rc, msg = _run_rsmc(rsmc, _render({}), tmp)
    if rc != 0:
        raise SystemExit('опорный скрипт не собрался (rc=%d):\n%s' % (rc, msg))

    def scr_hash(text):
        if os.path.exists(out_scr):
            os.remove(out_scr)
        rc_, _msg = _run_rsmc(rsmc, text, tmp)
        if rc_ != 0 or not os.path.exists(out_scr):
            return None
        return hashlib.md5(io.open(out_scr, 'rb').read()).hexdigest()

    bad = '"__srmod_bad__"'
    enums, unvalidated = {}, []
    for label, placeholder in sorted(_PROBES.items()):
        rc_arr, msg_arr = _run_rsmc(rsmc, _render_form(placeholder, bad, True), tmp)
        rc_str, msg_str = _run_rsmc(rsmc, _render_form(placeholder, bad, False), tmp)
        as_array = _expected_values(msg_arr)
        as_string = _expected_values(msg_str)
        if as_array and not as_string:
            entry = {'values': as_array, 'kind': 'flags'}
            good = as_array[1] if len(as_array) > 1 else as_array[0]
            if rc_str == 0:
                h_good = scr_hash(_render_form(placeholder, '"%s"' % good, False))
                h_bad = scr_hash(_render_form(placeholder, bad, False))
                entry['stringIgnored'] = bool(h_good and h_good == h_bad)
            enums[label] = entry
        elif as_string:
            enums[label] = {'values': as_string, 'kind': 'scalar'}
        elif rc_arr == 0 or rc_str == 0:
            unvalidated.append(label)      # rsmc проглотил мусор — проверки нет
        else:
            print('  ? %s: неожиданный ответ rsmc (rc=%d/%d)' % (label, rc_arr, rc_str))
    return enums, unvalidated


_UNKNOWN_PROBE_RE = re.compile(r'unknown [A-Za-z][A-Za-z0-9_]*\(\.\.\.\) field')


def probe_unknown_fields(rsmc):
    """Какие декларации ругаются на неизвестное поле, а какие глотают молча.

    Проверено живым rsmc: сообщение `unknown state(...) field "zzz"` есть в
    таблице строк, но выдаёт его только часть деклараций — `planet`, `group`,
    `star`, `ship` лишнее поле молча игнорируют, и опечатка в имени поля
    доезжает до игры незамеченной. Линт формулирует замечание по этому флагу.
    """
    tmp = tempfile.mkdtemp(prefix='srmod_unk_')
    strict = {}
    base = _render({})
    for decl in DECLARATIONS:
        lines = base.split('\n')
        hit = None
        for i, line in enumerate(lines):
            if line.startswith('%s(' % decl) and '{' in line:
                brace = line.index('{')
                lines[i] = line[:brace + 1] + ' __srmod_zzz: 1,' + line[brace + 1:]
                hit = i
                break
        if hit is None:
            continue                      # декларации нет в опорном скрипте
        rc, msg = _run_rsmc(rsmc, '\n'.join(lines), tmp)
        strict[decl] = bool(rc != 0 and _UNKNOWN_PROBE_RE.search(msg))
    return strict


# --------------------------------------------------------------- сборка

def build(rsmc, out_path):
    strings = utf16_strings(rsmc)
    fields, required, positional = parse_fields(strings)
    shapes = parse_entry_shapes(strings)
    free = parse_free_enums(strings)
    enums, unvalidated = probe_enums(rsmc)
    strict = probe_unknown_fields(rsmc)
    for key, values in free.items():
        # Значения из таблицы строк — только скалярные сообщения вида
        # `(expected none/move/…)`, форма у них по определению не массив.
        enums.setdefault(key, {'values': values, 'kind': 'scalar'})

    declarations = {}
    for decl in DECLARATIONS:
        decl_fields = fields.get(decl, [])
        entry = {'positional': positional.get(decl, []), 'fields': decl_fields}
        if required.get(decl):
            entry['required'] = required[decl]
        entry_shapes = {f: shapes[f] for f in decl_fields if f in shapes}
        if entry_shapes:
            entry['entryShapes'] = entry_shapes
        code = [f for f in decl_fields if f in CODE_FIELDS]
        if code:
            entry['codeFields'] = code
        field_enums = {}
        for f in decl_fields:
            key = '%s.%s' % (decl, f)
            if key in enums:
                field_enums[f] = dict(enums[key], validated=True)
            elif key in OBSERVED:
                field_enums[f] = {'values': OBSERVED[key], 'validated': False}
        if field_enums:
            entry['enums'] = field_enums
        if decl in strict:
            # Ругается ли rsmc на неизвестное поле этой декларации (иначе
            # молча игнорирует — см. probe_unknown_fields).
            entry['strictFields'] = strict[decl]
        declarations[decl] = entry

    data = {
        '_source': 'сгенерировано tools/build_dsl_schema.py из rsmc.exe '
                   '(таблица строк + опрос живого компилятора)',
        'rsmc': os.path.basename(rsmc),
        'declarations': declarations,
        'exports': free.get('exports', []),
        'varTypes': free.get('var.type', []),
        # Поля, значения которых rsmc не проверяет: подсказки давать можно,
        # диагностику "недопустимое значение" — нельзя.
        'unvalidatedFields': sorted(set(unvalidated) | set(OBSERVED)),
    }

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(out_path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rsmc', default=DEFAULT_RSMC, help='путь к rsmc.exe')
    parser.add_argument('--out', default=DEFAULT_OUT, help='куда писать схему')
    args = parser.parse_args()

    if not os.path.exists(args.rsmc):
        raise SystemExit('rsmc.exe не найден: %s (укажите --rsmc)' % args.rsmc)

    data = build(args.rsmc, args.out)
    print('деклараций : %d' % len(data['declarations']))
    flags, ignored = [], []
    for decl, entry in data['declarations'].items():
        marks = ''
        if entry.get('enums'):
            marks = '  перечислений: %d' % len(entry['enums'])
        if entry.get('strictFields') is False:
            marks += '  [неизвестные поля глотает молча]'
        for field, en in (entry.get('enums') or {}).items():
            if en.get('kind') == 'flags':
                flags.append('%s.%s' % (decl, field))
                if en.get('stringIgnored'):
                    ignored.append('%s.%s' % (decl, field))
        print('  %-14s (%s) полей=%-3d req=%-2d%s'
              % (decl, ', '.join(entry['positional']) or '—',
                 len(entry['fields']), len(entry.get('required', [])), marks))
    print('флаговые (только массивом): %s' % ', '.join(flags))
    print('  из них строку молча теряют: %s' % ', '.join(ignored))
    print('экспорты   : %s' % ', '.join(data['exports']))
    print('типы пер-х : %s' % ', '.join(data['varTypes']))
    print('без проверки rsmc: %s' % ', '.join(data['unvalidatedFields']))
    print('записано   : %s' % args.out)


if __name__ == '__main__':
    main()
