"""Точка входа CLI `srmod`. Этап 0: пока только `doctor` (config.py готов);
build/new/lint/watch/import/verify добавляются по очереди из docs/STAGE0.md."""
import argparse
import sys

from . import config as configmod
from . import doctor as doctormod
from . import build as buildmod
from . import new as newmod
from . import lint as lintmod
from . import watch as watchmod
from . import importer as importermod
from . import verify as verifymod


def _tool_cli_overrides(args):
    return {
        'rsmc': args.rsmc,
        'rscript': args.rscript,
        'blockpar': args.blockpar,
        'game': args.game,
        'decompiler': args.decompiler,
    }


def _add_tool_flags(p):
    p.add_argument('--rsmc', help='путь к rsmc.exe (перебивает конфиг/автодетект)')
    p.add_argument('--rscript', help='путь к RScript.exe')
    p.add_argument('--blockpar', help='путь к BlockParEditor.exe')
    p.add_argument('--game', help='путь к каталогу игры')
    p.add_argument('--decompiler', help='путь к rson-decompiler (для импорта srgi/srpkg/run)')


def build_parser():
    p = argparse.ArgumentParser(prog='srmod', description='CLI-тулинг для модов Space Rangers HD')
    sub = p.add_subparsers(dest='command', required=True)

    p_doctor = sub.add_parser('doctor', help='проверить конфиг и живьём пробить инструменты')
    p_doctor.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')
    _add_tool_flags(p_doctor)

    p_build = sub.add_parser('build', help='собрать мод: медиа -> язык -> rsmc -> dat -> pkg')
    p_build.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')
    _add_tool_flags(p_build)

    p_new = sub.add_parser('new', help='скаффолд минимального мода')
    p_new.add_argument('dest', help='каталог для нового мода')
    p_new.add_argument('--name', required=True, help='имя мода (scriptName/имя .scr)')
    p_new.add_argument('--install', help='путь установки от корня игры (по умолчанию Mods/Artem/<name>)')
    p_new.add_argument('--primary-lang', default='Rus')
    p_new.add_argument('--lang', dest='languages', action='append',
                       help='дополнительный язык (можно повторять); по умолчанию только primary')
    p_new.add_argument('--force', action='store_true', help='скаффолдить даже в непустой каталог')

    p_lint = sub.add_parser('lint', help='статические проверки .rsm (DText в dialogMsg, цифровые state)')
    p_lint.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')

    p_watch = sub.add_parser('watch', help='пересобирать при изменении исходников')
    p_watch.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')
    p_watch.add_argument('--once', action='store_true', help='собрать один раз (если изменилось) и выйти')
    p_watch.add_argument('--interval', type=float, default=1.0, help='период опроса, сек')
    _add_tool_flags(p_watch)

    p_import = sub.add_parser('import', help='завести чужой .rson-мод в раскладку .rsm')
    p_import.add_argument('rson', help='путь к .rson')
    p_import.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')
    p_import.add_argument('--name', help='имя модуля (по умолчанию — стем .rson)')
    p_import.add_argument('--no-keep-source', action='store_true',
                          help='не сохранять <Name>.import.rson (нужен для verify --gate)')
    _add_tool_flags(p_import)

    p_verify = sub.add_parser('verify', help='гейт доверия rsmc vs rson для импортированного мода')
    p_verify.add_argument('project', nargs='?', help='каталог мода (по умолчанию cwd)')
    p_verify.add_argument('--gate', action='store_true', help='(зарезервировано — сейчас единственный режим)')
    _add_tool_flags(p_verify)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'doctor':
        cli_tools = _tool_cli_overrides(args)
        cfg = configmod.Config(project_dir=getattr(args, 'project', None), cli_tools=cli_tools)
        return doctormod.run_doctor(cfg)

    if args.command == 'build':
        cli_tools = _tool_cli_overrides(args)
        cfg = configmod.Config(project_dir=getattr(args, 'project', None), cli_tools=cli_tools)
        try:
            buildmod.build(cfg)
        except buildmod.BuildError as e:
            print(f'error: {e}', file=sys.stderr)
            return 1
        return 0

    if args.command == 'new':
        languages = list(dict.fromkeys([args.primary_lang] + (args.languages or [])))
        try:
            dest = newmod.scaffold(args.dest, args.name, install=args.install,
                                   primary_lang=args.primary_lang, languages=languages,
                                   force=args.force)
        except buildmod.BuildError as e:
            print(f'error: {e}', file=sys.stderr)
            return 1
        print(f'OK: {dest}')
        return 0

    if args.command == 'lint':
        cfg = configmod.Config(project_dir=getattr(args, 'project', None))
        return lintmod.run_lint(cfg)

    if args.command == 'watch':
        cli_tools = _tool_cli_overrides(args)
        cfg = configmod.Config(project_dir=getattr(args, 'project', None), cli_tools=cli_tools)
        watchmod.run_watch(cfg, lambda: buildmod.build(cfg), interval=args.interval, once=args.once)
        return 0

    if args.command == 'import':
        cli_tools = _tool_cli_overrides(args)
        cfg = configmod.Config(project_dir=getattr(args, 'project', None), cli_tools=cli_tools)
        try:
            src_dir, kept_rson = importermod.import_rson(
                cfg, args.rson, name=args.name, keep_source=not args.no_keep_source)
        except buildmod.BuildError as e:
            print(f'error: {e}', file=sys.stderr)
            return 1
        print(f'OK: {src_dir}')
        if kept_rson:
            print(f'    (rson сохранён для verify --gate: {kept_rson})')
        return 0

    if args.command == 'verify':
        cli_tools = _tool_cli_overrides(args)
        cfg = configmod.Config(project_dir=getattr(args, 'project', None), cli_tools=cli_tools)
        try:
            return verifymod.verify_gate(cfg)
        except buildmod.BuildError as e:
            print(f'error: {e}', file=sys.stderr)
            return 1

    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
