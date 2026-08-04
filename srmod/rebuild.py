"""Гейт пересборки этапа C: `srmod build` из открытого src/ обязан
воспроизводить оригинальный компилированный мод.

Сравнение по классам файлов (байт-в-байт почти нигде не бывает законно):
- .scr: байты -> декомпилированная структура без шапки/Pos.* -> схлопнутые
  пробелы -> CT-ключи разрешены в тексты через таблицу СВОЕЙ сборки
  (нумерация у rsmc и RScript своя — сравнивать можно только тексты).
- .dat: оба конвертируются BlockParEditor'ом в текст и сравниваются построчно;
  для Lang.dat поддерево Script^{<name>} сравнивается мягко (rsmc мержит свою
  таблицу поверх оригинальных ключей — расхождение там ИНФОРМАЦИОННОЕ, если
  сам .scr сошёлся после CT-разрешения).
- INSTALL.TXT: построчно без регистра и порядка.
- остальное: байты (медиа декомпилировались только при чистом round-trip,
  так что законно обязаны совпадать).
"""
import re
from pathlib import Path

from .build import BuildError, build as run_build
from .opener import dat_to_text, is_ct_table_file, split_top_sections
from .verify import (HEADER_SKIP, POS_KEYS, _decompile, _first_diff,
                     _import_decompiler, _norm_ws, _strip)


def _read_ct_table(path):
    """DATA/Script/<Name>.txt: 'N=текст' (UTF-16LE+BOM). -> dict."""
    if not path.exists():
        return None
    raw = path.read_bytes()
    if raw[:2] == b'\xff\xfe':
        text = raw[2:].decode('utf-16-le')
    else:
        text = raw.decode('cp1251', errors='replace')
    table = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            table[k.strip()] = v
    return table


def ct_table_from_lang(lang_text, name):
    """Таблица CT из секции Script^{<name>^{...}} текстового дампа Lang.dat —
    у оригинальных модов отдельного файла-таблицы может не быть вовсе."""
    for sec_name, chunk in split_top_sections(lang_text):
        if sec_name != 'Script':
            continue
        table, in_name, depth = {}, False, 0
        for line in chunk.splitlines():
            s = line.strip()
            m = re.match(r'^(\S+)\s*[\^~]\{$', s)
            if m:
                depth += 1
                if m.group(1) == name and depth == 2:
                    in_name = True
                continue
            if s == '}':
                if in_name and depth == 2:
                    in_name = False
                depth -= 1
                continue
            if in_name and '=' in s:
                k, v = s.split('=', 1)
                table[k.strip()] = v
        if table:
            return table
    return None


def _resolve_ct(obj, name, table):
    """Заменить CT('Script.<name>.<N>')/CT("...") на текст из таблицы."""
    pat = re.compile(r'''CT\(\s*['"]Script\.''' + re.escape(name)
                     + r'''\.(\d+)['"]\s*\)''')

    def repl(m):
        return 'CT(<' + table.get(m.group(1), f'?{m.group(1)}') + '>)'

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, str):
            return pat.sub(repl, x)
        return x
    return walk(obj)


def compare_scr(cfg, name, orig_scr, new_scr, ta, tb, log):
    if orig_scr.read_bytes() == new_scr.read_bytes():
        log(f'  scr: MATCH (байты)')
        return True, True
    decomp_mod, _run = _import_decompiler(cfg)
    drop = HEADER_SKIP + POS_KEYS
    da = _strip(_decompile(decomp_mod, orig_scr), drop)
    db = _strip(_decompile(decomp_mod, new_scr), drop)
    if da == db or _norm_ws(da) == _norm_ws(db):
        log(f'  scr: MATCH (структура; байты нет — CT-ключи/пробелы)')
        return True, False
    if ta is not None and tb is not None:
        ra = _norm_ws(_resolve_ct(da, name, ta))
        rb = _norm_ws(_resolve_ct(db, name, tb))
        if ra == rb:
            log(f'  scr: MATCH (после CT-разрешения — нумерация ключей своя '
                f'у каждого компилятора)')
            return True, False
        diff = _first_diff(ra, rb)
    else:
        diff = _first_diff(_norm_ws(da), _norm_ws(db))
    log(f'  scr: DIFF — {diff}')
    return False, False


def _norm_datnik_lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def compare_dat(cfg, rel, orig_dat, new_dat, name, scr_matched, log):
    ta, tb = dat_to_text(cfg, orig_dat), dat_to_text(cfg, new_dat)
    if rel.name.lower() == 'cachedata.dat':
        # Пути в CacheData у авторских тулз кейс-небрежные (data\ vs DATA\),
        # порядок корневых секций произвольный (Bm до/после Script), игра
        # читает словарь — сравниваем без регистра и без порядка секций.
        def norm(text):
            return sorted(
                (name or '', [ln.lower() for ln in _norm_datnik_lines(chunk)])
                for name, chunk in split_top_sections(text))
        na, nb = norm(ta), norm(tb)
        if na == nb:
            log(f'  {rel}: MATCH (без регистра/порядка секций)')
            return True
        diff = next(((a, b) for a, b in zip(na, nb) if a != b),
                    ('<число секций>', f'{len(na)} vs {len(nb)}'))
        log(f'  {rel}: DIFF — {str(diff[0])[:120]!r} vs {str(diff[1])[:120]!r}')
        return False
    if _norm_datnik_lines(ta) == _norm_datnik_lines(tb):
        log(f'  {rel}: MATCH')
        return True
    if rel.name.lower() == 'lang.dat':
        # Script^{<name>} мержится rsmc'ом заново — сравниваем всё остальное
        # строго, а это поддерево мягко.
        keep_a = [c for n, c in split_top_sections(ta) if n != 'Script']
        keep_b = [c for n, c in split_top_sections(tb) if n != 'Script']
        sa = [c for n, c in split_top_sections(ta) if n == 'Script']
        sb = [c for n, c in split_top_sections(tb) if n == 'Script']
        if _norm_datnik_lines('\n'.join(keep_a)) == _norm_datnik_lines('\n'.join(keep_b)):
            if _norm_datnik_lines('\n'.join(sa)) == _norm_datnik_lines('\n'.join(sb)):
                log(f'  {rel}: MATCH')
                return True
            if scr_matched:
                log(f'  {rel}: MATCH* (секция Script переномерована rsmc — '
                    f'scr сошёлся после CT-разрешения, это согласованно)')
                return True
        for a, b in zip(_norm_datnik_lines(ta), _norm_datnik_lines(tb)):
            if a != b:
                log(f'  {rel}: DIFF — {a!r} vs {b!r}')
                return False
    la, lb = _norm_datnik_lines(ta), _norm_datnik_lines(tb)
    diff = next(((a, b) for a, b in zip(la, lb) if a != b),
                ('<длина>', f'{len(la)} vs {len(lb)}'))
    log(f'  {rel}: DIFF — {diff[0]!r} vs {diff[1]!r}')
    return False


def _norm_install_txt(path):
    raw = path.read_bytes()
    if raw[:2] == b'\xff\xfe':
        text = raw[2:].decode('utf-16-le')
    else:
        text = raw.decode('cp1251', errors='replace')
    return sorted(ln.strip().lower() for ln in text.splitlines() if ln.strip())


def verify_rebuild(cfg, original_dir):
    """Собрать проект и сравнить build/ с оригинальным компилированным модом."""
    original_dir = Path(original_dir).resolve()
    if not original_dir.is_dir():
        raise BuildError(f'{original_dir}: не каталог')
    name = cfg.project.get('name')
    log = print

    log('== rebuild: srmod build ==')
    run_build(cfg, verbose=False)
    build_dir = cfg.build_dir

    log('== rebuild: сравнение с оригиналом ==')
    orig_files = {p.relative_to(original_dir).as_posix().lower(): p
                  for p in original_dir.rglob('*') if p.is_file()}
    new_files = {p.relative_to(build_dir).as_posix().lower(): p
                 for p in build_dir.rglob('*') if p.is_file()}

    # Таблицы CT для разрешения ключей: у оригинала файла-таблицы может не
    # быть (или лежит где угодно) — истина всегда в Lang.dat.
    primary = cfg.project.get('primary_lang')
    ta = tb = None
    if name and primary:
        orig_lang = orig_files.get(f'cfg/{primary.lower()}/lang.dat')
        new_lang = new_files.get(f'cfg/{primary.lower()}/lang.dat')
        if orig_lang:
            ta = ct_table_from_lang(dat_to_text(cfg, orig_lang), name)
        if new_lang:
            tb = ct_table_from_lang(dat_to_text(cfg, new_lang), name)
    if name:
        ta = ta or _read_ct_table(original_dir / 'DATA' / 'Script' / f'{name}.txt')
        tb = tb or _read_ct_table(build_dir / 'DATA' / 'Script' / f'{name}.txt')

    ok = True
    scr_matched = True
    scr_rel = f'data/script/{name}.scr'.lower() if name else None
    # .scr сравниваем первым: от его исхода зависит трактовка Lang.dat.
    order = sorted(set(orig_files) | set(new_files),
                   key=lambda r: (r != scr_rel, r))
    for rel in order:
        a, b = orig_files.get(rel), new_files.get(rel)
        if a is None:
            if name and rel == f'data/script/{name.lower()}.txt':
                log(f'  {rel}: инфо — таблица CT от rsmc (оригинал её не шипил)')
            else:
                log(f'  {rel}: ЛИШНИЙ в пересборке')
                ok = False
            continue
        if b is None:
            if name and is_ct_table_file(Path(a), name):
                log(f'  {rel}: инфо — таблица CT (артефакт сборки оригинала), '
                    f'в пересборку не переносится')
            elif (rel.startswith('cfg/') and rel.endswith('.txt')
                    and Path(a).with_suffix('.dat').exists()):
                log(f'  {rel}: инфо — авторский txt-исходник рядом с .dat, '
                    f'в пересборку не переносится (истина — .dat)')
            else:
                log(f'  {rel}: ОТСУТСТВУЕТ в пересборке')
                ok = False
            continue
        low = rel.lower()
        if low == scr_rel:
            good, _bytes_eq = compare_scr(cfg, name, a, b, ta, tb, log)
            scr_matched = good
            ok = ok and good
        elif low.endswith('.dat'):
            ok = compare_dat(cfg, Path(rel), a, b, name, scr_matched, log) and ok
        elif low == 'install.txt':
            if _norm_install_txt(a) == _norm_install_txt(b):
                log(f'  {rel}: MATCH')
            else:
                log(f'  {rel}: DIFF')
                ok = False
        elif name and low == f'data/script/{name.lower()}.txt':
            # таблица CT: своя у каждого компилятора; строгий смысл уже
            # проверен CT-разрешением scr
            log(f'  {rel}: пропуск (таблица CT, сверяется через scr)')
        else:
            if a.read_bytes() == b.read_bytes():
                log(f'  {rel}: MATCH')
            else:
                log(f'  {rel}: DIFF (байты)')
                ok = False
    log(f'== rebuild: {"MATCH — пересборка воспроизводит оригинал" if ok else "DIFF"} ==')
    return 0 if ok else 1
