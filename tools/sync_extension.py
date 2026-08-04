# -*- coding: utf-8 -*-
"""Разложить по расширению VS Code то, что генерится в корне репозитория.

Расширение упаковывается (`vsce package`) только из своей папки — файлы снаружи
в .vsix не попадают, поэтому грамматика и схема языка живут в двух местах:
канон — `syntaxes/` и `data/` в корне, копия — внутри `extension/`. Копию
делает этот скрипт, чтобы её нельзя было забыть обновить руками.

    python tools/sync_extension.py            # только скопировать
    python tools/sync_extension.py --regen     # сначала перегенерировать канон
"""
import argparse
import io
import os
import shutil
import struct
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, 'extension')

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

COPIES = [
    (os.path.join('syntaxes', 'rangers-script.tmLanguage.json'),
     os.path.join('syntaxes', 'rangers-script.tmLanguage.json')),
    (os.path.join('data', 'rsm-dsl.json'),
     os.path.join('data', 'rsm-dsl.json')),
    ('LICENSE', 'LICENSE'),
]


def regen():
    for script in ('build_grammar.py', 'build_dsl_schema.py'):
        print('== %s' % script)
        rc = subprocess.call([sys.executable, os.path.join(HERE, script)], cwd=ROOT)
        if rc != 0:
            raise SystemExit('%s вернул %d' % (script, rc))


def copy_all():
    for src_rel, dst_rel in COPIES:
        src = os.path.join(ROOT, src_rel)
        dst = os.path.join(EXT, dst_rel)
        if not os.path.exists(src):
            raise SystemExit('нет исходника %s — сначала --regen' % src)
        dst_dir = os.path.dirname(dst)
        if dst_dir and not os.path.isdir(dst_dir):
            os.makedirs(dst_dir)
        shutil.copyfile(src, dst)
        print('  %s -> extension/%s (%d Б)'
              % (src_rel, dst_rel.replace(os.sep, '/'), os.path.getsize(dst)))


def _write_png(path, width, height, pixel_fn):
    """Тот же минимальный писатель PNG, что в srmod/new.py: Pillow в
    зависимостях нет и заводить его ради одной иконки незачем."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel_fn(x, y))

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    with io.open(path, 'wb') as fh:
        fh.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
                 + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))


def make_icon(force=False):
    path = os.path.join(EXT, 'media', 'icon.png')
    if os.path.exists(path) and not force:
        print('  иконка на месте: extension/media/icon.png')
        return
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    size = 128

    def pixel(x, y):
        cx = cy = size / 2.0
        dx, dy = x - cx + 8, y - cy + 8
        d2 = dx * dx + dy * dy
        planet_r = 40.0
        if d2 <= planet_r * planet_r:                       # планета
            shade = 1.0 - (d2 / (planet_r * planet_r)) * 0.5
            return (int(70 * shade), int(140 * shade), int(220 * shade), 255)
        mx, my = x - (size - 34), y - 34                    # спутник
        if mx * mx + my * my <= 12 * 12:
            return (235, 180, 90, 255)
        return (16, 20, 30, 255)                            # фон-космос

    _write_png(path, size, size, pixel)
    print('  нарисована иконка: extension/media/icon.png (%d Б)' % os.path.getsize(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--regen', action='store_true',
                        help='перегенерировать грамматику и схему языка перед копированием')
    parser.add_argument('--icon', action='store_true', help='перерисовать иконку')
    args = parser.parse_args()

    if args.regen:
        regen()
    print('== копирование в extension/')
    copy_all()
    make_icon(force=args.icon)


if __name__ == '__main__':
    main()
