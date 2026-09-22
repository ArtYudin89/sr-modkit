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

from .vendor import srblockpar

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
    """CFG/*.dat -> текст (str) своим кодом, без внешних программ.

    Значения, в которых есть `//`, текстовый вид не переживут: разбор самой
    игры режет строку по этим знакам, и обратная сборка их потеряет. Такие
    значения не проглатываются молча — о них печатается предупреждение.
    """
    try:
        document = srblockpar.read_dat(dat_path)
    except (srblockpar.DatError, srblockpar.BlockParError, srblockpar.ZLError) as e:
        raise BuildError(f'blockpar {dat_path} -> txt: {e}')
    for key, value in srblockpar.lossy_values(document.tree)[:5]:
        _log(f'  ВНИМАНИЕ: {dat_path.name}: значение {key} содержит // — '
             f'в исходнике .txt оно обрежется: {value[:60]!r}')
    return '\r\n'.join(srblockpar.render_text(document.tree)) + '\r\n' 


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


def _points_at_own_scr(line, own_stems, install):
    """Ведёт ли запись CacheData на скрипт ЭТОГО мода: путь внутри каталога
    установки и имя файла — одно из наших. Den_DatVersion переопределяет
    скрипты самой игры (`ms_begin`…) — там путь чужой, и такие записи наши."""
    value = line.split('=', 1)[1].strip().replace('/', '\\')
    stem = value.rsplit('\\', 1)[-1]
    if not stem.lower().endswith('.scr') or stem[:-4].lower() not in own_stems:
        return False
    if not install:
        return True
    prefix = install.replace('/', '\\').rstrip('\\').lower() + '\\'
    return value.lower().startswith(prefix)


def make_cachedata_source(cache_text, install, names):
    """Вырезать секцию Script, подставить $INSTALL$ вместо префикса установки.
    Возвращает (текст исходника | None, [предупреждения]).

    names — все скрипты мода: секцию Script build генерирует сам по этому
    списку, а вот запись о скрипте, которого в списке нет, потерялась бы молча
    (так было до поддержки мультискрипта) — о ней предупреждаем.
    """
    warnings = []
    known = {names} if isinstance(names, str) else set(names)
    kept = []
    for sec_name, chunk in split_top_sections(cache_text):
        if sec_name == 'Script':
            # Записи о СВОИХ скриптах генерирует build (пути зависят от install),
            # а всё остальное в этой секции — авторские ссылки на чужие скрипты
            # (Den_DatVersion переопределяет скрипты самой игры: ms_begin,
            # ms_blazer…). Их сохраняем как есть.
            # «Своя» запись — та, что ВЕДЁТ на наш .scr: ключ с именем скрипта
            # не совпадает (SR2Dominators: `MS_Begin_Legacy=…\MS_Begin.scr`,
            # Cat_Tardis: `Tardis=…\cat_tardis.scr`), и фильтр по ключу оставил
            # бы её в исходнике — а build генерит такую же, и они бы задвоились.
            own_stems = {n.lower() for n in known}
            extra = [ln for ln in chunk.splitlines()
                     if '=' in ln and ln.split('=')[0].strip() not in known
                     and not _points_at_own_scr(ln, own_stems, install)]
            if extra:
                kept.append('Script ^{\n' + '\n'.join(extra) + '\n}\n')
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
        if (src_file.parent / (src_file.stem + '.png')).exists():
            # Мод везёт и `X.gi`, и нативный `X.png` (TicTacToe: bg.gi + bg.png).
            # Декодировать gi некуда — исходником стал бы тот же самый файл, и
            # один из двух пропал бы из сборки. Оставляем gi бинарником.
            dst_dir_for_file.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_dir_for_file / src_file.name)
            stats.kept_binary.append(
                (rel, 'рядом лежит нативный .png с тем же именем'))
            return True
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


def _strip_lang_script_section(lang_txt_path, names=None):
    """Убрать из Lang-исходника тексты скриптов, которые пересобирает rsmc.

    names=None — вся секция Script (мод с единственным скриптом). Со списком
    вырезаются только подблоки этих имён: у мультискриптового мода часть
    скриптов может остаться бинарной (engine=scr), и их тексты — единственный
    источник, трогать их нельзя.
    """
    text, enc = _decode_datnik(lang_txt_path.read_bytes())
    if names is None:
        kept = [c for n, c in split_top_sections(text) if n != 'Script']
        out = '\n'.join(kept).strip('\n') + '\n' if kept else ''
        _encode_datnik(lang_txt_path, out, enc)
        return

    names = set(names)
    parts = []
    for sec_name, chunk in split_top_sections(text):
        if sec_name != 'Script':
            parts.append(chunk)
            continue
        lines, depth, skip_until = [], 0, None
        for line in chunk.splitlines():
            s = line.strip()
            m = re.match(r'^(\S+)\s*[\^~]\{$', s)
            if m:
                depth += 1
                if skip_until is None and depth == 2 and m.group(1) in names:
                    skip_until = depth
                    continue
            elif s == '}':
                if skip_until is not None and depth == skip_until:
                    depth -= 1
                    skip_until = None
                    continue
                depth -= 1
            if skip_until is None:
                lines.append(line)
        body = [ln for ln in lines if ln.strip() not in ('', '}')
                and not re.match(r'^Script\s*[\^~]\{$', ln.strip())]
        if body:                      # в секции остались чужие скрипты
            parts.append('\n'.join(lines))
    out = '\n'.join(parts).strip('\n')
    _encode_datnik(lang_txt_path, (out + '\n') if out else '', enc)


def script_roundtrip_ok(cfg, name, entry_rsm, orig_scr, orig_lang_text):
    """Собрать модули rsmc'ом (без языка) и сравнить с оригинальным .scr
    нормализованным сравнением с CT-разрешением.

    Возвращает (ok, why, new_ct_table): третьим — таблица ключей, которую rsmc
    выдал этой сборке (новый ключ -> текст основного языка). По ней
    перекеиваются неосновные языки, см. rekey_secondary_langs.
    """
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
        return good, '' if good else (msgs[-1].strip() if msgs else 'DIFF'), tb
    except BuildError as e:
        return False, f'rsmc не собрал .rsm: {e}', None


def rekey_map(old_table, new_table):
    """{старый ключ -> новый ключ} по совпадению ТЕКСТА основного языка.

    Единственный мост между языками — сам текст: rsmc нумерует ключи заново, а
    перевод в неосновном языке лежит под старыми номерами. Повторяющиеся тексты
    раздаются по очереди (в реальных модах это разные реплики с одинаковыми
    словами — «Да», «Конец связи»), ключи без пары остаются несопоставленными.
    """
    by_text = {}
    for k, text in sorted((new_table or {}).items(), key=lambda kv: _key_num(kv[0])):
        by_text.setdefault(text, []).append(k)
    mapping, unmatched = {}, []
    for k, text in sorted((old_table or {}).items(), key=lambda kv: _key_num(kv[0])):
        queue = by_text.get(text)
        if queue:
            mapping[k] = queue.pop(0)
        else:
            unmatched.append(k)
    return mapping, unmatched


def _key_num(k):
    try:
        return (0, int(k))
    except ValueError:
        return (1, k)


def rekey_secondary_langs(src_root, name, languages, primary, old_primary_table,
                          new_table):
    """Перенумеровать секции Script^{name} неосновных языков под ключи rsmc.

    Формат не даёт связать перевод с репликой иначе как через номер ключа, а
    номера rsmc раздаёт заново — поэтому после открытия мода перевод должен
    переехать на новую нумерацию, иначе второй язык в игре немой. Дальше их
    держит в согласии гейт сборки (build.check_secondary_lang_keys).
    Возвращает список предупреждений.
    """
    mapping, unmatched = rekey_map(old_primary_table, new_table)
    warnings = []
    if not mapping:
        return warnings
    for lang in languages:
        if lang == primary:
            continue
        path = src_root / 'CFG' / lang / 'Lang.txt'
        if not path.exists():
            continue
        text, enc = _decode_datnik(path.read_bytes())
        out, changed, missed = [], 0, 0
        depth, in_name = 0, False
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r'^(\S+)\s*[\^~]\{$', s)
            if m:
                depth += 1
                if depth == 2 and m.group(1) == name:
                    in_name = True
                out.append(line)
                continue
            if s == '}':
                if in_name and depth == 2:
                    in_name = False
                depth -= 1
                out.append(line)
                continue
            if in_name and '=' in s:
                k, v = s.split('=', 1)
                new_k = mapping.get(k.strip())
                if new_k is None:
                    missed += 1
                    out.append(line)
                else:
                    changed += 1
                    out.append(line.replace(f'{k}=', f'{new_k}=', 1)
                               if line.lstrip().startswith(k) else f'        {new_k}={v}')
                continue
            out.append(line)
        if changed:
            _encode_datnik(path, '\n'.join(out) + '\n', enc)
            warnings.append(f'{lang}: перевод переключён на нумерацию rsmc '
                            f'({changed} ключ(ей)' +
                            (f', без пары {missed}' if missed else '') + ')')
        if unmatched and lang != primary:
            warnings.append(f'{lang}: {len(unmatched)} текст(ов) основного языка '
                            f'не нашли пары в новой таблице — эти реплики после '
                            f'сборки останутся без перевода')
    return warnings


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
    # Мультискриптовый мод (28 из 236 скриптовых модов корпуса) разбирается
    # целиком: --name задаёт лишь ОСНОВНОЙ скрипт, остальные .scr открываются
    # такими же полноправными. Основной выбирается ниже — по Main.dat.
    stems = [s.stem for s in scrs]

    # --- CFG: датники -> тексты ---------------------------------------
    warnings = []
    cache_text = None
    cache_dat = mod_dir / 'CFG' / 'CacheData.dat'
    if cache_dat.exists():
        try:
            cache_text = dat_to_text(cfg, cache_dat)
        except (BuildError, UnicodeDecodeError) as e:
            # Тот же случай, что ниже с Lang/Main: датник не читается как
            # текст (WH40kGuns — байт вне cp1251) ⇒ остаётся бинарником.
            warnings.append(f'CFG/CacheData.dat: не читается как текст '
                            f'({str(e)[:60]}) — оставлен бинарником')
    if install is None and cache_text:
        # По любому из скриптов: какой из них основной, ещё не решено, а путь
        # установки у всех записей CacheData один и тот же.
        for stem in ([name] if name else stems):
            install = derive_install_from_cachedata(cache_text, stem)
            if install:
                break
    if install is None:
        # Мод лежит в <игра>/Mods/... ? Тогда install — его путь от корня игры.
        game = cfg.tool('game')
        if game:
            try:
                install = mod_dir.relative_to(Path(game).resolve()).as_posix()
            except ValueError:
                pass
    if install is None:
        install = f'Mods/Imported/{name or mod_dir.name}'
        warnings.append(f'install не удалось определить — взят {install}')

    src_root = dest / 'src'
    src_root.mkdir(parents=True, exist_ok=True)

    # Конвертируются только датники, которые build умеет собирать обратно:
    # Main.dat, <lang>/Lang.dat, CacheData.dat. Прочие .dat (если есть) уедут
    # в src/ бинарниками — build скопирует их как есть.
    languages, lang_texts = [], {}
    # Конвертированные датники в src/ не копируются (их пересоберёт build);
    # CacheData попадает сюда, только если его удалось прочитать.
    converted_dats = {str(cache_dat).lower()} if cache_text is not None else set()
    main_text, binary_dats = None, []
    cfg_dir = mod_dir / 'CFG'
    if cfg_dir.is_dir():
        for dat in sorted(cfg_dir.rglob('*.dat')):
            rel = dat.relative_to(cfg_dir)
            is_main = len(rel.parts) == 1 and rel.name.lower() == 'main.dat'
            is_lang = len(rel.parts) == 2 and rel.name.lower() == 'lang.dat'
            if dat == cache_dat or not (is_main or is_lang):
                continue
            try:
                text = dat_to_text(cfg, dat)
            except (BuildError, UnicodeDecodeError) as e:
                # Текст датника BPE 1.9 отдаёт в cp1251, но встречаются байты
                # вне неё (WH40kGuns) — такой файл через текст не проходит,
                # оставляем бинарником.
                binary_dats.append((str(rel).replace('\\', '/'),
                                    f'не читается как текст ({str(e)[:60]})'))
                continue
            if '//' in text:
                # BlockPar считает `//` началом комментария и при обратной
                # конвертации обрезает значение (проверено на BPE 1.9 и 2.0):
                # строка с URL (`https://…` в датниках Den_DatVersion) через
                # текст не воспроизводится. Такой .dat остаётся бинарным
                # исходником — как медиа, чей round-trip не сошёлся.
                binary_dats.append((str(rel).replace('\\', '/'),
                                    'значение содержит "//" — BlockPar обрезал бы '
                                    'его при обратной конвертации'))
                continue
            if is_main:
                main_text = text
            if is_lang:
                lang = rel.parts[0]
                languages.append(lang)
                lang_texts[lang] = text
            out_txt = src_root / 'CFG' / rel.with_suffix('.txt')
            _encode_datnik(out_txt, text, 'utf-16le')
            converted_dats.add(str(dat).lower())
    primary = 'Rus' if 'Rus' in languages else (languages[0] if languages else None)
    # Язык, оставшийся бинарным, rsmc'у недоступен: свою таблицу текстов он
    # мержит в текстовый Lang, поэтому скрипт такого мода собирается бинарным
    # passthrough'ем — иначе тексты просто не доедут.
    force_scr = any(Path(rel).name.lower() == 'lang.dat' for rel, _why in binary_dats)
    for rel, why in binary_dats:
        warnings.append(f'CFG/{rel}: {why} — файл оставлен бинарником'
                        + (' (скрипт собирается веткой engine=scr)'
                           if force_scr and rel.lower().endswith('lang.dat') else ''))

    # Основной скрипт — тот, что подключён в Main.dat (`X=1,Script.X`): у
    # мультискриптовых модов вспомогательные .scr там не упомянуты вовсе
    # (RefGreeting: 6 скриптов, в Main.dat один), а имя каталога с именем
    # скрипта обычно не совпадает.
    if name is None:
        linked = [s for s in stems
                  if main_text and re.search(
                      re.escape(s) + r'\s*=\s*1\s*,\s*Script\.' + re.escape(s), main_text)]
        if linked:
            name = linked[0]
        elif mod_dir.name in stems:
            name = mod_dir.name
        elif stems:
            name = stems[0]
        else:
            name = mod_dir.name
    script_names = ([name] if name in stems else []) + [s for s in stems if s != name]
    has_script = name in stems

    if cache_text is not None:
        cache_src, w = make_cachedata_source(cache_text, install, script_names)
        warnings += w
        if cache_src:
            _encode_datnik(src_root / 'CFG' / 'CacheData.txt', cache_src, 'utf-16le')

    # INSTALL.TXT генерируется сборкой из списка пакетов — но только если автор
    # перечислил в нём ровно все .pkg мода. Den_UIRecolor везёт три пакета, а в
    # INSTALL.TXT вписан один (языковые монтируются иначе) — такой файл
    # выводом не воспроизвести, переносим его в src/ как обычный файл.
    install_txt_path = next((f for f in mod_dir.iterdir()
                             if f.is_file() and f.name.lower() == 'install.txt'), None)
    mod_pkgs = {p.name.lower() for p in mod_dir.rglob('*.pkg') if p.is_file()}
    install_txt_generated = False
    if install_txt_path is not None:
        listed = set()
        raw = install_txt_path.read_bytes()
        text = raw[2:].decode('utf-16-le') if raw[:2] == b'\xff\xfe' \
            else raw.decode('cp1251', errors='replace')
        for line in text.splitlines():
            if '=' in line:
                listed.add(line.split('=', 1)[1].strip().rsplit('\\', 1)[-1].lower())
        install_txt_generated = bool(listed) and listed == mod_pkgs
        if not install_txt_generated:
            warnings.append('INSTALL.TXT перечисляет не все пакеты мода — '
                            'перенесён в src/ как есть (build его не генерирует)')

    # --- статика: всё, кроме артефактов -------------------------------
    media_cfg = {'gi_format': 'argb', 'gai_format': 'delta'}
    stats = MediaStats()
    packages = []
    deferred_ct = []                  # [(path, rel, script_name)] — см. ниже
    for f in sorted(mod_dir.rglob('*')):
        if f.is_dir():
            continue
        rel = f.relative_to(mod_dir)
        _log(f'  open: {rel}')
        low = f.name.lower()
        rel_low = [p.lower() for p in rel.parts]
        if str(f).lower() in converted_dats:
            continue  # уже конвертированы в .txt-исходники
        if str(rel).lower() in _SKIP_ROOT_FILES and install_txt_generated:
            continue  # INSTALL.TXT воспроизводит build (список = все пакеты мода)
        if (len(rel_low) == 3 and rel_low[:2] == ['data', 'script']
                and any(low == f'{n.lower()}.scr' for n in script_names)):
            # Только НЕПОСРЕДСТВЕННО в DATA/Script: у FairanCoalitionHeart в
            # подпапке «Бэкап» лежит .scr с тем же именем — это файл мода, и
            # выбрасывать его как артефакт нельзя.
            continue  # скрипты декомпилируются/копируются отдельно
        art_of = next((n for n in script_names
                       if (len(rel_low) == 3 and rel_low[:2] == ['data', 'script']
                           and low == f'{n.lower()}.txt') or is_ct_table_file(f, n)), None)
        if art_of is not None:
            # Таблица CT-ключей: артефакт сборки RScript. Её воспроизводит rsmc
            # — но только на своей ветке. Скрипт, оставшийся бинарником, ничего
            # не пишет, и тогда таблица обязана доехать до build как файл мода.
            # Ветку узнаём ниже, поэтому решение откладываем.
            deferred_ct.append((f, rel, art_of))
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
    primary_lang_text = lang_texts.get(primary) if primary else None
    # Какие скрипты автор внёс в секцию Script CacheData.dat: воспроизводим
    # ровно этот список (у RefGreeting из шести скриптов там только основной,
    # у BlockSOTE — оба; вывести правилом нельзя).
    # Ключ записи не обязан совпадать с именем скрипта (Cat_Tardis:
    # `tardis=…\cat_tardis.scr`) — запоминаем авторский ключ по пути.
    cached_names, cache_keys = set(), {}
    if cache_text:
        for sec_name, chunk in split_top_sections(cache_text):
            if sec_name != 'Script':
                continue
            for ln in chunk.splitlines():
                if '=' not in ln:
                    continue
                key, value = ln.split('=', 1)
                key = key.strip()
                cached_names.add(key)
                stem = value.strip().replace('/', '\\').rsplit('\\', 1)[-1]
                if stem.lower().endswith('.scr'):
                    cache_keys[stem[:-4].lower()] = key
    opened = []                       # [{'name', 'engine', 'entry', 'modules'}]
    for scr_name in (script_names if has_script else []):
        orig_scr = mod_dir / 'DATA' / 'Script' / f'{scr_name}.scr'
        modules = rson = None
        try:
            modules, rson = open_script(
                cfg, mod_dir, src_root, scr_name, primary_lang_text,
                lang_dat_path=(mod_dir / 'CFG' / primary / 'Lang.dat')
                if primary else None)
            ok_rt, why_rt, new_ct = script_roundtrip_ok(
                cfg, scr_name, modules / 'main.rsm', orig_scr, primary_lang_text)
            if ok_rt and force_scr:
                ok_rt, why_rt = False, 'Lang.dat остался бинарным (см. выше)'
        except BuildError as e:
            modules = rson = None
            ok_rt, why_rt, new_ct = False, str(e), None
        # Связь «скрипт <-> запись CacheData» ищем по ПУТИ, а не по имени:
        # у Cat_Tardis ключ `Tardis` ведёт на `cat_tardis.scr`, и совпадение
        # имени с чужим ключом дало бы лишнюю запись в пересборке.
        cache_key = cache_keys.get(scr_name.lower())
        if cache_text:
            in_cache = cache_key is not None
        else:
            # Нет CFG/CacheData.dat вовсе (RefLitRes: скрипт запускает соседний
            # мод) — генерировать его нельзя, в пересборке он был бы лишним.
            # Нечитаемый датник остаётся бинарником, там cache не наше дело.
            in_cache = cache_dat.exists()
        if ok_rt:
            if len(languages) > 1 and primary_lang_text:
                # Перевод неосновных языков лежит под СТАРЫМИ номерами ключей, а
                # rsmc раздаёт их заново — переключаем сразу при открытии,
                # иначе второй язык в игре молчит.
                from .rebuild import ct_table_from_lang
                warnings += rekey_secondary_langs(
                    src_root, scr_name, languages, primary,
                    ct_table_from_lang(primary_lang_text, scr_name), new_ct)
            opened.append({'name': scr_name, 'engine': 'rsmc',
                           'entry': f'src/DATA/Script/{scr_name}.src/main.rsm',
                           'cache': in_cache, 'cache_key': cache_key,
                           'modules': modules, 'rson': rson})
        else:
            (src_root / 'DATA' / 'Script').mkdir(parents=True, exist_ok=True)
            shutil.copy2(orig_scr, src_root / 'DATA' / 'Script' / orig_scr.name)
            ref = (f'модули {modules.name}/ — справочные'
                   if modules else 'модулей .rsm нет')
            warnings.append(
                f'{scr_name}: .rsm-ветка НЕ воспроизводит оригинальный .scr '
                f'({why_rt}) — скрипт оставлен бинарником (engine=scr), {ref}; '
                f'тексты остаются в Lang.txt')
            opened.append({'name': scr_name, 'engine': 'scr', 'entry': None,
                           'cache': in_cache, 'cache_key': cache_key,
                           'modules': modules, 'rson': rson})
        if scr_name == name:
            src_modules, kept_rson = modules, rson

    # --- отложенные CT-таблицы: чей скрипт остался бинарником, тот файл нужен
    scr_engine = {s['name'].lower(): s['engine'] for s in opened}
    for f, rel, art_of in deferred_ct:
        if scr_engine.get(art_of.lower()) == 'rsmc':
            warnings.append(f'{rel}: таблица CT-ключей (артефакт сборки RScript) — '
                            f'в src/ не перенесена, rsmc пишет свою')
            continue
        dst = src_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)

    rsmc_names = [s['name'] for s in opened if s['engine'] == 'rsmc']
    if rsmc_names:
        # Тексты этих скриптов живут в .rsm (text:) — их секции Script^{<name>}
        # в Lang-исходнике избыточны: rsmc генерит таблицы заново. Вычищаем,
        # чтобы не копить мёртвые ключи; блоки скриптов с engine=scr остаются.
        if primary and primary in lang_texts:
            all_rsmc = len(rsmc_names) == len(opened)
            _strip_lang_script_section(src_root / 'CFG' / primary / 'Lang.txt',
                                       None if all_rsmc else rsmc_names)

    # --- srmod.json / .gitignore ---------------------------------------
    engine = opened[0]['engine'] if opened else 'none'
    if engine == 'rsmc':
        script_cfg = {'engine': 'rsmc',
                      'entry': f'src/DATA/Script/{name}.src/main.rsm'}
    elif engine == 'scr':
        script_cfg = {'engine': 'scr'}
    else:
        script_cfg = {'engine': 'none'}
    if opened and not opened[0].get('cache', True):
        script_cfg['cache'] = False       # автор не внёс его в CacheData
    if has_script and main_text is not None and not re.search(
            r'=\s*1\s*,\s*Script\.' + re.escape(name) + r'\s*$',
            main_text, flags=re.M | re.I):
        # Мод собран без связки в Main.dat (RefQuest) — скрипт запускает
        # кто-то другой. Наше дело воспроизвести мод, а не чинить: гейт
        # build'а для него выключается явным флагом.
        script_cfg['main_link'] = False
        warnings.append(f'Main.dat не подключает {name}.scr '
                        f'(=1,Script.{name}) — так в оригинале; гейт связки '
                        f'выключен флагом script.main_link=false')
    had_install_txt = install_txt_generated
    project = {
        'name': name,
        'install': install,
        # Мод без Lang.dat вообще (BlockSOTE и т.п.) — законный случай: язык не
        # выдумываем, иначе build потребует несуществующий src/CFG/Rus/Lang.txt.
        'languages': languages,
        'primary_lang': primary,
        'script': script_cfg,
        'media': media_cfg,
        'packages': packages,
        'install_txt': had_install_txt,
        'deploy': {'mode': 'junction'},
    }
    if opened and opened[0].get('cache_key') \
            and opened[0]['cache_key'] != opened[0]['name']:
        script_cfg['cache_key'] = opened[0]['cache_key']
    if len(opened) > 1:
        project['scripts'] = [
            dict({'name': s['name'], 'engine': s['engine'], 'entry': s['entry'],
                  'cache': s.get('cache', True)},
                 **({'cache_key': s['cache_key']}
                    if s.get('cache_key') and s['cache_key'] != s['name'] else {}))
            for s in opened]
    (dest / 'srmod.json').write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (dest / '.gitignore').write_text('/build/\n/.srmod/\n', encoding='utf-8')

    return dest, {
        'name': name, 'install': install, 'has_script': has_script,
        'engine': engine, 'languages': languages, 'packages': packages,
        'scripts': [{'name': s['name'], 'engine': s['engine']} for s in opened],
        'decoded': stats.decoded, 'kept_binary': stats.kept_binary,
        'src_modules': src_modules, 'kept_rson': kept_rson,
        'warnings': warnings,
    }


def print_report(dest, report):
    _log(f'OK: мод открыт -> {dest}')
    scripts = report.get('scripts') or []
    if len(scripts) > 1:
        script_desc = ', '.join(f'{s["name"]}={s["engine"]}' for s in scripts)
    else:
        script_desc = report['engine'] if report['has_script'] else 'нет'
    _log(f'  name={report["name"]}  install={report["install"]}  '
         f'script={script_desc}  '
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
