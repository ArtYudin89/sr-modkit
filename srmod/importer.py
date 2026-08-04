"""`srmod import` — завести чужой .rson-мод в раскладку .rsm (docs/STAGE0.md, г).

`RScript --cli -x --split` раскладывает rson на модули main/vars/world/places/
states/dialogs/code_*.rsm (docs/STAGE0.md, «Формат .rsm»). Каталог с модулями
обязан называться `<Name>.src/`, а не безличным `main` — иначе два импорта в
одном моде затрут друг друга. `run_rscript_export_rsm(..., split=True)` сама
считает целевой каталог как `out_rsm` без ПОСЛЕДНЕГО суффикса — поэтому просим
экспорт в `<Name>.src.rsm`, а не `<Name>.rsm`: pathlib отрежет только `.rsm`
и оставит `<Name>.src` (проверено руками, см. project_sr_vscode).
"""
import shutil
import sys
from pathlib import Path

from .build import BuildError
from .lint import lint_project


def import_rson(cfg, rson_path, name=None, keep_source=True):
    rson_path = Path(rson_path)
    if not rson_path.exists():
        raise BuildError(f'{rson_path}: не найден')

    rscript = cfg.tool('rscript')
    if not rscript:
        raise BuildError('rscript не найден (нужен для --cli -x --split)')
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('decompiler (rson-decompiler) не найден')
    d = str(Path(decompiler).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import run as decomp_run  # noqa: E402

    name = name or rson_path.stem
    script_dir = cfg.src_dir / 'DATA' / 'Script'
    script_dir.mkdir(parents=True, exist_ok=True)
    out_rsm_arg = script_dir / f'{name}.src.rsm'  # -> каталог "<name>.src" (см. докстринг)

    ok, msg = decomp_run.run_rscript_export_rsm(Path(rscript), rson_path, out_rsm_arg, split=True)
    if not ok:
        raise BuildError(f'экспорт в .rsm провалился: {msg}')
    src_dir = Path(msg)
    from .opener import strip_rsm_bom
    strip_rsm_bom(src_dir)

    kept_rson = None
    if keep_source:
        # Нужен srmod verify --gate: сборка --engine rson требует исходный .rson,
        # а не производится обратно из .rsm автоматически (docs/STAGE0.md, г).
        kept_rson = script_dir / f'{name}.import.rson'
        shutil.copy2(rson_path, kept_rson)

    problems = [p for p in lint_project(cfg) if str(src_dir) in str(p[0])]
    if problems:
        print(f'lint: {len(problems)} замечание(й) в импортированном моде '
              f'(возможна потеря диалогового текста) — см. `srmod lint`:')
        for path, line, msg_ in problems:
            print(f'  {path}:{line}: {msg_}')

    return src_dir, kept_rson
