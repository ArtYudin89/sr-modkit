"""`srmod verify --gate` — сборка обеими ветками (rsmc / rson) и сравнение
(docs/STAGE0.md, г). Применимо только к импортированным модам: rson-ветка
собирает из `DATA/Script/<Name>.import.rson`, который кладёт `srmod import`
(докстринг importer.py) — для мода, начатого `srmod new`, исходного .rson
никогда не было, гейту сравнивать нечего.

Самодостаточная реализация (не тянет `_rsm_regress.py` — тот приватный, живёт
только в рабочей rson_decompiler-копии, не в git). Уровни нормализации — те,
что документированы в STAGE0 (д): байты -> Pos.*/шапка вырезаны -> пробелы в
строках схлопнуты. CT-разрешение и обезличивание имён (более мягкие уровни
корпусного гейта) здесь не переоткрываются — при расхождении просто печатаем
первую разницу и оставляем разбор человеку.
"""
import json
import re
import sys
import tempfile
from pathlib import Path

from .build import BuildError, resolve_entry, run_rsmc

HEADER_SKIP = ('ScriptFileOut', 'ScriptTextOut', 'ScriptName')
POS_KEYS = ('Pos.x', 'Pos.y', 'ViewPos.x', 'ViewPos.y')


def _import_decompiler(cfg):
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('decompiler (rson-decompiler) не найден')
    d = str(Path(decompiler).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import decompiler as decomp_mod  # noqa: E402
    import run as run_mod  # noqa: E402
    return decomp_mod, run_mod


def _strip(obj, drop_keys):
    if isinstance(obj, dict):
        return {k: _strip(v, drop_keys) for k, v in obj.items() if k not in drop_keys}
    if isinstance(obj, list):
        return [_strip(x, drop_keys) for x in obj]
    return obj


def _norm_ws(obj):
    if isinstance(obj, dict):
        return {k: _norm_ws(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm_ws(x) for x in obj]
    if isinstance(obj, str):
        return re.sub(r'\s+', ' ', obj).strip()
    return obj


def _first_diff(a, b, path='$'):
    if type(a) is not type(b):
        return f'{path}: тип {type(a).__name__} vs {type(b).__name__}'
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                return f'{path}.{k}: отсутствует в rsmc-сборке'
            if k not in b:
                return f'{path}.{k}: отсутствует в rson-сборке'
            if a[k] != b[k]:
                return _first_diff(a[k], b[k], f'{path}.{k}')
        return None
    if isinstance(a, list):
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_diff(x, y, f'{path}[{i}]')
        if len(a) != len(b):
            return f'{path}: длина {len(a)} vs {len(b)}'
        return None
    return f'{path}: {a!r} vs {b!r}'


def _decompile(decomp_mod, scr_path):
    tmp_rson = scr_path.with_suffix('.gate.rson')
    decomp_mod.decompile(scr_path, out_path=tmp_rson)
    if not tmp_rson.exists():
        raise BuildError(f'decompiler.py не смог разобрать {scr_path}')
    with open(tmp_rson, encoding='utf-8-sig') as f:
        data = json.load(f)
    tmp_rson.unlink(missing_ok=True)
    return data


def verify_gate(cfg):
    name = cfg.project.get('name')
    if not name:
        raise BuildError('srmod.json без "name"')
    rson_path = cfg.src_dir / 'DATA' / 'Script' / f'{name}.import.rson'
    if not rson_path.exists():
        print(f'gate неприменим: {rson_path.name} не найден (мод не импортирован '
              f'`srmod import` — сравнивать нечего)')
        return 0

    decomp_mod, run_mod = _import_decompiler(cfg)
    rscript = cfg.tool('rscript')
    if not rscript:
        raise BuildError('rscript не найден (нужен для rson-ветки)')

    entry = resolve_entry(cfg)
    with tempfile.TemporaryDirectory(prefix='srmod_gate_') as tmp:
        tmp = Path(tmp)
        scr_rsmc = tmp / f'{name}.rsmc.scr'
        run_rsmc(cfg, entry, scr_rsmc, lang_txt=None)

        scr_rson = tmp / f'{name}.rson.scr'
        rc, err, _writeback = run_mod._run_rscript(Path(rscript), rson_path, scr_rson)
        if err or not scr_rson.exists():
            raise BuildError(f'RScript (rson-ветка) провалилась: rc={rc} {err}')

        raw_a, raw_b = scr_rsmc.read_bytes(), scr_rson.read_bytes()
        if raw_a == raw_b:
            print(f'MATCH (байты идентичны): {name}')
            return 0

        da = _strip(_decompile(decomp_mod, scr_rsmc), HEADER_SKIP + POS_KEYS)
        db = _strip(_decompile(decomp_mod, scr_rson), HEADER_SKIP + POS_KEYS)
        if da == db:
            print(f'MATCH (структура идентична, байты нет — ожидаемо, CT-ключи '
                  f'нумеруются заново): {name}')
            return 0
        if _norm_ws(da) == _norm_ws(db):
            print(f'MATCH (после схлопывания пробелов в Code — rsmc пишет с отступом): {name}')
            return 0

        diff = _first_diff(da, db) or 'расхождение вне сравниваемых полей'
        print(f'DIFF: {name}')
        print(f'  {diff}')
        print('  (используй --engine rson для сборки этого мода, пока не заведён issue rsmc)')
        return 1
