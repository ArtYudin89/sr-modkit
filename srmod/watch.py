"""`srmod watch` — пересобирать при изменении исходников (docs/STAGE0.md).

Инкрементальность по хэшам исходников в `.srmod/state.json`: это ускоряет
итерацию (junction в Mods\\ даёт "поправил -> собрал -> в игре"), а не
инкрементальную сборку самого rsmc/BlockParEditor — они всё равно
пересобирают файл целиком, мы просто решаем, НАДО ЛИ вызывать их.
"""
import hashlib
import json
import time
from pathlib import Path

# Раскладка src/build сняла старую граблю «rsmc пишет артефакт рядом с
# исходником, watch ловит его как изменение и зацикливается»: артефакты
# теперь живут только в build/, а исходники — только в src/. Следим за всем
# src/ (что бы там ни лежало — это по определению исходник) плюс srmod.json.
_STATE_REL = Path('.srmod') / 'state.json'


def _iter_sources(root):
    src = root / 'src'
    if src.is_dir():
        for p in src.rglob('*'):
            if p.is_file():
                yield p
    project = root / 'srmod.json'
    if project.exists():
        yield project


def hash_sources(root):
    """dict относительный путь (posix) -> sha1 содержимого."""
    root = Path(root)
    out = {}
    for p in _iter_sources(root):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = hashlib.sha1(data).hexdigest()
    return out


def load_state(root):
    state_path = Path(root) / _STATE_REL
    try:
        return json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_state(root, hashes):
    state_path = Path(root) / _STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'hashes': hashes, 'built_at': None}, indent=2),
                          encoding='utf-8')


def changed_since(root, prev_state):
    cur = hash_sources(root)
    prev = (prev_state or {}).get('hashes', {})
    added = sorted(k for k in cur if k not in prev)
    removed = sorted(k for k in prev if k not in cur)
    modified = sorted(k for k in cur if k in prev and cur[k] != prev[k])
    return cur, (added, removed, modified)


def run_watch(cfg, build_fn, interval=1.0, once=False):
    """build_fn() -> вызывается при первом запуске и на каждое изменение;
    должен бросать buildmod.BuildError на провал (ловим и печатаем, не падаем)."""
    root = cfg.root
    state = load_state(root)
    cur, (added, removed, modified) = changed_since(root, state)
    print(f'watch: {root} ({len(cur)} исходник(ов))')

    def do_build():
        try:
            build_fn()
            save_state(root, hash_sources(root))
            print('OK: собрано')
        except Exception as e:  # noqa: BLE001 - watch не должен падать целиком
            print(f'error: {e}')

    do_build()
    if once:
        return

    try:
        while True:
            time.sleep(interval)
            new_hashes = hash_sources(root)
            state = load_state(root)
            if new_hashes != state.get('hashes', {}):
                changed = sorted(set(new_hashes) ^ set(state.get('hashes', {})))
                print(f'изменилось: {", ".join(changed) or "(содержимое)"}')
                do_build()
    except KeyboardInterrupt:
        print('watch: остановлено')
