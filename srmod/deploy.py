"""`srmod deploy` — junction <игра>\\<install> -> build/ (docs/STAGE0.md).

Junction, а не копирование: цикл «поправил -> собрал -> запустил игру» без
шага установки, build/ и есть установленный мод. Junction (в отличие от
symlink) не требует прав администратора и прозрачен для игры.

Если по пути установки уже лежит НЕ наш junction (например, старая копия
мода) — не трогаем и говорим об этом: удалять чужие данные deploy не имеет
права, снос — явное действие пользователя.
"""
import os
import subprocess
from pathlib import Path

from .build import BuildError


def _same_target(link, build_dir):
    try:
        return os.path.realpath(str(link)) == os.path.realpath(str(build_dir))
    except OSError:
        return False


def _create_junction(build_dir, link):
    try:
        import _winapi
        _winapi.CreateJunction(str(build_dir), str(link))
        return
    except (ImportError, AttributeError):
        pass
    proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(build_dir)],
                         capture_output=True, text=True, errors='replace')
    if proc.returncode != 0:
        out = ((proc.stdout or '') + (proc.stderr or '')).strip()
        raise BuildError(f'mklink /J {link} -> {build_dir}: {out}')


def deploy(cfg):
    game = cfg.tool('game')
    if not game:
        raise BuildError('путь к игре не найден (config/автодетект/--game)')
    install = cfg.project.get('install')
    if not install:
        raise BuildError('srmod.json: "install" не задан')
    build_dir = cfg.build_dir
    if not build_dir.is_dir():
        raise BuildError(f'{build_dir}: нет сборки — сначала `srmod build`')

    link = Path(game).joinpath(*install.replace('\\', '/').split('/'))
    if link.exists():
        if _same_target(link, build_dir):
            print(f'OK: junction уже стоит: {link} -> {build_dir}')
            return link
        raise BuildError(
            f'{link}: уже существует и это не junction на build/ — не трогаю.\n'
            f'Если это старая копия мода, удалите её вручную и повторите deploy.')

    link.parent.mkdir(parents=True, exist_ok=True)
    _create_junction(build_dir, link)
    print(f'OK: junction создан: {link} -> {build_dir}')
    return link
