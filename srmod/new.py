"""`srmod new` — скаффолд минимального мода по раскладке docs/STAGE0.md (а).

Генерирует ровно то, что `srmod build` умеет собрать без правок: один .rsm
(односкриптовый вариант — точка входа `DATA/Script/<Name>.rsm`, без `.src/`)
с минимальным работающим содержимым (Star+Planet+Ship+Group+State), пустой
Lang.txt на primary_lang и пустой Main.txt. Шаблон .rsm — не выдумка: то же
содержимое (доведённое до минимума) проверено сборкой+декомпиляцией в шаге 2.
"""
import re
from pathlib import Path

from .build import BuildError, _encode_datnik

_RSM_TEMPLATE = '''scriptName("{name}");

star("MainStar", {{noKling: false, noComeKling: false}});
planet("MainPlanet", {{star: "MainStar", race: ["Maloc"], owner: ["Maloc"], economy: ["Agriculture"], government: ["Anarchy"], rangeMin: 0, rangeMax: 100}});
ship({{star: "MainStar", player: true, count: 1, owner: ["Maloc"], type: ["Ranger"], speedMin: 0, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, strengthMin: 0, strengthMax: 0, ruins: ""}});
group("MainGroup", {{planet: "MainPlanet", state: "MainState", owner: ["Maloc"], type: ["Ranger"], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: true, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""}});
state("MainState", {{move: "none", takeAllItem: false}});

export function onGlobal() {{
  //;
}}
'''

_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')

# Заполни Author/описания — это то, что игрок увидит в списке модов.
_MODULE_INFO_TEMPLATE = '''Name={name}
Author=
Conflict=
Dependence=
Priority=5
Section=
Languages={languages}
SmallDescription=
FullDescription=
'''


def scaffold(dest, name, install=None, primary_lang='Rus', languages=None, force=False):
    dest = Path(dest)
    if not _NAME_RE.match(name):
        raise BuildError(f'"{name}": имя мода должно быть [A-Za-z][A-Za-z0-9_]* '
                          f'(это и scriptName, и имя .scr, и CT-префикс)')
    if dest.exists() and any(dest.iterdir()) and not force:
        raise BuildError(f'{dest}: не пусто (--force для игнора)')
    languages = languages or [primary_lang]
    install = install or f'Mods/{name}'

    (dest / 'DATA' / 'Script').mkdir(parents=True, exist_ok=True)
    (dest / 'CFG').mkdir(parents=True, exist_ok=True)

    (dest / 'DATA' / 'Script' / f'{name}.rsm').write_bytes(
        _RSM_TEMPLATE.format(name=name).encode('utf-8'))

    for lang in languages:
        lang_dir = dest / 'CFG' / lang
        lang_dir.mkdir(parents=True, exist_ok=True)
        _encode_datnik(lang_dir / 'Lang.txt', '', 'utf-16le')

    _encode_datnik(dest / 'CFG' / 'Main.txt', 'Data ~{\n}\n', 'utf-16le')

    # Поля — те же, что у реальных модов игры (сверено по 550 установленным):
    # именно их читает менеджер модов, когда рисует список.
    _encode_datnik(dest / 'ModuleInfo.txt', _MODULE_INFO_TEMPLATE.format(
        name=name, languages=', '.join(languages)), 'utf-16le')

    # Только то, что сборка реально читает. `lang.merge_into` и `deploy` из
    # docs/STAGE0.md ещё не реализованы — в шаблон их не пишем, чтобы файл не
    # обещал того, чего нет.
    project = {
        'name': name,
        'install': install,
        'languages': languages,
        'primary_lang': primary_lang,
        'script': {'engine': 'rsmc', 'entry': f'DATA/Script/{name}.rsm'},
        'media': {'gi_format': 'argb', 'gai_format': 'delta'},
        'packages': [],
    }
    import json
    (dest / 'srmod.json').write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return dest
