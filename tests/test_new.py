"""`srmod new`: путь установки по умолчанию — Mods/<группа>/<имя>, группа MyMods. Тест мода в
sr-lab ставит мод только как Группа\\Мод, а прежнее умолчание Mods/<имя> он не принимал
(аудит 21.10.2026, F1021-link-modkit-srlab-1).

    python -m unittest discover -s tests
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from srmod import cli, new  # noqa: E402
from srmod.build import BuildError  # noqa: E402


def install_of(project):
    return json.loads((Path(project) / 'srmod.json').read_text(encoding='utf-8'))['install']


class NewInstall(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, True)

    def test_default_has_a_group(self):
        new.scaffold(self.temp / 'X', 'X')
        self.assertEqual(install_of(self.temp / 'X'), 'Mods/MyMods/X')

    def test_group_or_whole_path(self):
        new.scaffold(self.temp / 'A', 'X', group='Tweaks', minimal=True)
        self.assertEqual(install_of(self.temp / 'A'), 'Mods/Tweaks/X')
        new.scaffold(self.temp / 'B', 'X', install='Mods/A/X', minimal=True)
        self.assertEqual(install_of(self.temp / 'B'), 'Mods/A/X')

    def test_group_is_a_plain_folder_name(self):
        for bad in ('A/B', 'A\\B', '..', '', 'Мои моды'):
            with self.subTest(group=bad), self.assertRaises(BuildError):
                new.scaffold(self.temp / 'bad', 'X', group=bad, minimal=True)
        self.assertFalse((self.temp / 'bad').exists())  # отказ — до первой записи на диск

    def test_cli(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['new', str(self.temp / 'C'), '--name', 'X', '--minimal']), 0)
            self.assertEqual(cli.main(['new', str(self.temp / 'D'), '--name', 'X', '--minimal',
                                       '--group', 'G']), 0)
        self.assertEqual(install_of(self.temp / 'C'), 'Mods/MyMods/X')
        self.assertEqual(install_of(self.temp / 'D'), 'Mods/G/X')
        # группа или путь целиком — не оба сразу
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['new', str(self.temp / 'E'), '--name', 'X', '--group', 'G',
                      '--install', 'Mods/A/X'])
        self.assertFalse((self.temp / 'E').exists())

    def test_help_tells_the_default(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['new', '--help'])
        text = ' '.join(out.getvalue().split())
        self.assertIn('Mods/<group>/<name>', text)
        self.assertIn('MyMods', text)
        self.assertNotIn('Artem', text)  # справка обещала Mods/Artem/<name>, а код давал Mods/<name>


if __name__ == '__main__':
    unittest.main()
