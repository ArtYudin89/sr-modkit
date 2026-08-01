"""`srmod doctor` — печатает, что откуда взялось, и пробует инструменты живьём.

Ничего не переизобретает: версия/форма CLI RScript определяется тем же
`_rscript_is_modern` из rson-decompiler/run.py, что использует и сам декомпилятор
(см. docs/STAGE0.md, (в)). rsmc.exe — обычная консольная программа без модалок,
поэтому пробуется прямым запуском без аргументов.
"""
import subprocess
import sys
from pathlib import Path

# Кодировки исходников мода — гейт сборки (docs/STAGE0.md, (а)):
# ModuleInfo.txt и *.txt для датников только UTF-16LE+BOM, *.rsm только UTF-8.
BOM_UTF16LE = b'\xff\xfe'
BOM_UTF16BE = b'\xfe\xff'
BOM_UTF8 = b'\xef\xbb\xbf'


def _import_decompiler(decompiler_dir):
    """sys.path-инъекция rson-decompiler и импорт run.py (лениво, только когда нужно)."""
    if not decompiler_dir:
        return None
    d = str(Path(decompiler_dir).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import run  # noqa: E402  (rson-decompiler/run.py)
        return run
    except Exception as e:
        print(f'  ! не удалось импортировать run.py из {d}: {e}')
        return None


def probe_rscript(path, decompiler_dir):
    if not path:
        return 'не найден'
    run = _import_decompiler(decompiler_dir)
    if run is None:
        return f'{path} (версию определить не удалось — нет декомпилятора)'
    try:
        modern = run._rscript_is_modern(Path(path))
    except Exception as e:
        return f'{path} (проба не удалась: {e})'
    return f'{path} — CLI {"4.14f+ (modern: -x/-d/--full)" if modern else "≤4.13f (-b -f)"}'


def probe_rsmc(path):
    if not path:
        return 'не найден'
    try:
        proc = subprocess.run([str(path)], capture_output=True, timeout=10,
                               text=True, errors='replace')
        first_line = (proc.stdout or proc.stderr or '').strip().splitlines()
        first_line = first_line[0] if first_line else '(пусто)'
        return f'{path} — отвечает: "{first_line}"'
    except subprocess.TimeoutExpired:
        return f'{path} (не ответил за 10с — неожиданно для консольной rsmc)'
    except Exception as e:
        return f'{path} (не запустился: {e})'


def probe_blockpar(path):
    if not path:
        return 'не найден'
    name = Path(path).parent.name
    if '1.9' in name:
        return f'{path} — 1.9 (dat→txt в cp1251)'
    if '2.0' in name:
        return f'{path} — 2.0 (dat→txt в UTF-16LE+BOM)'
    return f'{path} — версия неизвестна по имени папки'


def _sniff_encoding(data):
    if data[:2] == BOM_UTF16LE:
        return 'utf-16le+bom'
    if data[:2] == BOM_UTF16BE:
        return 'utf-16be+bom'
    if data[:3] == BOM_UTF8:
        return 'utf-8+bom (ЗАПРЕЩЕНО для датников)'
    try:
        data.decode('utf-8')
        # UTF-8 valid text without BOM is still suspect for dat-sources: cp1251
        # single-byte cyrillic decodes as UTF-8 only by accident (ASCII range).
        if any(b >= 0x80 for b in data):
            return 'utf-8 без BOM (ЗАПРЕЩЕНО для датников)'
        return 'ascii'
    except UnicodeDecodeError:
        return 'вероятно cp1251 (не UTF-8) — игра тоже это ест, но не rsmc/BPE'


def check_source_encodings(root):
    """Обойти мод и проверить кодировки: ModuleInfo.txt/*.txt (датники) — только
    UTF-16LE+BOM; *.rsm — UTF-8. Возвращает список строк-проблем (пусто = ок)."""
    root = Path(root)
    problems = []

    mi = root / 'ModuleInfo.txt'
    if mi.exists():
        enc = _sniff_encoding(mi.read_bytes())
        if 'utf-16le' not in enc:
            problems.append(f'{mi}: {enc}, ожидается utf-16le+bom')

    cfg = root / 'CFG'
    if cfg.is_dir():
        for txt in cfg.rglob('*.txt'):
            data = txt.read_bytes()
            enc = _sniff_encoding(data)
            if 'utf-16le' not in enc:
                problems.append(f'{txt}: {enc}, ожидается utf-16le+bom')

    script_dir = root / 'DATA' / 'Script'
    if script_dir.is_dir():
        for rsm in script_dir.rglob('*.rsm'):
            data = rsm.read_bytes()
            if data[:3] == BOM_UTF8:
                problems.append(f'{rsm}: utf-8+bom, ожидается utf-8 без BOM')
                continue
            try:
                data.decode('utf-8')
            except UnicodeDecodeError:
                problems.append(f'{rsm}: не декодируется как UTF-8')

    return problems


def run_doctor(cfg):
    """cfg: srmod.config.Config. Печатает отчёт в stdout."""
    print('== Инструменты ==')
    for key in ('rsmc', 'rscript', 'blockpar', 'game', 'decompiler'):
        path = cfg.tool(key)
        src = cfg.tool_sources.get(key, '?')
        status = path if path else 'НЕ НАЙДЕН'
        print(f'  {key:10s} [{src}]: {status}')

    print()
    print('== Пробы ==')
    print(f'  rsmc:     {probe_rsmc(cfg.tool("rsmc"))}')
    print(f'  rscript:  {probe_rscript(cfg.tool("rscript"), cfg.tool("decompiler"))}')
    print(f'  blockpar: {probe_blockpar(cfg.tool("blockpar"))}')

    print()
    print('== Проект ==')
    if cfg.project.get('_path'):
        print(f'  srmod.json: {cfg.project["_path"]}')
        print(f'  name={cfg.project.get("name")!r} install={cfg.project.get("install")!r}')
    else:
        print('  srmod.json не найден (ищем от cwd вверх) — команды build/new/verify его потребуют')

    print()
    print('== Кодировки исходников ==')
    problems = check_source_encodings(cfg.root)
    if not problems:
        print('  ок (или мод ещё не создан)')
    else:
        for p in problems:
            print(f'  ! {p}')

    missing = cfg.tool_missing()
    return 1 if missing else 0
