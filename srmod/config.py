"""Трёхуровневый конфиг srmod: %APPDATA%\\srmod\\config.json -> srmod.json -> CLI/SRMOD_*.

Пути к чужим бинарникам (rsmc/RScript/BlockParEditor/игра) нигде не хардкожены:
машинный уровень задаёт их явно, автодетект ищет сиблингов sr-modkit/ по той же
конвенции, что tools/build_lexicon.py (../rson-decompiler, ../rson_decompiler),
а поверх всего — CLI-флаги и переменные окружения SRMOD_*. Подробности и грабли
(зачем rson-decompiler первым, а не rson_decompiler) — в docs/STAGE0.md (в).
"""
import json
import os
from pathlib import Path

TOOL_KEYS = ('rsmc', 'rscript', 'blockpar', 'game', 'decompiler')

# Код правится только в дефисном клоне (git), поэтому для импорта run.py/srgi.py/
# srpkg.py он первый; сами бинарники (rsmc/RScript 4.14f/BPE 2.0) лежат только в
# подчёркивании — см. project_scr_decompiler. Оба каталога всё равно проверяются
# для КАЖДОГО инструмента по отдельности, порядок влияет лишь при совпадении.
_SIBLINGS = ('rson-decompiler', 'rson_decompiler')
_RSCRIPT_ORDER = ('RScript_4.14f', 'RScript_4.13f', 'RScript_4.12f',
                   'RScript_4.10f', 'RScript_4.5f')
_BLOCKPAR_ORDER = ('BlockParEditor_1.9', 'BlockParEditor_2.0')

DEFAULT_PROJECT = {
    'name': None,
    'install': None,
    'languages': ['Rus'],
    'primary_lang': 'Rus',
    'script': {'engine': 'rsmc', 'entry': None},
    'lang': {'merge_into': 'txt'},
    'media': {'gi_format': 'argb', 'gai_format': 'delta'},
    'packages': [],
    'deploy': {'mode': 'junction'},
}


def machine_config_path():
    appdata = os.environ.get('APPDATA')
    base = Path(appdata) if appdata else Path.home() / 'AppData' / 'Roaming'
    return base / 'srmod' / 'config.json'


def _load_json(path):
    try:
        # utf-8-sig: srmod.json может нести BOM (PowerShell `Set-Content -Encoding
        # utf8` пишет его по умолчанию) — без -sig json.loads падает на первом
        # символе и мы молча получаем {} вместо реального содержимого.
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def sandbox_root(start=None):
    """Каталог, содержащий sr-modkit/ и его сиблингов (обычно c:\\claude_sandbox)."""
    here = Path(start).resolve() if start else Path(__file__).resolve()
    # srmod/config.py -> srmod/ -> sr-modkit/ -> sandbox/
    return here.parent.parent.parent


def _find_in_siblings(sandbox, rel_globs):
    """Первое совпадение rel_globs (relative glob-паттерны) среди сиблингов sandbox."""
    for sib in _SIBLINGS:
        base = sandbox / sib
        if not base.is_dir():
            continue
        for rel in rel_globs:
            for p in sorted(base.glob(rel)):
                if p.exists():
                    return p
    return None


def autodetect(sandbox=None):
    """Найти инструменты рядом с sr-modkit/. Возвращает dict key -> str-путь|None."""
    sandbox = Path(sandbox) if sandbox else sandbox_root()
    found = {
        'rsmc': _find_in_siblings(sandbox, ['rsmc/rsmc.exe']),
        'rscript': _find_in_siblings(sandbox, [f'{d}/RScript.exe' for d in _RSCRIPT_ORDER]),
        'blockpar': _find_in_siblings(sandbox, [f'{d}/BlockParEditor.exe' for d in _BLOCKPAR_ORDER]),
        'game': _find_in_siblings(sandbox, ['Space Rangers HD*']),
        'decompiler': None,
    }
    for sib in _SIBLINGS:
        base = sandbox / sib
        if (base / 'run.py').exists():
            found['decompiler'] = base
            break
    return {k: (str(v) if v else None) for k, v in found.items()}


def resolve_tools(cli_overrides=None, machine_path=None, sandbox=None):
    """Слить 4 источника путей к инструментам. Возвращает (values, sources)."""
    cli_overrides = cli_overrides or {}
    machine_path = Path(machine_path) if machine_path else machine_config_path()
    machine = _load_json(machine_path).get('tools', {})
    auto = autodetect(sandbox)

    values, sources = {}, {}
    for key in TOOL_KEYS:
        env_key = f'SRMOD_{key.upper()}'
        if cli_overrides.get(key):
            values[key], sources[key] = cli_overrides[key], 'cli'
        elif os.environ.get(env_key):
            values[key], sources[key] = os.environ[env_key], f'env {env_key}'
        elif machine.get(key):
            values[key], sources[key] = machine[key], f'machine ({machine_path})'
        elif auto.get(key):
            values[key], sources[key] = auto[key], 'autodetect'
        else:
            values[key], sources[key] = None, 'missing'
    return values, sources


def find_project_config(start=None):
    """Идти вверх от start (или cwd) в поисках srmod.json."""
    d = Path(start).resolve() if start else Path.cwd()
    if d.is_file():
        d = d.parent
    for cur in (d, *d.parents):
        cand = cur / 'srmod.json'
        if cand.exists():
            return cand
    return None


def load_project(start=None, path=None):
    cfg_path = Path(path) if path else find_project_config(start)
    raw = _load_json(cfg_path) if cfg_path else {}
    merged = _deep_merge(DEFAULT_PROJECT, raw)
    merged['_path'] = str(cfg_path) if cfg_path else None
    return merged


class Config:
    """Единая точка входа: tools (3 уровня) + project (srmod.json + дефолты)."""

    def __init__(self, project_dir=None, project_path=None, cli_tools=None,
                 machine_path=None, sandbox=None):
        self.tools, self.tool_sources = resolve_tools(cli_tools, machine_path, sandbox)
        self.project = load_project(project_dir, project_path)

    @property
    def root(self):
        """Каталог мода: где лежит srmod.json, иначе cwd."""
        if self.project.get('_path'):
            return Path(self.project['_path']).parent
        return Path.cwd()

    def tool(self, key):
        return self.tools.get(key)

    def tool_missing(self):
        return [k for k in TOOL_KEYS if not self.tools.get(k)]
