"""`srmod new` — скаффолд мода по раскладке src/build.

По умолчанию — «богатый» шаблон (решение юзера 2026-08-04): модульный скрипт
`<Name>.src/` с комментариями (переменные, мир, состояния, диалоги, функции),
Lang.txt с примером раздела текстов и комментариями (BPE вырезает их при
компиляции — проверено round-trip'ом), Main.txt со связкой, два медиа-объекта
(иконка .png -> .gi и двухкадровая анимация .gai.src/ -> .gai). Всё это
собирается `srmod build` без единой правки; в build/ комментарии не попадают.
`--minimal` — прежний голый скаффолд (Star+Planet+Ship+Group+State).

Комментарии внутри тел function(){} — единственные, что доезжают до .scr:
движок хранит код текстом (так делают и все реальные моды), файлов с
комментариями в build/ от этого не появляется.
"""
import json
import re
import struct
import zlib
from pathlib import Path

from .build import BuildError, _encode_datnik

_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')

# --------------------------------------------------------------------- минимальный

_RSM_MINIMAL = '''scriptName("{name}");

star("MainStar", {{noKling: false, noComeKling: false}});
planet("MainPlanet", {{star: "MainStar", race: ["Maloc"], owner: ["Maloc"], economy: ["Agriculture"], government: ["Anarchy"], rangeMin: 0, rangeMax: 100}});
ship({{star: "MainStar", player: true, count: 1, owner: ["Maloc"], type: ["Ranger"], speedMin: 0, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, strengthMin: 0, strengthMax: 0, ruins: ""}});
group("MainGroup", {{planet: "MainPlanet", state: "MainState", owner: ["Maloc"], type: ["Ranger"], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: true, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""}});
state("MainState", {{move: "none", takeAllItem: false}});

export function onGlobal() {{
  //;
}}
'''

# --------------------------------------------------------------------- богатый

_RSM_MAIN = '''// {name} — мод для Space Rangers HD: A War Apart.
// Точка входа скрипта: имя + подключение модулей (порядок import не важен).
// Сборка: `srmod build` (src/ -> build/), установка: `srmod deploy` (junction в Mods\\).
scriptName("{name}");

import from './vars.rsm';      // переменные
import from './world.rsm';     // звёзды, планеты, корабли, группы
import from './states.rsm';    // состояния — поведение групп
import from './dialogs.rsm';   // диалоги
import from './code.rsm';      // функции, которые вызывает игра
'''

_RSM_VARS = '''// Переменные скрипта. Третий аргумент — начальное значение (строкой).
// globalVar видна и другим скриптам (GetValueFromScript), localVar — только этому.

globalVar("vStage", "int", "0");   // этап сюжета: 0 = не начат, 1 = поговорили
localVar("vTurns", "int", "0");    // счётчик ходов (пример из states.rsm)
'''

_RSM_WORLD = '''// Мир: где что появляется. Списки в опциях — «любой из», игра выбирает сама.

// Звезда, в системе которой всё происходит (имя — для ссылок из этого скрипта).
star("MainStar", {{noKling: false, noComeKling: false}});

// Планета в этой системе.
planet("MainPlanet", {{star: "MainStar", race: ["Maloc", "Peleng", "People", "Fei", "Gaal"], owner: ["GroupToggle", "Maloc", "Peleng", "People", "Fei", "Gaal"], economy: ["Agriculture", "Industrial", "Mixed"], government: ["Anarchy", "Dictatorship", "Monarchy", "Republic", "Democracy"], rangeMin: 0, rangeMax: 100}});

// Требование к кораблю ИГРОКА (player: true): скрипт стартует, когда игрок ему соответствует.
ship({{star: "MainStar", player: true, count: 1, owner: ["Maloc", "Peleng", "People", "Fei", "Gaal"], type: ["Ranger", "Warrior", "Pirate", "Transport", "Liner", "Diplomat"], speedMin: 0, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, strengthMin: 0, strengthMax: 0, ruins: ""}});

// Группа NPC: один рейнджер у планеты, живёт в состоянии StGreeter (states.rsm).
group("Greeter", {{planet: "MainPlanet", state: "StGreeter", owner: ["Maloc", "Peleng", "People", "Fei", "Gaal"], type: ["GroupToggle", "Ranger"], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: false, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""}});
'''

_RSM_STATES = '''// Состояние — поведение группы. onTalk — какой диалог открыть при разговоре;
// code — выполняется каждый ход, пока группа в этом состоянии.

state("StGreeter", {{move: "none", takeAllItem: false, onTalk: "dGreet", code: function() {{
    vTurns = vTurns + 1;   // пример: просто считаем ходы в этом состоянии
}}}});
'''

_RSM_DIALOGS = '''// Диалог: dialog (вход и маршрутизация) -> dialogMsg (реплики NPC)
// -> dialogAnswer (ответы игрока). DChange(N) показывает N-ю dialogMsg,
// DAdd(N) добавляет N-й dialogAnswer к текущей реплике; нумерация — по
// порядку объявления, с нуля.
//
// Текст реплик пишите ТОЛЬКО в text: — сборка сама кладёт его в Lang.dat.
// DText(CT("...")) внутри code: не пишите: rsmc портит такие вызовы
// (двойной CT) — это ловит `srmod lint`.

dialog("dGreet", {{code: function() {{
    if(vStage == 0) {{
      DChange(0);      // первая встреча — показать MsgHello
      exit;
    }}
    DChange(1);        // уже знакомы — показать MsgAgain
    exit;
}}}});

dialogMsg("MsgHello", {{text: "Привет, <Player>! Я первый NPC этого мода. Поговори со мной ещё раз — реплика сменится.", code: function() {{
    vStage = 1;
    DAdd(0);           // предложить ответ AnsBye
}}}});

dialogMsg("MsgAgain", {{text: "Снова ты, <Player>? Мы ведь уже поговорили.", code: function() {{
    DAdd(0);
}}}});

dialogAnswer("AnsBye", {{text: "До связи!", answerCommand: "exit"}});
'''

_RSM_CODE = '''// Функции, которые вызывает сама игра (имена фиксированы):
//   onInit        — при старте скрипта в новой игре;
//   onTurn        — каждый игровой ход;
//   onDialogBegin — при открытии диалога этого скрипта;
//   onGlobal      — каждый ход независимо от состояния скрипта.
// Пустому телу нужен хотя бы комментарий "//;".

export function onInit() {{
  //;
}}

export function onTurn() {{
  //;
}}

export function onDialogBegin() {{
  //;
}}

export function onGlobal() {{
  //;
}}
'''

_LANG_TEMPLATE = '''// Тексты мода. Комментарии можно: при сборке в Lang.dat они вырезаются.
//
// Раздел Script/{name} руками НЕ заводить — его создаёт сборка,
// туда rsmc складывает тексты из text:-полей диалогов.
// Свои разделы — так (из кода: CT("{name}Extra.hint")):
{name}Extra ^{{
    // произвольные ключи-тексты
    hint=Пример текста, который скрипт может взять через CT
}}
'''

_MAIN_TEMPLATE = '''// Подключение скрипта к игре. Без этой связки мод виден в списке модов,
// но скрипт не запускается. Комментарии вырезаются при компиляции в .dat.
Data ^{{
    Script ^{{
        {name}=1,Script.{name}
    }}
}}
'''

# ModuleInfo.txt игра читает как есть (он не компилируется) — комментариев
# сюда не писать. Поля — те же, что у реальных модов (сверено по 550
# установленным): их читает менеджер модов, когда рисует список.
_MODULE_INFO_TEMPLATE = '''Name={name}
Author=
Conflict=
Dependence=
Priority=5
Section=
Languages={languages}
SmallDescription={name} (создан srmod — заполните описание)
FullDescription=Полное описание мода: что делает, как включить.
'''


# --------------------------------------------------------------------- медиа

def _write_png(path, size, pixel_fn):
    """Минимальный RGBA-PNG без Pillow: pixel_fn(x, y) -> (r, g, b, a)."""
    w = h = size
    raw = bytearray()
    for y in range(h):
        raw.append(0)                              # фильтр строки: None
        for x in range(w):
            raw += bytes(pixel_fn(x, y))

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', ihdr)
                     + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
                     + chunk(b'IEND', b''))


def _disc(size, cx, cy, radius, rgb):
    """Пиксель-функция: закрашенный круг с мягким краем на прозрачном фоне."""
    def fn(x, y):
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 > radius * radius:
            return (0, 0, 0, 0)
        shade = 1.0 - (d2 / float(radius * radius)) * 0.55
        return (int(rgb[0] * shade), int(rgb[1] * shade), int(rgb[2] * shade), 255)
    return fn


def _write_sample_media(items_dir, name):
    """Иконка .png -> .gi и двухкадровая анимация .gai.src/ -> .gai.
    Кадры именуются <stem>_NNN.png (это формат srgi); times.json не нужен —
    без него srgi ставит 50 мс на кадр."""
    size = 32
    _write_png(items_dir / f'{name}Icon.png', size,
               _disc(size, size // 2, size // 2, 13, (90, 170, 240)))
    anim = items_dir / f'{name}Anim.gai.src'
    _write_png(anim / f'{name}Anim_000.png', size,
               _disc(size, size // 2, size // 2, 11, (240, 170, 60)))
    _write_png(anim / f'{name}Anim_001.png', size,
               _disc(size, size // 2, size // 2, 14, (240, 120, 40)))


# --------------------------------------------------------------------- скаффолд

def scaffold(dest, name, install=None, primary_lang='Rus', languages=None,
             force=False, minimal=False):
    dest = Path(dest)
    if not _NAME_RE.match(name):
        raise BuildError(f'"{name}": имя мода должно быть [A-Za-z][A-Za-z0-9_]* '
                          f'(это и scriptName, и имя .scr, и CT-префикс)')
    if dest.exists() and any(dest.iterdir()) and not force:
        raise BuildError(f'{dest}: не пусто (--force для игнора)')
    languages = languages or [primary_lang]
    install = install or f'Mods/{name}'
    src = dest / 'src'

    script_dir = src / 'DATA' / 'Script'
    script_dir.mkdir(parents=True, exist_ok=True)
    (src / 'CFG').mkdir(parents=True, exist_ok=True)

    if minimal:
        entry_rel = f'src/DATA/Script/{name}.rsm'
        (script_dir / f'{name}.rsm').write_bytes(
            _RSM_MINIMAL.format(name=name).encode('utf-8'))
    else:
        entry_rel = f'src/DATA/Script/{name}.src/main.rsm'
        module_dir = script_dir / f'{name}.src'
        module_dir.mkdir(parents=True, exist_ok=True)
        for fname, template in (('main.rsm', _RSM_MAIN), ('vars.rsm', _RSM_VARS),
                                ('world.rsm', _RSM_WORLD), ('states.rsm', _RSM_STATES),
                                ('dialogs.rsm', _RSM_DIALOGS), ('code.rsm', _RSM_CODE)):
            (module_dir / fname).write_bytes(template.format(name=name).encode('utf-8'))
        _write_sample_media(src / 'DATA' / 'Items', name)

    for lang in languages:
        lang_dir = src / 'CFG' / lang
        lang_dir.mkdir(parents=True, exist_ok=True)
        lang_text = '' if minimal else _LANG_TEMPLATE.format(name=name)
        _encode_datnik(lang_dir / 'Lang.txt', lang_text, 'utf-16le')

    # Связка "запусти мой скрипт" — без неё мод виден в списке, но скрипт
    # не стартует. Форма списана с реальных модов (Main.dat -> txt через BPE):
    # у всех 4 проверенных скриптовых модов ровно Data ^{ Script ^{ X=1,Script.X } }.
    _encode_datnik(src / 'CFG' / 'Main.txt', _MAIN_TEMPLATE.format(name=name), 'utf-16le')

    _encode_datnik(src / 'ModuleInfo.txt', _MODULE_INFO_TEMPLATE.format(
        name=name, languages=', '.join(languages)), 'utf-16le')

    # build/ генерится с нуля каждой сборкой, .srmod/ — локальное состояние.
    (dest / '.gitignore').write_text('/build/\n/.srmod/\n', encoding='utf-8')

    project = {
        'name': name,
        'install': install,
        'languages': languages,
        'primary_lang': primary_lang,
        'script': {'engine': 'rsmc', 'entry': entry_rel},
        'media': {'gi_format': 'argb', 'gai_format': 'delta'},
        'packages': [],
    }
    (dest / 'srmod.json').write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return dest
