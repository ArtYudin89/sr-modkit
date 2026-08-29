"""`srmod build` — конвейер src/ -> build/ (docs/STAGE0.md, б).

Раскладка src/build (2026-08-04): все исходники в `src/`, сборка целиком в
`build/` — game-ready дерево без исходников и комментариев, его junction'ом
кладут в Mods\\. `build/` каждый раз собирается с нуля (иначе переименованный
исходник оставлял бы в нём осиротевший артефакт).

Порядок шагов и гейты не придуманы заново — калька со спеки, которая сама
калька с рук пощупанных граблей rsmc (см. project_sr_vscode/project_scr_decompiler).
Инструменты (rsmc/BlockParEditor/srgi.py/srpkg.py) вызываются как внешние
процессы: ни формат .dat, ни бинарник rsmc не наши, реимплемент запрещён.
"""
import json
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

# Сайдкары srmod open: формат кадра / метка "нативный png" — не часть мода.
_MEDIA_SIDECAR_SUFFIXES = ('.gi.json', '.gai.json', '.hai.json', '.png.json')

# Файлы src/, которые НЕ копируются в build/ как есть: они либо компилируются
# в артефакт с другим расширением, либо вообще не нужны игре.
_NO_COPY_SUFFIXES = ('.rsm', '.png', '.rson') + _MEDIA_SIDECAR_SUFFIXES


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


def looks_like_utf8_datnik(data):
    """Тот же признак, что у гейта, но без исключения — для файлов, которые мы
    только копируем (см. copy_static)."""
    try:
        assert_not_utf8_datnik(Path('.'), data)
    except BuildError:
        return True
    return False


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

def project_scripts(cfg):
    """Скрипты мода: [{'name', 'engine', 'entry'}].

    Обычный мод описан парой "name" + "script" — тогда список из одного
    элемента. Мультискриптовый (в корпусе 28 из 236 скриптовых модов везут по
    2–5 .scr) перечисляет их в "scripts"; каждому нужен свой вызов rsmc, свой
    каркас Script^{} в Lang, своя строка в Main.dat и в CacheData.
    """
    script_cfg = cfg.project.get('script') or {}
    default_engine = script_cfg.get('engine', 'rsmc')
    listed = cfg.project.get('scripts') or []
    if not listed:
        name = cfg.project.get('name')
        if not name:
            raise BuildError('srmod.json: "name" не задан')
        return [{'name': name, 'engine': default_engine,
                 'entry': script_cfg.get('entry'),
                 'cache': script_cfg.get('cache', True),
                 'cache_key': script_cfg.get('cache_key') or name}]
    out = []
    for i, item in enumerate(listed):
        if not isinstance(item, dict) or not item.get('name'):
            raise BuildError(f'srmod.json: scripts[{i}] без "name"')
        out.append({'name': item['name'],
                    'engine': item.get('engine', default_engine),
                    'entry': item.get('entry'),
                    # Попадает ли скрипт в секцию Script CacheData.dat. Это
                    # авторский факт, а не правило: у BlockSOTE в CacheData оба
                    # скрипта, у RefGreeting из шести — только основной.
                    'cache': item.get('cache', True),
                    # Ключ записи в CacheData: обычно равен имени скрипта, но
                    # не обязан (Cat_Tardis: `tardis=…\cat_tardis.scr`).
                    'cache_key': item.get('cache_key') or item['name']})
    return out


def resolve_entry(cfg, script=None):
    script = script or project_scripts(cfg)[0]
    name = script['name']
    src = cfg.src_dir
    entry_cfg = script.get('entry')
    candidates = []
    if entry_cfg:
        candidates.append(cfg.root / entry_cfg)
    candidates.append(src / 'DATA' / 'Script' / f'{name}.src' / 'main.rsm')
    candidates.append(src / 'DATA' / 'Script' / f'{name}.rsm')
    for c in candidates:
        if c.exists():
            return c
    hint = ''
    if not src.is_dir() and (cfg.root / 'DATA').is_dir():
        hint = (f'\nПохоже, мод в старой раскладке (исходники в корне): '
                f'перенесите ModuleInfo.txt, CFG/ и DATA/ в {src}')
    tried = ', '.join(str(c) for c in candidates)
    raise BuildError(f'точка входа .rsm не найдена (пробовали: {tried}){hint}')


def check_script_name(cfg, entry, out_scr, name=None):
    assert_rsm_is_utf8(entry)
    text = entry.read_text(encoding='utf-8')
    m = re.search(r'scriptName\(\s*"([^"]+)"\s*\)', text)
    if not m:
        raise BuildError(f'{entry}: не найден scriptName("...")')
    script_name = m.group(1)
    name = name or cfg.project['name']
    if script_name != name:
        raise BuildError(f'{entry}: scriptName("{script_name}") != srmod.json:name ("{name}")')
    if out_scr.stem != name:
        raise BuildError(f'выходной .scr назван "{out_scr.stem}", ожидался "{name}"')
    return script_name


def check_main_script_link(path, data, names):
    """Гейт: раз проект собирает скрипт, Main.txt обязан его подключать.

    Без строки вида <ключ>=1,Script.<name> внутри Data^{ Script^{ } } мод виден
    в списке модов, но скрипт не запускается — тихий отказ ровно того класса,
    который srmod обязан ловить. Форма сверена с реальными модами игры
    (Main.dat -> txt: AdvancedOptions, AMod_HardSkills, AMod_Invaders,
    AMod_DefStation — у всех одинаково).

    Проверяется только ПРАВАЯ часть: ключ слева — произвольное имя записи и с
    именем скрипта совпадать не обязан (AMod_Merchant:
    `mod_trader=1,Script.mod_merchant`)."""
    text, _enc = _decode_datnik(data)
    if isinstance(names, str):
        names = [names]
    missing = [n for n in names
               if not re.search(r'=\s*1\s*,\s*Script\.' + re.escape(n) + r'\s*$',
                                text, flags=re.M | re.I)]
    if missing:
        lines = '\n'.join(f'        {n}=1,Script.{n}' for n in missing)
        raise BuildError(
            f'{path}: нет связки скрипта — игра не запустит '
            f'{", ".join(n + ".scr" for n in missing)}.\n'
            f'Добавьте внутрь Data ^{{ ... }} блок:\n'
            f'    Script ^{{\n{lines}\n'
            f'    }}')


# --------------------------------------------------------------------- 0. каркас build/

def reset_build_dir(cfg):
    """build/ собирается с нуля: остаточный артефакт от переименованного
    исходника иначе уедет к игрокам, и никто этого не заметит."""
    build = cfg.build_dir
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    return build


def copy_static(cfg):
    """src/** -> build/** для всего, что игра читает как есть.

    Не копируются: компилируемые исходники (*.rsm -> .scr, *.png -> .gi,
    CFG/**/*.txt -> .dat, *.rson — служебный), и любые папки `*.src/`
    (модули скрипта, кадры анимаций, содержимое пакетов) — их судьба решается
    своими шагами. Всё остальное (звуки, готовые ресурсы) — часть мода."""
    src, build = cfg.src_dir, cfg.build_dir
    if not src.is_dir():
        raise BuildError(f'{src}: каталог исходников не найден')

    mi = src / 'ModuleInfo.txt'
    if not mi.exists():
        raise BuildError(f'{mi}: обязателен — без него игра не видит мод')

    # Компилируемые датники-источники: не копируются как есть. Остальные
    # CFG/**/*.txt — самостоятельные файлы мода, уходят в build копией.
    languages = cfg.project.get('languages') or []
    cfg_sources = {('cfg', 'main.txt'), ('cfg', 'cachedata.txt')}
    cfg_sources |= {('cfg', lang.lower(), 'lang.txt') for lang in languages}

    # Наши исходники скриптов: их компилирует rsmc, в build они не едут. Всё
    # остальное с теми же расширениями — файлы мода (авторы шипят рядом со
    # скриптом свой .rson/.rsm: Cat_Yamato, UtilityFunctionsPack) и обязаны
    # доехать до сборки, иначе пересборка теряет файлы оригинала.
    own_sources = set()
    for s in project_scripts(cfg):
        own_sources.add(f'{s["name"].lower()}.rsm')
        own_sources.add(f'{s["name"].lower()}.import.rson')
        entry = s.get('entry')
        if entry:
            own_sources.add(Path(entry).name.lower())

    copied, utf8_copies = 0, []
    for dirpath, dirnames, filenames in os.walk(src):
        dirpath = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not d.lower().endswith('.src')]
        rel_dir = dirpath.relative_to(src)
        for f in filenames:
            low = f.lower()
            if low.endswith(('.rsm', '.rson')):
                if low in own_sources:
                    continue
            elif low.endswith(_NO_COPY_SUFFIXES):
                continue
            rel_key = tuple(p.lower() for p in rel_dir.parts) + (low,)
            if rel_key in cfg_sources:
                continue  # датники-источники: компилируются в .dat своими шагами
            # Кодировку проверяем только у датников, которые компилируем сами
            # (cfg_sources выше). Прочие .txt едут копией, и ронять сборку
            # из-за них нельзя: у 25 модов корпуса такие файлы лежат в UTF-8
            # (PlanetaryBattles: CFG/Rus/Robots/cache.txt), игра их читает, и
            # наше дело — воспроизвести мод как есть.
            if low.endswith('.txt') and looks_like_utf8_datnik((dirpath / f).read_bytes()):
                utf8_copies.append((rel_dir / f).as_posix())
            dst = build / rel_dir / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dirpath / f, dst)
            copied += 1
    return copied, utf8_copies


# --------------------------------------------------------------------- 1. медиа

def _srgi_encode(cfg, run_py, src, dst, fmt, results):
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(run_py), 'gi', 'encode', str(src), str(dst), '--format', fmt],
        capture_output=True, text=True, errors='replace')
    out = (proc.stdout or '') + (proc.stderr or '')
    ok = proc.returncode == 0 and dst.exists()
    results.append((str(src), str(dst), ok, out.strip()))
    if not ok:
        raise BuildError(f'srgi encode {src} -> {dst} провалился:\n{out.strip()}')


def _sidecar_format(dirpath, stem, kind, default):
    """Формат кадра из сайдкара <stem>.<kind>.json (пишет srmod open для
    медиа, чей оригинальный формат отличается от дефолта srmod.json)."""
    sc = dirpath / f'{stem}.{kind}.json'
    if sc.exists():
        try:
            return json.loads(sc.read_text(encoding='utf-8')).get('format', default)
        except (OSError, ValueError):
            pass
    return default


def _is_raw_png(dirpath, stem):
    """<stem>.png.json {"raw": true}: png — нативный ресурс мода (игра умеет
    читать png, в т.ч. из pkg), копировать как есть, а не кодировать в .gi."""
    sc = dirpath / f'{stem}.png.json'
    if sc.exists():
        try:
            return bool(json.loads(sc.read_text(encoding='utf-8')).get('raw'))
        except (OSError, ValueError):
            pass
    return False


def encode_media_tree(cfg, run_py, src_root, dst_root, results, skip_pkg_src):
    """png->gi, *.gai.src/->gai, *.hai.src/->hai: src_root/** -> dst_root/**
    (то же относительное место). skip_pkg_src=True на общем проходе по DATA —
    медиа внутри пакетов кодируются отдельно, прямо в стейджинг пакета."""
    media_cfg = cfg.project.get('media') or {}
    gi_fmt = media_cfg.get('gi_format', 'argb')
    gai_fmt = media_cfg.get('gai_format', 'delta')

    for dirpath, dirnames, filenames in os.walk(src_root):
        dirpath = Path(dirpath)
        rel = dirpath.relative_to(src_root)
        keep = []
        for d in list(dirnames):
            low = d.lower()
            if low.endswith('.gai.src'):
                stem = d[:-len('.gai.src')]
                _srgi_encode(cfg, run_py, dirpath / d,
                             dst_root / rel / (stem + '.gai'),
                             _sidecar_format(dirpath, stem, 'gai', gai_fmt),
                             results)
            elif low.endswith('.hai.src'):
                stem = d[:-len('.hai.src')]
                _srgi_encode(cfg, run_py, dirpath / d,
                             dst_root / rel / (stem + '.hai'),
                             _sidecar_format(dirpath, stem, 'hai', gi_fmt),
                             results)
            elif low.endswith('.pkg.src') and skip_pkg_src:
                pass        # содержимое пакета кодируется на шаге pkg
            elif low.endswith('.src'):
                pass        # модули скрипта <Name>.src/ — не медиа
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f.lower().endswith('.png'):
                stem = Path(f).stem
                if _is_raw_png(dirpath, stem):
                    dst = dst_root / rel / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dirpath / f, dst)
                    continue
                _srgi_encode(cfg, run_py, dirpath / f,
                             dst_root / rel / (stem + '.gi'),
                             _sidecar_format(dirpath, stem, 'gi', gi_fmt),
                             results)


def run_media(cfg):
    """Медиа ищем по ВСЕМУ дереву мода, не только в DATA/.

    Мод не обязан складывать картинки в DATA: у семейства PlanetaryBattles они
    лежат в собственном корневом каталоге `matrix/` — при обходе одной только
    DATA такие файлы не доезжали до build вовсе (`.png` не копируется как есть,
    его положено кодировать).
    """
    if not cfg.src_dir.is_dir():
        return []
    decompiler = cfg.tool('decompiler')
    if not decompiler:
        raise BuildError('не найден decompiler (rson-decompiler) — нужен для srgi (медиа)')
    run_py = Path(decompiler) / 'run.py'
    results = []
    encode_media_tree(cfg, run_py, cfg.src_dir, cfg.build_dir, results,
                      skip_pkg_src=True)
    return results


# --------------------------------------------------------------------- 2/4. язык

def stage_lang(cfg, tmpdir, skeleton=True, require_primary=None, skeleton_names=None):
    """src/CFG/<lang>/*.txt -> один временный файл на язык; в файл primary_lang
    добавляется каркас Script ^{ <name> ^{ } }, если такого блока ещё нет
    (skeleton=False — merge rsmc не будет: мод без скрипта или engine=scr).

    skeleton_names — имена всех rsmc-скриптов мода: у мультискриптового мода
    каждый вызов rsmc мержит свою таблицу в СВОЙ блок, и каркас нужен каждому.
    """
    if require_primary is None:
        require_primary = skeleton
    names = list(skeleton_names) if skeleton_names else [cfg.project['name']]
    primary = cfg.project.get('primary_lang')
    languages = cfg.project.get('languages') or ([primary] if primary else [])
    staged = {}
    for lang in languages:
        lang_dir = cfg.src_dir / 'CFG' / lang
        if not lang_dir.is_dir():
            continue
        # Только Lang.txt: прочие *.txt в языковом каталоге — самостоятельные
        # файлы мода (копируются в build как есть), мержить их в Lang.dat
        # нельзя (иначе, напр., затесавшийся файл дублирует секции).
        txt_files = [lang_dir / 'Lang.txt'] if (lang_dir / 'Lang.txt').exists() else []
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
        if lang == primary and skeleton:
            # Проверять по тексту БЕЗ комментариев: упоминание "Script ^{ X ^{"
            # в комментарии иначе выдаёт себя за существующий блок, каркас не
            # подставляется и rsmc молча (rc=0) пропускает слияние текстов.
            no_comments = re.sub(r'/\*.*?\*/', '', combined, flags=re.S)
            no_comments = re.sub(r'//[^\n]*', '', no_comments)
            # [\^~]: у реальных модов блок Script бывает и ~{ (сортированный
            # вариант BlockPar) — это тоже существующий блок, каркас не нужен.
            need = [n for n in names
                    if not re.search(r'Script\s*[\^~]\{\s*' + re.escape(n) + r'\s*[\^~]\{',
                                     no_comments)]
            if need:
                # rsmc парсит блок построчно (не полноценный токенайзер): каркас
                # ОБЯЗАН быть на отдельных строках — однострочный
                # "Script ^{ Name ^{ } }" молча не матчится ("has no existing
                # ... block to merge into"), проверено руками.
                inner = ''.join(f'    {n} ^{{\n    }}\n' for n in need)
                lines = combined.split('\n')
                at = next((i for i, ln in enumerate(lines)
                           if re.match(r'^\s*Script\s*[\^~]\{\s*$', ln)
                           and not ln.strip().startswith('//')), None)
                if at is not None:
                    # Блок Script уже есть (мультискриптовый мод, где часть
                    # скриптов осталась бинарной) — каркасы кладём ВНУТРЬ него.
                    # Второй корневой блок с тем же именем даёт .dat, который
                    # BlockParEditor 1.9 потом не читает назад (проверено на
                    # KavkazScripts: 9 скриптов, "no output written").
                    lines[at + 1:at + 1] = inner.rstrip('\n').split('\n')
                    combined = '\n'.join(lines)
                else:
                    # Каркас ставится ПЕРЕД остальным содержимым, не после.
                    # Грабля BlockParEditor 1.9, проверена руками: корневой
                    # plain "key=value" ПЕРЕД вложенным "Key ^{ }" даёт .dat,
                    # который сам BPE не может прочитать назад (round-trip
                    # молча ломается — write проходит без единой ошибки).
                    combined = f'Script ^{{\n{inner}}}\n' + combined
        tmp_path = tmpdir / f'{lang}.Lang.txt'
        _encode_datnik(tmp_path, combined, enc)
        staged[lang] = tmp_path
    if require_primary and primary and primary not in staged:
        raise BuildError(f'src/CFG/{primary}/*.txt (primary_lang) не найдены')
    return staged


def check_secondary_lang_keys(staged, primary, name, ct_table, log):
    """Ключи перевода неосновных языков обязаны совпасть с таблицей rsmc.

    Связь реплики с переводом в этом формате только по номеру ключа, а номера
    rsmc раздаёт заново при каждой сборке: стоит поправить тексты в .rsm — и
    перевод молча съезжает (в игре второй язык показывает чужие строки или
    служебные ключи). `srmod open` переключает перевод на актуальную нумерацию,
    дальше согласие держит этот гейт.
    """
    if not ct_table:
        return
    fresh = set(ct_table)
    for lang, path in staged.items():
        if lang == primary:
            continue
        text, _enc = _decode_datnik(path.read_bytes())
        keys, depth, in_name = set(), 0, False
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r'^(\S+)\s*[\^~]\{$', s)
            if m:
                depth += 1
                if depth == 2 and m.group(1) == name:
                    in_name = True
                continue
            if s == '}':
                if in_name and depth == 2:
                    in_name = False
                depth -= 1
                continue
            if in_name and '=' in s:
                keys.add(s.split('=', 1)[0].strip())
        if not keys:
            continue
        # Лишние ключи перевода безвредны (мёртвые записи), нехватка — это
        # реплики без перевода, и у автора их часть могла не быть переведена
        # изначально. Ошибка — только полный развал: ни один ключ не совпал.
        if not (keys & fresh):
            # Ронять сборку нельзя: так бывает и у оригинального мода (ExpArts —
            # перевод Eng ведёт нумерацию, которой в основном языке нет вовсе,
            # мост «текст -> новый ключ» не построить). Но сказать надо громко.
            log(f'  ! {lang}: перевод скрипта {name} не совпадает с таблицей '
                f'этой сборки НИ ОДНИМ ключом — в игре {lang} останется без '
                f'текста. Перенесите перевод на номера из '
                f'build/DATA/Script/{name}.txt или соберите мод веткой engine=scr.')
            continue
        stale, missing = len(keys - fresh), len(fresh - keys)
        if stale:
            log(f'  ! {lang}: {stale} ключ(ей) перевода не ведут никуда '
                f'(тексты .rsm менялись?)')
        if missing:
            log(f'  {lang}: без перевода {missing} реплик(и) из {len(fresh)}')


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
    # error: даёт rc=8, но warning: — rc=0 (мод молча остаётся без текстов),
    # поэтому гейт по тексту вывода, rc — только запасная проверка.
    if proc.returncode != 0 or 'error:' in low:
        raise BuildError(f'rsmc rc={proc.returncode}: {out}')
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
    """txt -> dat через BlockParEditor, ВСЕГДА под нейтральным именем.

    Грабля BPE 1.9, найдена прогоном корпуса (моды Den_DatVersion) и проверена
    изолированно: если имя входа ИЛИ выхода — `CacheData`, редактор требует в
    тексте секцию `Script` и, не найдя её, молча пишет ПУСТОЙ .dat (8 байт,
    ни ошибки, ни диалога). Такой мод (CacheData только с секцией BV) собирался
    в мусор. Под именем `srmod_bp` тот же текст конвертируется нормально —
    поэтому конвертируем во временном каталоге и кладём результат на место.
    """
    blockpar = cfg.tool('blockpar')
    if not blockpar:
        raise BuildError('blockpar (BlockParEditor.exe) не найден')
    decomp_run = _import_decompiler_run(cfg)
    dst_dat = Path(dst_dat)
    dst_dat.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='srmod_bp_') as tmp:
        neutral_txt = Path(tmp) / 'srmod_bp.txt'
        neutral_dat = Path(tmp) / 'srmod_bp.dat'
        shutil.copy2(src_txt, neutral_txt)
        ok, msg = decomp_run._run_blockpar(Path(blockpar), neutral_txt, neutral_dat)
        if not ok:
            raise BuildError(f'blockpar {src_txt} -> {dst_dat}: {msg}')
        shutil.copy2(neutral_dat, dst_dat)
    return dst_dat


# --------------------------------------------------------------------- 6. CacheData

def generate_cachedata_txt(cfg, tmpdir, include_script=True, script_names=None):
    """Секция Script генерируется из install; остальные секции (Bm/Sound/
    ABMap/... — реверс по корпусу 2026-08-04: ключи семантические, авторские,
    автогенерация невозможна) берутся из src/CFG/CacheData.txt, где $INSTALL$
    заменяется на актуальный путь установки. Возвращает None, если CacheData
    моду не нужен вовсе (нет ни скрипта, ни сохранённых секций)."""
    # script_names — [(имя скрипта, ключ записи)] или просто имена.
    pairs = [(n, n) if isinstance(n, str) else tuple(n)
             for n in (script_names or [cfg.project['name']])]
    names = [n for n, _k in pairs]
    install = cfg.project.get('install')
    if not install:
        raise BuildError('srmod.json: "install" не задан (нужен для CacheData/INSTALL.TXT)')
    install_win = install.replace('/', '\\').rstrip('\\')
    rows = [f'    {key}={install_win}\\DATA\\Script\\{n}.scr' for n, key in pairs] \
        if include_script else []
    parts = []
    preserved = cfg.src_dir / 'CFG' / 'CacheData.txt'
    if preserved.exists():
        data = preserved.read_bytes()
        assert_not_utf8_datnik(preserved, data)
        text, _enc = _decode_datnik(data)
        text = text.replace('$INSTALL$', install_win)
        from .opener import split_top_sections   # lazy: opener импортирует нас
        # Записи о СВОИХ скриптах генерируются здесь (пути зависят от install), и
        # дублировать их в исходнике нельзя. Но секция Script бывает и про чужие
        # скрипты — мод, переопределяющий скрипты самой игры (Den_DatVersion:
        # ms_begin, ms_blazer), держит их здесь, и они обязаны доехать. Такие
        # строки сливаются в ОДИН блок Script с генерируемыми: второй корневой
        # блок с тем же именем даёт .dat, который BPE не читает назад.
        for sec_name, chunk in split_top_sections(text):
            if sec_name != 'Script':
                parts.append(chunk.rstrip('\n') + '\n')
                continue
            for ln in chunk.splitlines():
                s = ln.strip()
                if '=' not in s or s.startswith('//'):
                    continue
                key = s.split('=', 1)[0].strip()
                if key in {k for _n, k in pairs}:
                    # Имя скрипта мода бывает и ключом в других секциях
                    # (Cat_Dragon: `Dragon=…\Ship\Dragon.hai` в Bm) — потому
                    # проверяем ТОЛЬКО внутри Script.
                    raise BuildError(
                        f'{preserved}: запись о скрипте мода "{key}" в секции '
                        f'Script запрещена — её генерирует build из install '
                        f'(уберите строку)')
                rows.append(ln.rstrip())
    if rows:
        parts.insert(0, 'Script ^{\n' + '\n'.join(rows) + '\n}\n')
    if not parts:
        return None
    tmp_txt = tmpdir / 'CacheData.txt'
    _encode_datnik(tmp_txt, '\n'.join(parts), 'utf-16le')
    return tmp_txt


# --------------------------------------------------------------------- 7. pkg

def _stage_pkg_tree(cfg, run_py, pkg_src, stage, results):
    """Содержимое X.pkg.src/ -> стейджинг: медиа кодируются, остальное копия.
    png/times.json в архив не попадают никогда (это исходники кадров)."""
    for dirpath, dirnames, filenames in os.walk(pkg_src):
        dirpath = Path(dirpath)
        rel = dirpath.relative_to(pkg_src)
        (stage / rel).mkdir(parents=True, exist_ok=True)
        for f in filenames:
            low = f.lower()
            if (low.endswith('.png') or low.endswith('.times.json')
                    or low.endswith(_MEDIA_SIDECAR_SUFFIXES)):
                continue
            shutil.copy2(dirpath / f, stage / rel / f)
    encode_media_tree(cfg, run_py, pkg_src, stage, results, skip_pkg_src=False)


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
        pkg_src = cfg.src_dir / pkg_src_rel
        if not pkg_src.is_dir():
            raise BuildError(f'пакет не найден: {pkg_src} (путь в "packages" — от src/)')
        if not pkg_src.name.lower().endswith('.pkg.src'):
            raise BuildError(f'{pkg_src}: ожидается суффикс .pkg.src')
        rel = pkg_src.relative_to(cfg.src_dir)
        pkg_out = cfg.build_dir / rel.parent / (pkg_src.name[:-len('.pkg.src')] + '.pkg')
        pkg_out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='srmod_pkgstage_') as stage_root:
            stage = Path(stage_root) / pkg_src.name
            _stage_pkg_tree(cfg, run_py, pkg_src, stage, [])
            proc = subprocess.run(
                [sys.executable, str(run_py), 'pkg', 'pack', str(stage), str(pkg_out)],
                capture_output=True, text=True, errors='replace')
            out = ((proc.stdout or '') + (proc.stderr or '')).strip()
            if proc.returncode != 0 or not pkg_out.exists():
                raise BuildError(f'srpkg pack {pkg_src} -> {pkg_out}:\n{out}')
        built.append(pkg_out)
    return built


def generate_install_txt(cfg, built_pkgs):
    """INSTALL.TXT перечисляет ВСЕ пакеты мода — и собранные из *.pkg.src/, и
    приехавшие бинарниками (архив больше --pkg-limit не разворачивается, но
    игре его всё равно надо смонтировать: Den_UIRecolor)."""
    pkgs = list(built_pkgs or [])
    known = {p.resolve() for p in pkgs}
    if cfg.build_dir.is_dir():
        pkgs += [p for p in sorted(cfg.build_dir.rglob('*.pkg'))
                 if p.is_file() and p.resolve() not in known]
    built_pkgs = pkgs
    if not built_pkgs or not cfg.project.get('install_txt', True):
        return None
    install = cfg.project.get('install').replace('/', '\\').rstrip('\\')
    lines = ['Packages {']
    for pkg in built_pkgs:
        rel = str(pkg.relative_to(cfg.build_dir)).replace('/', '\\')
        lines.append(f'    Package={install}\\{rel}')
    lines.append('}')
    text = '\n'.join(lines) + '\n'
    out = cfg.build_dir / 'INSTALL.TXT'
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

    scripts = project_scripts(cfg)
    for s in scripts:
        engine = s['engine']
        if engine == 'rsmc':
            s['entry_path'] = resolve_entry(cfg, s)
            log(f'entry: {s["entry_path"]}')
        elif engine == 'scr':
            # Пассthrough: .rsm-ветка не воспроизводит оригинал (см. srmod open) —
            # источник скрипта = бинарный src/DATA/Script/<name>.scr, тексты — в
            # Lang.txt с оригинальными CT-ключами. copy_static скопирует его сам.
            scr_src = cfg.src_dir / 'DATA' / 'Script' / f'{s["name"]}.scr'
            if not scr_src.exists():
                raise BuildError(f'{scr_src}: engine=scr, но бинарного .scr нет')
            log(f'script: {scr_src.name} (engine=scr, бинарный passthrough)')
        elif engine == 'none':
            log('script: нет (engine=none) — мод без скрипта')
        else:
            raise BuildError(f'srmod.json: неизвестный script.engine "{engine}" '
                              f'(ожидается rsmc | scr | none)')
    live = [s for s in scripts if s['engine'] != 'none']
    has_script = bool(live)
    rsmc_scripts = [s for s in scripts if s['engine'] == 'rsmc']

    reset_build_dir(cfg)
    copied, utf8_copies = copy_static(cfg)
    if copied:
        log(f'static: {copied} файл(ов) скопировано в build/')
    for rel in utf8_copies:
        log(f'  ! {rel}: UTF-8 без BOM — так лежало в оригинале мода, копируем '
            f'как есть; свои датники держите в UTF-16LE+BOM')

    media_results = run_media(cfg)
    if media_results:
        log(f'медиа: {len(media_results)} файл(ов) закодировано')

    for s in live:
        s['out_scr'] = cfg.build_dir / 'DATA' / 'Script' / f'{s["name"]}.scr'
        if s['engine'] == 'rsmc':
            check_script_name(cfg, s['entry_path'], s['out_scr'], s['name'])
    out_scr = live[0]['out_scr'] if live else None

    with tempfile.TemporaryDirectory(prefix='srmod_build_') as tmp:
        tmpdir = Path(tmp)
        # Каркас Script^{} и merge таблицы rsmc нужны только rsmc-ветке:
        # у engine=scr тексты уже лежат в Lang.txt с оригинальными ключами.
        staged = stage_lang(cfg, tmpdir, skeleton=bool(rsmc_scripts),
                            require_primary=has_script,
                            skeleton_names=[s['name'] for s in rsmc_scripts])
        primary = cfg.project.get('primary_lang')
        primary_txt = staged.get(primary) if primary else None

        # Каждый скрипт мержит СВОЮ таблицу CT в тот же staged-файл языка:
        # rsmc дописывает её в собственный блок Script^{<name>}, соседние не
        # трогает (проверено на мультискриптовых модах корпуса).
        for s in rsmc_scripts:
            log(f'rsmc build {s["entry_path"].name} -> {s["out_scr"].name}'
                + (f' (--lang-txt {primary_txt.name})' if primary_txt else ''))
            rsmc_out = run_rsmc(cfg, s['entry_path'], s['out_scr'], primary_txt)
            if rsmc_out:
                log(f'  {rsmc_out}')
            if len(staged) > 1:
                from .rebuild import _read_ct_table
                check_secondary_lang_keys(
                    staged, primary, s['name'],
                    _read_ct_table(s['out_scr'].with_suffix('.txt')), log)

        lang_dats = {}
        for lang, txt_path in staged.items():
            dat_path = cfg.build_dir / 'CFG' / lang / 'Lang.dat'
            run_blockpar(cfg, txt_path, dat_path)
            lang_dats[lang] = dat_path
            log(f'lang: {lang}: {txt_path.name} -> {dat_path}')

        main_txt = cfg.src_dir / 'CFG' / 'Main.txt'
        if main_txt.exists():
            data = main_txt.read_bytes()
            assert_not_utf8_datnik(main_txt, data)
            if has_script and (cfg.project.get('script') or {}).get('main_link', True):
                # Связку в Main.dat требуем только для ОСНОВНОГО скрипта:
                # вспомогательные .scr мультискриптового мода игра подхватывает
                # через CacheData (BlockSOTE: PC_destroyer есть в CacheData, в
                # Main.dat его нет — так у всех модов корпуса).
                main_name = name if any(s['name'] == name for s in live) else live[0]['name']
                check_main_script_link(main_txt, data, [main_name])
            main_dat = cfg.build_dir / 'CFG' / 'Main.dat'
            run_blockpar(cfg, main_txt, main_dat)
            log(f'main: {main_txt.name} -> {main_dat}')

        cache_dat = None
        cached = [(s['name'], s.get('cache_key') or s['name'])
                  for s in live if s.get('cache', True)]
        if (cfg.src_dir / 'CFG' / 'CacheData.dat').exists():
            # Бинарный исходник (не прочитался как текст при открытии) — он уже
            # скопирован статикой, генерировать поверх нельзя.
            cache_txt = None
            log('cachedata: бинарный исходник src/CFG/CacheData.dat — копия как есть')
        else:
            cache_txt = generate_cachedata_txt(cfg, tmpdir,
                                               include_script=bool(cached),
                                               script_names=cached)
        if cache_txt is not None:
            cache_dat = cfg.build_dir / 'CFG' / 'CacheData.dat'
            run_blockpar(cfg, cache_txt, cache_dat)
            log(f'cachedata: -> {cache_dat}')

        built_pkgs = build_packages(cfg)
        if built_pkgs:
            log(f'pkg: {len(built_pkgs)} архив(ов) собрано')
        install_txt = generate_install_txt(cfg, built_pkgs)
        if install_txt:
            log(f'install: -> {install_txt}')

    log(f'OK: сборка завершена -> {cfg.build_dir}')
    return {
        'scr': out_scr,
        'scrs': [s['out_scr'] for s in live],
        'lang_dats': lang_dats,
        'cache_dat': cache_dat,
        'packages': built_pkgs,
    }
