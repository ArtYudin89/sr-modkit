"""`srmod open` — этап C: любой компилированный мод -> проект src/ (исходники).

Один проход: scr -> .import.rson -> модули .rsm (`RScript --cli -x --split`),
датники CFG/*.dat -> *.txt (BlockParEditor), медиа gi/gai/hai -> png/кадры,
pkg -> <stem>.pkg.src/. Всё, что не декомпилируется БЕЗ ПОТЕРЬ, остаётся
бинарником-как-есть: каждый медиа-файл проверяется round-trip'ом
(decode -> encode == оригинал побайтово), и только выигравшие заменяются
исходником. Так `srmod build` из полученного src/ обязан воспроизводить
оригинал — это и есть гейт пересборки (verify_rebuild).

CacheData (реверс по корпусу 385 модов, 2026-08-04): секции Bm/Sound/ABMap/
Graphicks/Font — резольвер «внутренний ключ ресурса -> путь» (путь от корня
игры к файлу мода, VFS-путь внутрь pkg или, редко, путь от корня мода).
Ключи СЕМАНТИЧЕСКИЕ (имена предметов/кораблей из данных игры) — из файлов их
не вывести, автогенерация невозможна и не нужна: секции сохраняются как
исходник src/CFG/CacheData.txt, где префикс установки заменён на $INSTALL$
(build подставляет актуальный install). Секция Script оттуда вырезается —
её генерирует build.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .build import (BOM, BuildError, _decode_datnik, _encode_datnik,
                    _import_decompiler_run)

INSTALL_TOKEN = '$INSTALL$'
# Артефакты, которые не переносятся в src/ — их воспроизводит build.
_SKIP_ROOT_FILES = {'install.txt'}


def is_ct_table_file(path, name):
    """<name>.txt с одними строками 'N=текст' — таблица CT-ключей, артефакт
    сборки RScript (авторы кладут её куда попало, напр. CFG/Rus/): тексты уже
    в Lang.dat, при пересборке rsmc пишет свою."""
    if path.stem.lower() != name.lower() or path.suffix.lower() != '.txt':
        return False
    try:
        from .build import _decode_datnik
        text, _ = _decode_datnik(path.read_bytes())
    except Exception:
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return bool(lines) and all(re.match(r'^\d+=', ln.strip()) for ln in lines)


def _log(msg):
    print(msg)


# ------------------------------------------------------------------ blockpar dat->txt

def dat_to_text(cfg, dat_path):
    """CFG/*.dat -> текст (str). BPE выбирает направление по расширению;
    1.9 пишет txt в cp1251, 2.0 — в UTF-16LE+BOM, поэтому байты декодируем
    сами и дальше работаем со строкой."""
    decomp_run = _import_decompiler_run(cfg)
    blockpar = cfg.tool('blockpar')
    if not blockpar:
        raise BuildError('blockpar (BlockParEditor.exe) не найден')
    with tempfile.TemporaryDirectory(prefix='srmod_open_bpe_') as tmp:
        out_txt = Path(tmp) / (dat_path.stem + '.txt')
        ok, msg = decomp_run._run_blockpar(Path(blockpar), dat_path, out_txt)
        if not ok or not out_txt.exists():
            raise BuildError(f'blockpar {dat_path} -> txt: {msg}')
        text, _enc = _decode_datnik(out_txt.read_bytes())
    return text


# ------------------------------------------------------------------ CacheData

def split_top_sections(text):
    """Разбить текст датника на корневые секции: [(name|None, текст-куска)].
    name=None — куски вне блоков (plain key=value на корне)."""
    lines = text.splitlines()
    sections, cur_name, cur, depth = [], None, [], 0
    for line in lines:
        s = line.strip()
        m = re.match(r'^(\S+)\s*[\^~]\{$', s)
        if depth == 0 and m:
            if cur:
                sections.append((cur_name, cur))
            cur_name, cur = m.group(1), [line]
            depth = 1
            continue
        if depth > 0:
            cur.append(line)
            depth += s.count('{') - s.count('}')
            if depth <= 0:
                sections.append((cur_name, cur))
                cur_name, cur, depth = None, [], 0
            continue
        cur.append(line)
    if cur:
        sections.append((cur_name, cur))
    return [(n, '\n'.join(ls)) for n, ls in sections]


def derive_install_from_cachedata(cache_text, name):
    """Script ^{ <name>=<install>\\DATA\\Script\\<name>.scr } -> install."""
    m = re.search(re.escape(name) + r'\s*=\s*(.+)$', cache_text, flags=re.M)
    if not m:
        return None
    value = m.group(1).strip()
    suffix = f'\\data\\script\\{name.lower()}.scr'
    if value.lower().endswith(suffix):
        return value[:-len(suffix)].replace('\\', '/')
    return None


def make_cachedata_source(cache_text, install, name):
    """Вырезать секцию Script, подставить $INSTALL$ вместо префикса установки.
    Возвращает (текст исходника | None, [предупреждения])."""
    warnings = []
    kept = []
    for sec_name, chunk in split_top_sections(cache_text):
        if sec_name == 'Script':
            extra = [ln for ln in chunk.splitlines()
                     if '=' in ln and ln.split('=')[0].strip() != name]
            if extra:
                warnings.append(f'CacheData: в секции Script есть чужие записи '
                                f'(мультискрипт?) — потеряны: {extra}')
            continue
        kept.append(chunk)
    if not kept:
        return None, warnings
    text = '\n'.join(kept) + '\n'
    if install:
        prefix = install.replace('/', '\\').rstrip('\\') + '\\'
        out_lines = []
        for line in text.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                if v.strip().lower().startswith(prefix.lower()):
                    v = v.strip()
                    line = f'{k}={INSTALL_TOKEN}\\{v[len(prefix):]}'
            out_lines.append(line)
        text = '\n'.join(out_lines) + '\n'
    return text, warnings


# ------------------------------------------------------------------ медиа round-trip

class MediaStats:
    def __init__(self):
        self.decoded = []      # (rel, 'png'|'gai.src'|'hai.src'|'pkg.src')
        self.kept_binary = []  # (rel, причина)


def _run_tool(run_py, args, timeout=300):
    proc = subprocess.run([sys.executable, str(run_py)] + [str(a) for a in args],
                          capture_output=True, text=True, errors='replace',
                          timeout=timeout)
    out = ((proc.stdout or '') + (proc.stderr or '')).strip()
    return proc.returncode == 0, out


# Тип кадра из `gi info` -> какие --format могли его породить (все lossless
# для своего типа; порядок — от наиболее вероятного).
_GI_TYPE_FORMATS = {
    '0': ['argb', 'rgb565'],
    '1': ['rle565'],
    '2': ['rle-alpha'],
    '3': ['rle-indexed'],
    '4': ['indexed'],
}
_ANIM_FORMATS = ['delta', 'rle-alpha', 'argb', 'rgb565', 'rle565', 'indexed']

# Анимации больше лимита не декомпилируются: encode_delta_frame — чистый
# python, у 200-кадрового 559x768 gai (меню SR2Reboot) одна попытка стоит
# минуты; такие остаются бинарниками.
ANIM_LIMIT_MB = 2


def _anim_candidates(srgi, raw, default_fmt):
    """Сузить перебор форматов по типу первого кадра gai (иначе на большой
    анимации перебор 6 форматов x 2 фон-варианта — часы)."""
    try:
        import struct
        gi_seek, gi_size = struct.unpack_from('<2I', raw, 48)
        if not gi_seek:
            return [default_fmt]
        fsig = struct.unpack_from('<I', raw, gi_seek)[0]
        if fsig in (getattr(srgi, 'ZL01_SIG', 0), getattr(srgi, 'ZL02_SIG', 0)):
            fbuf = srgi.unpack_zl(raw[gi_seek:gi_seek + gi_size])
            typ = str(srgi.read_gi_header(fbuf, 0)['type'])
        else:
            typ = str(srgi.read_gi_header(raw, gi_seek)['type'])
        by_type = {'5': ['delta'], '6': ['delta'], '2': ['rle-alpha'],
                   '1': ['rle565'], '3': ['rle-indexed'], '4': ['indexed'],
                   '0': ['argb', 'rgb565']}
        return by_type.get(typ, [default_fmt])
    except Exception:
        return [default_fmt]


def _write_format_sidecar(parent, stem, kind, fmt):
    """<stem>.gi.json / <stem>.gai.json / <stem>.hai.json рядом с исходником:
    build читает отсюда формат кодирования вместо дефолта из srmod.json."""
    sc = Path(parent) / f'{stem}.{kind}.json'
    sc.write_text(json.dumps({'format': fmt}) + '\n', encoding='utf-8')


def mark_raw_png(png_path):
    """<stem>.png.json {"raw": true}: этот png — НАТИВНЫЙ ресурс мода (игра
    читает png как есть, в т.ч. из pkg — см. SR2RebootMenu), а не исходник
    для кодирования в .gi. build такой png копирует, а не кодирует."""
    sc = png_path.parent / f'{png_path.stem}.png.json'
    sc.write_text(json.dumps({'raw': True}) + '\n', encoding='utf-8')


def _import_srgi(decompiler_dir):
    d = str(Path(decompiler_dir).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import srgi  # noqa: E402
    return srgi


def try_decode_gi(srgi, gi_path, dst_png, default_fmt):
    """gi -> png, если png -> gi воспроизводит оригинал побайтово. In-process
    (srgi как модуль): на pkg с сотнями картинок subprocess-вариант стоил
    минуты только на старте питонов. Эквивалентность с build гарантирована
    тем, что это те же функции, что зовёт CLI: encode_gi/read_png/write_png.
    Не-дефолтный формат фиксируется в сайдкаре <stem>.gi.json (читает build)."""
    raw = gi_path.read_bytes()
    try:
        typ = str(srgi.read_gi_header(raw, 0)['type'])
        cv = srgi.decode_gi(raw)
    except Exception as e:
        return False, f'decode: {e}'
    candidates = _GI_TYPE_FORMATS.get(typ, ['argb'])
    for fmt in candidates:
        try:
            blob = srgi.encode_gi(cv.w, cv.h, cv.buf, fmt)
        except Exception:
            continue
        if bytes(blob) == raw:
            dst_png.parent.mkdir(parents=True, exist_ok=True)
            srgi.write_png(str(dst_png), cv.w, cv.h, cv.buf)
            if fmt != default_fmt:
                _write_format_sidecar(dst_png.parent, gi_path.stem, 'gi', fmt)
            return True, ''
    return False, f'round-trip не байт-в-байт ни одним форматом {candidates} (type={typ})'


def try_decode_anim(run_py, srgi, ani_path, dst_dir, default_fmt):
    """gai/hai -> каталог кадров <stem>.<ext>.src/, если обратная сборка
    воспроизводит оригинал. Пробуем decode как есть и --no-bg (для gai с
    haveBackground кадры иначе запекаются на фоновый .gi) × форматы кадров."""
    kind = ani_path.suffix.lower().lstrip('.')
    size_mb = ani_path.stat().st_size / (1024 * 1024)
    if size_mb > ANIM_LIMIT_MB:
        return False, (f'{size_mb:.1f} МБ > лимита {ANIM_LIMIT_MB} МБ для '
                       f'анимаций — оставлена бинарником')
    if kind == 'gai':
        fmts = _anim_candidates(srgi, ani_path.read_bytes(), default_fmt)
    else:
        fmts = [default_fmt]
    last = ''
    for extra in ([], ['--no-bg']):
        with tempfile.TemporaryDirectory(prefix='srmod_open_ani_') as tmp:
            tmp = Path(tmp)
            frames = tmp / 'frames'
            ok, out = _run_tool(run_py, ['gi', 'decode', ani_path, frames] + extra)
            if not ok:
                last = f'decode{extra}: {out}'
                continue
            for fmt in fmts:
                re_ani = tmp / ani_path.name
                ok, out = _run_tool(run_py, ['gi', 'encode', frames, re_ani,
                                             '--format', fmt])
                if not ok or not re_ani.exists():
                    last = f'encode {fmt}{extra}: {out}'
                    continue
                if re_ani.read_bytes() != ani_path.read_bytes():
                    last = f'round-trip {fmt}{extra} не байт-в-байт'
                    continue
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(frames, dst_dir)
                if fmt != default_fmt:
                    _write_format_sidecar(dst_dir.parent, ani_path.stem, kind, fmt)
                return True, ''
    return False, last


def open_media_file(cfg_media, run_py, srgi, src_file, dst_dir_for_file, rel, stats):
    """Один gi/gai/hai: декомпилировать с round-trip-гарантией или копировать."""
    low = src_file.name.lower()
    gi_fmt = cfg_media.get('gi_format', 'argb')
    gai_fmt = cfg_media.get('gai_format', 'delta')
    if low.endswith('.gi'):
        ok, why = try_decode_gi(srgi, src_file,
                                dst_dir_for_file / (src_file.stem + '.png'), gi_fmt)
        kind = 'png'
    elif low.endswith('.gai'):
        ok, why = try_decode_anim(run_py, srgi, src_file,
                                  dst_dir_for_file / (src_file.stem + '.gai.src'),
                                  gai_fmt)
        kind = 'gai.src'
    elif low.endswith('.hai'):
        ok, why = try_decode_anim(run_py, srgi, src_file,
                                  dst_dir_for_file / (src_file.stem + '.hai.src'),
                                  gi_fmt)
        kind = 'hai.src'
    else:
        return False
    if ok:
        stats.decoded.append((rel, kind))
    else:
        dst_dir_for_file.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_dir_for_file / src_file.name)
        stats.kept_binary.append((rel, why))
    return True


def open_pkg(cfg_media, run_py, srgi, pkg_path, dst_dir, rel, stats, limit_mb=64):
    """pkg -> <stem>.pkg.src/ (только если pack воспроизводит оригинал),
    внутри — те же медиа-round-trip'ы. Иначе pkg остаётся бинарником.
    Гигантские архивы (напр. DenBG.pkg — 1.6 ГБ фонов) по умолчанию не
    разворачиваются: и распаковка, и каждая последующая сборка стоили бы
    минуты и гигабайты."""
    size_mb = pkg_path.stat().st_size / (1024 * 1024)
    if size_mb > limit_mb:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg_path, dst_dir / pkg_path.name)
        stats.kept_binary.append(
            (rel, f'pkg {size_mb:.0f} МБ > лимита {limit_mb} МБ — оставлен '
                  f'архивом (--pkg-limit, чтобы поднять)'))
        return None
    with tempfile.TemporaryDirectory(prefix='srmod_open_pkg_') as tmp:
        tmp = Path(tmp)
        unpacked = tmp / 'unpacked'
        ok, out = _run_tool(run_py, ['pkg', 'unpack', pkg_path, unpacked],
                            timeout=1800)
        if ok and unpacked.exists():
            re_pkg = tmp / pkg_path.name
            ok, out = _run_tool(run_py, ['pkg', 'pack', unpacked, re_pkg],
                                timeout=1800)
            ok = ok and re_pkg.exists() and re_pkg.read_bytes() == pkg_path.read_bytes()
        if not ok:
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pkg_path, dst_dir / pkg_path.name)
            stats.kept_binary.append((rel, f'pkg round-trip: {out[-200:]}'))
            return None
        pkg_src = dst_dir / (pkg_path.stem + '.pkg.src')
        if pkg_src.exists():
            shutil.rmtree(pkg_src)
        pkg_src.mkdir(parents=True)
        for f in sorted(unpacked.rglob('*')):
            if f.is_dir():
                continue
            frel = f.relative_to(unpacked)
            _log(f'    pkg: {frel}')
            target_dir = pkg_src / frel.parent
            if not open_media_file(cfg_media, run_py, srgi, f, target_dir,
                                   f'{rel}/{frel}', stats):
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target_dir / f.name)
                if f.suffix.lower() == '.png':
                    mark_raw_png(target_dir / f.name)
        stats.decoded.append((rel, 'pkg.src'))
        return pkg_src


# ------------------------------------------------------------------ скрипт

def strip_rsm_bom(dir_path):
    """RScript --cli -x пишет .rsm с UTF-8 BOM; наш гейт сборки требует UTF-8
    без BOM (rsmc ест оба) — нормализуем экспорт на месте."""
    for f in Path(dir_path).rglob('*.rsm'):
        data = f.read_bytes()
        if data[:3] == b'\xef\xbb\xbf':
            f.write_bytes(data[3:])


def _strip_lang_script_section(lang_txt_path):
    """Убрать секцию Script из Lang-исходника (тексты переехали в .rsm)."""
    text, enc = _decode_datnik(lang_txt_path.read_bytes())
    kept = [c for n, c in split_top_sections(text) if n != 'Script']
    _encode_datnik(lang_txt_path, '\n'.join(kept).strip('\n') + '\n'
                   if kept else '', enc)


def script_roundtrip_ok(cfg, name, entry_rsm, orig_scr, orig_lang_text):
    """Собрать модули rsmc'ом (без языка) и сравнить с оригинальным .scr
    нормализованным сравнением с CT-разрешением. (True, '') | (False, why)."""
    from .build import run_rsmc
    from .rebuild import compare_scr, ct_table_from_lang  # lazy: rebuild импортирует нас
    msgs = []
    try:
        with tempfile.TemporaryDirectory(prefix='srmod_open_rt_') as tmp:
            tmp_scr = Path(tmp) / f'{name}.scr'
            run_rsmc(cfg, entry_rsm, tmp_scr, lang_txt=None)
            ta = ct_table_from_lang(orig_lang_text, name) if orig_lang_text else None
            from .rebuild import _read_ct_table
            tb = _read_ct_table(tmp_scr.with_suffix('.txt'))
            good, _ = compare_scr(cfg, name, orig_scr, tmp_scr, ta, tb,
                                  msgs.append)
        return good, '' if good else (msgs[-1].strip() if msgs else 'DIFF')
    except BuildError as e:
        return False, f'rsmc не собрал .rsm: {e}'


def open_script(cfg, mod_dir, src_root, name, lang_txt_text, lang_dat_path=None):
    """<name>.scr -> <name>.import.rson -> <name>.src/ (модули .rsm).

    Каскад декомпиляции: наш decompiler.py (умеет FileVersion 7/8) ->
    `run.py rsdec` (декомпилятор самого RScript, берёт и старые форматы).
    Если оба мимо — вызывающий откатывается на engine=scr."""
    scr = mod_dir / 'DATA' / 'Script' / f'{name}.scr'
    decompiler_dir = cfg.tool('decompiler')
    rscript = cfg.tool('rscript')
    if not decompiler_dir:
        raise BuildError('decompiler (rson-decompiler) не найден')
    if not rscript:
        raise BuildError('rscript не найден (нужен для --cli -x --split)')
    d = str(Path(decompiler_dir).resolve())
    if d not in sys.path:
        sys.path.insert(0, d)
    import decompiler as decomp_mod  # noqa: E402
    import run as decomp_run  # noqa: E402

    script_dir = src_root / 'DATA' / 'Script'
    script_dir.mkdir(parents=True, exist_ok=True)
    rson_out = script_dir / f'{name}.import.rson'

    lang_arg = None
    tmp_lang = None
    if lang_txt_text:
        # decompiler.load_lang ест только текстовый Lang — передаём во временном
        # файле (UTF-16LE, чтобы не потерять ничего сверх cp1251).
        fd_dir = tempfile.mkdtemp(prefix='srmod_open_lang_')
        tmp_lang = Path(fd_dir) / 'Lang.txt'
        _encode_datnik(tmp_lang, lang_txt_text, 'utf-16le')
        lang_arg = str(tmp_lang)
    try:
        try:
            decomp_mod.decompile(str(scr), str(rson_out), lang_arg)
        except Exception:
            pass
    finally:
        if tmp_lang is not None:
            shutil.rmtree(tmp_lang.parent, ignore_errors=True)
    if not rson_out.exists():
        run_py = Path(decompiler_dir) / 'run.py'
        args = ['rsdec', scr, rson_out, '--rscript', rscript]
        if lang_dat_path and Path(lang_dat_path).exists():
            args += ['--langdat', lang_dat_path]
        ok, out = _run_tool(run_py, args)
        if not ok or not rson_out.exists():
            raise BuildError(f'скрипт не декомпилировался ни decompiler.py, '
                             f'ни rsdec: {out[-200:]}')

    # Каталог модулей: просим экспорт в <name>.src.rsm — обёртка режет только
    # последний суффикс и создаёт "<name>.src" (грабля именования, см. importer.py).
    out_rsm_arg = script_dir / f'{name}.src.rsm'
    ok, msg = decomp_run.run_rscript_export_rsm(Path(rscript), rson_out,
                                                out_rsm_arg, split=True)
    if not ok:
        raise BuildError(f'RScript -x --split провалился: {msg}')
    strip_rsm_bom(Path(msg))
    return Path(msg), rson_out


# ------------------------------------------------------------------ оркестрация open

def open_mod(cfg, mod_dir, dest, name=None, install=None, force=False,
             pkg_limit_mb=64):
    """Разобрать компилированный мод mod_dir в новый проект dest.
    Возвращает (dest, отчёт-словарь)."""
    mod_dir = Path(mod_dir).resolve()
    dest = Path(dest).resolve()
    if not mod_dir.is_dir():
        raise BuildError(f'{mod_dir}: не каталог')
    if not (mod_dir / 'ModuleInfo.txt').exists():
        raise BuildError(f'{mod_dir}: нет ModuleInfo.txt — это не мод SR HD')
    if dest.exists() and any(dest.iterdir()) and not force:
        raise BuildError(f'{dest}: каталог не пуст (--force, чтобы всё равно)')
    if mod_dir == dest or mod_dir in dest.parents:
        raise BuildError('dest внутри открываемого мода — так нельзя')

    decompiler_dir = cfg.tool('decompiler')
    if not decompiler_dir:
        raise BuildError('decompiler (rson-decompiler) не найден')
    run_py = Path(decompiler_dir) / 'run.py'
    srgi_mod = _import_srgi(decompiler_dir)

    scrs = sorted((mod_dir / 'DATA' / 'Script').glob('*.scr')) \
        if (mod_dir / 'DATA' / 'Script').is_dir() else []
    if name is None:
        if len(scrs) == 1:
            name = scrs[0].stem
        elif not scrs:
            name = mod_dir.name
        else:
            raise BuildError(f'в моде несколько .scr ({[s.stem for s in scrs]}) — '
                             f'укажи --name')
    has_script = any(s.stem == name for s in scrs)

    # --- CFG: датники -> тексты ---------------------------------------
    warnings = []
    cache_text = None
    cache_dat = mod_dir / 'CFG' / 'CacheData.dat'
    if cache_dat.exists():
        cache_text = dat_to_text(cfg, cache_dat)
    if install is None and cache_text and has_script:
        install = derive_install_from_cachedata(cache_text, name)
    if install is None:
        # Мод лежит в <игра>/Mods/... ? Тогда install — его путь от корня игры.
        game = cfg.tool('game')
        if game:
            try:
                install = mod_dir.relative_to(Path(game).resolve()).as_posix()
            except ValueError:
                pass
    if install is None:
        install = f'Mods/Imported/{name}'
        warnings.append(f'install не удалось определить — взят {install}')

    src_root = dest / 'src'
    src_root.mkdir(parents=True, exist_ok=True)

    # Конвертируются только датники, которые build умеет собирать обратно:
    # Main.dat, <lang>/Lang.dat, CacheData.dat. Прочие .dat (если есть) уедут
    # в src/ бинарниками — build скопирует их как есть.
    languages, lang_texts, converted_dats = [], {}, {str(cache_dat).lower()}
    cfg_dir = mod_dir / 'CFG'
    if cfg_dir.is_dir():
        for dat in sorted(cfg_dir.rglob('*.dat')):
            rel = dat.relative_to(cfg_dir)
            is_main = len(rel.parts) == 1 and rel.name.lower() == 'main.dat'
            is_lang = len(rel.parts) == 2 and rel.name.lower() == 'lang.dat'
            if dat == cache_dat or not (is_main or is_lang):
                continue
            text = dat_to_text(cfg, dat)
            if is_lang:
                lang = rel.parts[0]
                languages.append(lang)
                lang_texts[lang] = text
            out_txt = src_root / 'CFG' / rel.with_suffix('.txt')
            _encode_datnik(out_txt, text, 'utf-16le')
            converted_dats.add(str(dat).lower())
    primary = 'Rus' if 'Rus' in languages else (languages[0] if languages else None)

    if cache_text is not None:
        cache_src, w = make_cachedata_source(cache_text, install, name)
        warnings += w
        if cache_src:
            _encode_datnik(src_root / 'CFG' / 'CacheData.txt', cache_src, 'utf-16le')

    # --- статика: всё, кроме артефактов -------------------------------
    media_cfg = {'gi_format': 'argb', 'gai_format': 'delta'}
    stats = MediaStats()
    packages = []
    for f in sorted(mod_dir.rglob('*')):
        if f.is_dir():
            continue
        rel = f.relative_to(mod_dir)
        _log(f'  open: {rel}')
        low = f.name.lower()
        rel_low = [p.lower() for p in rel.parts]
        if str(f).lower() in converted_dats:
            continue  # уже конвертированы в .txt-исходники
        if str(rel).lower() in _SKIP_ROOT_FILES:
            continue  # INSTALL.TXT генерирует build
        if rel_low[:2] == ['data', 'script'] and (low == f'{name.lower()}.scr'
                                                  or low == f'{name.lower()}.txt'):
            continue  # скрипт декомпилируется отдельно; .txt — таблица CT (артефакт)
        if is_ct_table_file(f, name):
            warnings.append(f'{rel}: таблица CT-ключей (артефакт сборки RScript) — '
                            f'в src/ не перенесена, rsmc пишет свою')
            continue
        if (rel_low[0] == 'cfg' and low.endswith('.txt')
                and f.with_suffix('.dat').exists()):
            # Авторский txt-исходник рядом со своим .dat (Lang.txt при живом
            # Lang.dat и т.п.). Игра читает .dat, и они бывают РАССИНХРОНЕНЫ
            # (DreadnoughtsForAll) — источником берём .dat, txt не переносим,
            # иначе он затёр бы конвертированный текст.
            warnings.append(f'{rel}: авторский txt-исходник рядом с .dat — '
                            f'в src/ не перенесён (источник истины — .dat)')
            continue
        dst_dir = (src_root / rel).parent
        if low.endswith('.pkg'):
            pkg_src = open_pkg(media_cfg, run_py, srgi_mod, f, dst_dir, str(rel),
                               stats, limit_mb=pkg_limit_mb)
            if pkg_src is not None:
                packages.append(pkg_src.relative_to(src_root).as_posix())
            continue
        if open_media_file(media_cfg, run_py, srgi_mod, f, dst_dir, str(rel), stats):
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst_dir / f.name)
        if low.endswith('.png'):
            mark_raw_png(dst_dir / f.name)

    # --- скрипт --------------------------------------------------------
    # Симметрия с медиа: .rsm-ветка принимается только если rsmc-сборка из неё
    # эквивалентна оригинальному .scr (round-trip-гейт прямо при открытии).
    # Иначе оригинальный .scr остаётся БИНАРНЫМ исходником (engine=scr),
    # а модули .rsm лежат рядом как справочные.
    src_modules = kept_rson = None
    engine = 'rsmc' if has_script else 'none'
    primary_lang_text = lang_texts.get(primary) if primary else None
    if has_script:
        orig_scr = mod_dir / 'DATA' / 'Script' / f'{name}.scr'
        try:
            src_modules, kept_rson = open_script(
                cfg, mod_dir, src_root, name, primary_lang_text,
                lang_dat_path=(mod_dir / 'CFG' / primary / 'Lang.dat')
                if primary else None)
            ok_rt, why_rt = script_roundtrip_ok(
                cfg, name, src_modules / 'main.rsm', orig_scr, primary_lang_text)
        except BuildError as e:
            src_modules = kept_rson = None
            ok_rt, why_rt = False, str(e)
        if ok_rt:
            # Тексты скрипта живут в .rsm (text:) — оригинальная секция
            # Script^{<name>} в Lang-исходнике избыточна: rsmc генерит свою
            # таблицу заново. Вычищаем, чтобы не копить мёртвые ключи.
            if primary and primary in lang_texts:
                _strip_lang_script_section(src_root / 'CFG' / primary / 'Lang.txt')
            if len(languages) > 1:
                warnings.append(
                    'мультиязычный мод: секции Script неосновных языков ведут '
                    'СТАРУЮ нумерацию CT-ключей — после первой сборки их надо '
                    'переключить вручную (см. docs/STAGE0.md, многоязычность)')
        else:
            engine = 'scr'
            (src_root / 'DATA' / 'Script').mkdir(parents=True, exist_ok=True)
            shutil.copy2(orig_scr, src_root / 'DATA' / 'Script' / orig_scr.name)
            ref = (f'модули {src_modules.name}/ — справочные'
                   if src_modules else 'модулей .rsm нет')
            warnings.append(
                f'.rsm-ветка НЕ воспроизводит оригинальный .scr ({why_rt}) — '
                f'скрипт оставлен бинарником (engine=scr), {ref}; тексты '
                f'остаются в Lang.txt')

    # --- srmod.json / .gitignore ---------------------------------------
    if engine == 'rsmc':
        script_cfg = {'engine': 'rsmc',
                      'entry': f'src/DATA/Script/{name}.src/main.rsm'}
    elif engine == 'scr':
        script_cfg = {'engine': 'scr'}
    else:
        script_cfg = {'engine': 'none'}
    had_install_txt = any(f.name.lower() == 'install.txt'
                          for f in mod_dir.iterdir() if f.is_file())
    project = {
        'name': name,
        'install': install,
        'languages': languages or ['Rus'],
        'primary_lang': primary or 'Rus',
        'script': script_cfg,
        'media': media_cfg,
        'packages': packages,
        'install_txt': had_install_txt,
        'deploy': {'mode': 'junction'},
    }
    (dest / 'srmod.json').write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (dest / '.gitignore').write_text('/build/\n/.srmod/\n', encoding='utf-8')

    return dest, {
        'name': name, 'install': install, 'has_script': has_script,
        'engine': engine, 'languages': languages, 'packages': packages,
        'decoded': stats.decoded, 'kept_binary': stats.kept_binary,
        'src_modules': src_modules, 'kept_rson': kept_rson,
        'warnings': warnings,
    }


def print_report(dest, report):
    _log(f'OK: мод открыт -> {dest}')
    _log(f'  name={report["name"]}  install={report["install"]}  '
         f'script={report["engine"] if report["has_script"] else "нет"}  '
         f'langs={",".join(report["languages"]) or "-"}')
    if report['src_modules']:
        _log(f'  скрипт: {report["src_modules"]} (+ {Path(report["kept_rson"]).name} '
             f'для verify --gate)')
    if report['packages']:
        _log(f'  pkg: {", ".join(report["packages"])}')
    dec = report['decoded']
    if dec:
        _log(f'  декомпилировано медиа: {len(dec)}')
    for rel, why in report['kept_binary']:
        _log(f'  ! оставлен бинарником: {rel} — {why}')
    for w in report['warnings']:
        _log(f'  ! {w}')
