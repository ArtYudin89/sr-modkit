"""`srmod build` — конвейер медиа -> язык -> rsmc -> dat'ы -> pkg (docs/STAGE0.md, б).

Порядок шагов и гейты не придуманы заново — калька со спеки, которая сама
калька с рук пощупанных граблей rsmc (см. project_sr_vscode/project_scr_decompiler).
Инструменты (rsmc/BlockParEditor/srgi.py/srpkg.py) вызываются как внешние
процессы: ни формат .dat, ни бинарник rsmc не наши, реимплемент запрещён.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BOM = {
    'utf-16le': b'\xff\xfe',
    'utf-16be': b'\xfe\xff',
}


class BuildError(Exception):
    """Гейт сборки провален — сообщение уже готово для вывода пользователю."""


def _log(msg):
    print(msg)


# --------------------------------------------------------------------- гейты кодировок

def assert_rsm_is_utf8(path):
    data = path.read_bytes()
    if data[:3] == b'\xef\xbb\xbf':
        raise BuildError(f'{path}: .rsm обязан быть UTF-8 БЕЗ BOM (найден BOM)')
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise BuildError(f'{path}: .rsm не декодируется как UTF-8 ({e})')


def assert_not_utf8_datnik(path, data):
    """Гейт STAGE0.md: 'UTF-8 во входных .txt = падение'.

    BlockParEditor молча портит UTF-8 на входе (даёт кракозябры, 0 ошибок) —
    поэтому единственный безопасный контроль на нашей стороне: явно опознать
    UTF-8 (с BOM или без, с настоящей кириллицей) и упасть ДО вызова BPE.
    """
    if data[:3] == b'\xef\xbb\xbf':
        raise BuildError(f'{path}: UTF-8+BOM запрещён для датников — '
                          f'BlockParEditor молча портит вход (--lang.merge_into=txt)')
    if data[:2] in (BOM['utf-16le'], BOM['utf-16be']):
        return
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return  # не UTF-8 (вероятно cp1251) — BPE это ест нормально
    if any(ord(ch) >= 0x80 for ch in text):
        raise BuildError(f'{path}: похоже на UTF-8 без BOM с кириллицей — '
                          f'запрещено для датников, нужен UTF-16LE+BOM')


def _decode_datnik(data):
    if data[:2] == BOM['utf-16le']:
        return data[2:].decode('utf-16-le'), 'utf-16le'
    if data[:2] == BOM['utf-16be']:
        return data[2:].decode('utf-16-be'), 'utf-16be'
    return data.decode('cp1251'), 'cp1251'


def _encode_datnik(path, text, enc):
    path.parent.mkdir(parents=True, exist_ok=True)
    if enc == 'utf-16le':
        path.write_bytes(BOM['utf-16le'] + text.encode('utf-16-le'))
    elif enc == 'utf-16be':
        path.write_bytes(BOM['utf-16be'] + text.encode('utf-16-be'))
    else:
        path.write_bytes(text.encode('cp1251'))


# --------------------------------------------------------------------- точка входа

def resolve_entry(cfg):
    name = cfg.project.get('name')
    if not name:
        raise BuildError('srmod.json: "name" не задан')
    root = cfg.root
    entry_cfg = (cfg.project.get('script') or {}).get('entry')
    candidates = []
    if entry_cfg:
        candidates.append(root / entry_cfg)
    candidates.append(root / 'DATA' / 'Script' / f'{name}.src' / 'main.rsm')
    candidates.append(root / 'DATA' / 'Script' / f'{name}.rsm')
    for c in candidates:
        if c.exists():
            return c
    tried = ', '.join(str(c) for c in candidates)
    raise BuildError(f'точка входа .rsm не найдена (пробовали: {tried})')


def check_script_name(cfg, entry, out_scr):
    assert_rsm_is_utf8(entry)
    text = entry.read_text(encoding='utf-8')
    m = re.search(r'scriptName\(\s*"([^"]+)"\s*\)', text)
    if not m:
        raise BuildError(f'{entry}: не найден scriptName("...")')
    script_name = m.group(1)
    name = cfg.project['name']
    if script_name != name:
        raise BuildError(f'{entry}: scriptName("{script_name}") != srmod.json:name ("{name}")')
    if out_scr.stem != name:
        raise BuildError(f'выходной .scr назван "{out_scr.stem}", ожидался "{name}"')
    return script_name


# --------------------------------------------------------------------- 1. медиа

def run_media(cfg):
    """png->gi, *.gai.src/->gai, *.hai.src/->hai по всему DATA/ (в т.ч. внутри
    *.pkg.src/ — упаковка потом сама исключит png/times.json из стейджинга)."""
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('не найден decompiler (rson-decompiler) — нужен для srgi (медиа)')
    run_py = Path(decompiler) / 'run.py'
    data_dir = cfg.root / 'DATA'
    if not data_dir.is_dir():
        return []

    media_cfg = cfg.project.get('media') or {}
    gi_fmt = media_cfg.get('gi_format', 'argb')
    gai_fmt = media_cfg.get('gai_format', 'delta')
    results = []

    def encode(src, dst, fmt):
        proc = subprocess.run(
            [sys.executable, str(run_py), 'gi', 'encode', str(src), str(dst), '--format', fmt],
            capture_output=True, text=True, errors='replace')
        out = (proc.stdout or '') + (proc.stderr or '')
        ok = proc.returncode == 0 and dst.exists()
        results.append((str(src), str(dst), ok, out.strip()))
        if not ok:
            raise BuildError(f'srgi encode {src} -> {dst} провалился:\n{out.strip()}')

    for dirpath, dirnames, filenames in os.walk(data_dir):
        dirpath = Path(dirpath)
        keep = []
        for d in list(dirnames):
            low = d.lower()
            if low.endswith('.gai.src'):
                stem = d[:-len('.gai.src')]
                encode(dirpath / d, dirpath / f'{stem}.gai', gai_fmt)
            elif low.endswith('.hai.src'):
                stem = d[:-len('.hai.src')]
                encode(dirpath / d, dirpath / f'{stem}.hai', gi_fmt)
            else:
                keep.append(d)
        dirnames[:] = keep  # не спускаться внутрь уже обработанных .src-папок
        for f in filenames:
            if f.lower().endswith('.png'):
                encode(dirpath / f, dirpath / f'{Path(f).stem}.gi', gi_fmt)
    return results


# --------------------------------------------------------------------- 2/4. язык

def stage_lang(cfg, tmpdir):
    """CFG/<lang>/*.txt -> один временный файл на язык; в файл primary_lang
    добавляется каркас Script ^{ <name> ^{ } }, если такого блока ещё нет."""
    root = cfg.root
    name = cfg.project['name']
    primary = cfg.project.get('primary_lang')
    languages = cfg.project.get('languages') or ([primary] if primary else [])
    staged = {}
    for lang in languages:
        lang_dir = root / 'CFG' / lang
        if not lang_dir.is_dir():
            continue
        txt_files = sorted(lang_dir.glob('*.txt'))
        if not txt_files:
            continue
        pieces, enc = [], None
        for txt in txt_files:
            data = txt.read_bytes()
            assert_not_utf8_datnik(txt, data)
            text, this_enc = _decode_datnik(data)
            pieces.append(text.rstrip('\n'))
            enc = enc or this_enc
        combined = '\n'.join(pieces) + '\n'
        if lang == primary:
            block_re = re.compile(r'Script\s*\^\{\s*' + re.escape(name) + r'\s*\^\{')
            if not block_re.search(combined):
                # rsmc парсит блок построчно (не полноценный токенайзер): каркас
                # ОБЯЗАН быть на отдельных строках — однострочный
                # "Script ^{ Name ^{ } }" молча не матчится ("has no existing
                # ... block to merge into"), проверено руками.
                #
                # Каркас ставится ПЕРЕД остальным содержимым, не после. Грабля
                # BlockParEditor 1.9, проверена руками: корневой plain
                # "key=value" ПЕРЕД вложенным "Key ^{ }" даёт .dat, который сам
                # BPE не может прочитать назад (round-trip молча ломается —
                # write проходит без единой ошибки). Блок-потом-значение — цел.
                combined = f'Script ^{{\n    {name} ^{{\n    }}\n}}\n' + combined
        tmp_path = tmpdir / f'{lang}.Lang.txt'
        _encode_datnik(tmp_path, combined, enc)
        staged[lang] = tmp_path
    if primary and primary not in staged:
        raise BuildError(f'CFG/{primary}/*.txt (primary_lang) не найдены')
    return staged


def run_rsmc(cfg, entry, out_scr, lang_txt, timeout=60):
    rsmc = cfg.tool('rsmc')
    if not rsmc:
        raise BuildError('rsmc не найден (config/автодетект)')
    out_scr.parent.mkdir(parents=True, exist_ok=True)
    if lang_txt is not None and lang_txt.resolve() == out_scr.with_suffix('.txt').resolve():
        raise BuildError('--lang-txt не должен указывать на файл с тем же стемом, что -o '
                          '(rsmc пишет туда таблицу CT-ключей и затрёт язык)')
    args = [str(rsmc), 'build', str(entry), '-o', str(out_scr)]
    if lang_txt is not None:
        args += ['--lang-txt', str(lang_txt)]
    start = time.time()
    proc = subprocess.run(args, capture_output=True, text=True, errors='replace', timeout=timeout)
    out = ((proc.stdout or '') + (proc.stderr or '')).strip()
    low = out.lower()
    # rc бесполезен (0 даже на error:) — контроль только по тексту + свежести .scr.
    if 'error:' in low:
        raise BuildError(f'rsmc: {out}')
    if 'warning:' in low:
        raise BuildError(f'rsmc warning (гейт считает это ошибкой сборки — мод останется '
                          f'немым): {out}')
    if not out_scr.exists() or out_scr.stat().st_mtime < start - 2:
        raise BuildError(f'rsmc rc={proc.returncode}, но .scr не создан/не обновлён:\n{out}')
    return out


def _import_decompiler_run(cfg):
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('не найден decompiler (rson-decompiler) — нужен для blockpar/srpkg')
    d = str(Path(decompiler).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import run  # noqa: E402
    return run


def run_blockpar(cfg, src_txt, dst_dat):
    blockpar = cfg.tool('blockpar')
    if not blockpar:
        raise BuildError('blockpar (BlockParEditor.exe) не найден')
    decomp_run = _import_decompiler_run(cfg)
    ok, msg = decomp_run._run_blockpar(Path(blockpar), Path(src_txt), Path(dst_dat))
    if not ok:
        raise BuildError(f'blockpar {src_txt} -> {dst_dat}: {msg}')
    return dst_dat


# --------------------------------------------------------------------- 6. CacheData

def generate_cachedata_txt(cfg, tmpdir):
    """Только подтверждённая руками часть схемы: секция Script (docs/STAGE0.md
    не покрывает Bm/медиа-индекс — реверс внутренних ключей арт-ресурсов остаётся
    открытым пунктом низкого приоритета, см. project_sr_vscode)."""
    name = cfg.project['name']
    install = cfg.project.get('install')
    if not install:
        raise BuildError('srmod.json: "install" не задан (нужен для CacheData/INSTALL.TXT)')
    install_win = install.replace('/', '\\').rstrip('\\')
    scr_rel = f'{install_win}\\DATA\\Script\\{name}.scr'
    text = f'Script ^{{\n    {name}={scr_rel}\n}}\n'
    tmp_txt = tmpdir / 'CacheData.txt'
    _encode_datnik(tmp_txt, text, 'utf-16le')
    return tmp_txt


# --------------------------------------------------------------------- 7. pkg

def _copy_pkg_stage(src, dst):
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        outdir = dst / rel
        outdir.mkdir(parents=True, exist_ok=True)
        for f in filenames:
            if f.lower().endswith('.png') or f.lower().endswith('.times.json'):
                continue  # уже стали .gi/.gai/.hai на шаге медиа
            shutil.copy2(Path(dirpath) / f, outdir / f)


def build_packages(cfg):
    packages = cfg.project.get('packages') or []
    if not packages:
        return []
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('не найден decompiler (rson-decompiler) — нужен для srpkg')
    run_py = Path(decompiler) / 'run.py'
    built = []
    for pkg_src_rel in packages:
        pkg_src = cfg.root / pkg_src_rel
        if not pkg_src.is_dir():
            raise BuildError(f'пакет не найден: {pkg_src}')
        if not pkg_src.name.lower().endswith('.pkg.src'):
            raise BuildError(f'{pkg_src}: ожидается суффикс .pkg.src')
        pkg_out = pkg_src.parent / (pkg_src.name[:-len('.pkg.src')] + '.pkg')
        with tempfile.TemporaryDirectory(prefix='srmod_pkgstage_') as stage_root:
            stage = Path(stage_root) / pkg_src.name
            _copy_pkg_stage(pkg_src, stage)
            proc = subprocess.run(
                [sys.executable, str(run_py), 'pkg', 'pack', str(stage), str(pkg_out)],
                capture_output=True, text=True, errors='replace')
            out = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0 or not pkg_out.exists():
                raise BuildError(f'srpkg pack {pkg_src} -> {pkg_out}:\n{out}')
        built.append(pkg_out)
    return built


def generate_install_txt(cfg, built_pkgs):
    if not built_pkgs:
        return None
    install = cfg.project.get('install').replace('/', '\\').rstrip('\\')
    lines = ['Packages {']
    for pkg in built_pkgs:
        rel = str(pkg.relative_to(cfg.root)).replace('/', '\\')
        lines.append(f'    Package={install}\\{rel}')
    lines.append('}')
    text = '\n'.join(lines) + '\n'
    out = cfg.root / 'INSTALL.TXT'
    _encode_datnik(out, text, 'utf-16le')
    return out


# --------------------------------------------------------------------- оркестрация

def build(cfg, verbose=True):
    def log(msg):
        if verbose:
            _log(msg)

    name = cfg.project.get('name')
    if not name:
        raise BuildError('srmod.json не найден или без "name" — команда build требует проект')

    entry = resolve_entry(cfg)
    log(f'entry: {entry}')

    media_results = run_media(cfg)
    if media_results:
        log(f'медиа: {len(media_results)} файл(ов) закодировано')

    out_scr = cfg.root / 'DATA' / 'Script' / f'{name}.scr'
    check_script_name(cfg, entry, out_scr)

    with tempfile.TemporaryDirectory(prefix='srmod_build_') as tmp:
        tmpdir = Path(tmp)
        staged = stage_lang(cfg, tmpdir)
        primary = cfg.project.get('primary_lang')
        primary_txt = staged.get(primary) if primary else None

        log(f'rsmc build {entry.name} -> {out_scr.name}'
            + (f' (--lang-txt {primary_txt.name})' if primary_txt else ''))
        rsmc_out = run_rsmc(cfg, entry, out_scr, primary_txt)
        if rsmc_out:
            log(f'  {rsmc_out}')

        lang_dats = {}
        for lang, txt_path in staged.items():
            dat_path = cfg.root / 'CFG' / lang / 'Lang.dat'
            run_blockpar(cfg, txt_path, dat_path)
            lang_dats[lang] = dat_path
            log(f'lang: {lang}: {txt_path.name} -> {dat_path}')

        main_txt = cfg.root / 'CFG' / 'Main.txt'
        if main_txt.exists():
            data = main_txt.read_bytes()
            assert_not_utf8_datnik(main_txt, data)
            main_dat = cfg.root / 'CFG' / 'Main.dat'
            run_blockpar(cfg, main_txt, main_dat)
            log(f'main: {main_txt} -> {main_dat}')

        cache_txt = generate_cachedata_txt(cfg, tmpdir)
        cache_dat = cfg.root / 'CFG' / 'CacheData.dat'
        run_blockpar(cfg, cache_txt, cache_dat)
        log(f'cachedata: -> {cache_dat}')

        built_pkgs = build_packages(cfg)
        if built_pkgs:
            log(f'pkg: {len(built_pkgs)} архив(ов) собрано')
        install_txt = generate_install_txt(cfg, built_pkgs)
        if install_txt:
            log(f'install: -> {install_txt}')

    log('OK: сборка завершена')
    return {
        'scr': out_scr,
        'lang_dats': lang_dats,
        'cache_dat': cache_dat,
        'packages': built_pkgs,
    }
