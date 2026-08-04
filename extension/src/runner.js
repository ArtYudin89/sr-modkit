'use strict';
/**
 * Запуск CLI `srmod` из расширения.
 *
 * Расширение НЕ везёт с собой Python-пакет: он живёт в репозитории sr-modkit и
 * ставится `pip install -e`. Поэтому чем запускать — вопрос трёх вариантов,
 * которые пробуются по очереди и кэшируются до смены настроек:
 *   1. srmod.command из настроек (явное слово пользователя — без проб);
 *   2. `srmod` из PATH (после `pip install -e`);
 *   3. `<python> -m srmod` с рабочим каталогом репозитория sr-modkit.
 *
 * Кодировка: srmod печатает по-русски, а Python на Windows при перенаправлении
 * вывода в канал берёт кодировку локали (cp1251) — в панели вывода это каша.
 * Лечится PYTHONIOENCODING. PYTHONUTF8 здесь ставить НЕЛЬЗЯ: он меняет
 * кодировку по умолчанию для файловых операций, а srmod работает с cp1251- и
 * UTF-16-файлами игры, где менять умолчание опасно.
 */
const cp = require('child_process');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let cached = null;

function invalidate() {
    cached = null;
}

/** Папка репозитория sr-modkit (та, где лежит пакет srmod/). */
function repoRoot(context) {
    const configured = vscode.workspace.getConfiguration('srmod').get('repoPath');
    if (configured && fs.existsSync(path.join(configured, 'srmod', '__init__.py'))) {
        return configured;
    }
    // Расширение запущено из исходников репозитория: <repo>/extension/src/…
    const fromSource = path.resolve(context.extensionPath, '..');
    if (fs.existsSync(path.join(fromSource, 'srmod', '__init__.py'))) {
        return fromSource;
    }
    return null;
}

function candidates(context) {
    const cfg = vscode.workspace.getConfiguration('srmod');
    const explicit = cfg.get('command') || [];
    if (explicit.length) {
        return [{ cmd: explicit[0], args: explicit.slice(1), cwd: null, how: 'настройка srmod.command' }];
    }
    const list = [{ cmd: 'srmod', args: [], cwd: null, how: 'srmod из PATH' }];
    const repo = repoRoot(context);
    if (repo) {
        list.push({
            cmd: cfg.get('pythonPath') || 'python',
            args: ['-m', 'srmod'],
            cwd: repo,
            how: `python -m srmod из ${repo}`,
        });
    }
    return list;
}

function env() {
    const cfg = vscode.workspace.getConfiguration('srmod');
    const out = Object.assign({}, process.env, {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
    });
    for (const key of ['rsmc', 'rscript', 'blockpar', 'game', 'decompiler']) {
        const value = cfg.get(`tools.${key}`);
        if (value) {
            out[`SRMOD_${key.toUpperCase()}`] = value;
        }
    }
    return out;
}

function probe(candidate) {
    return new Promise((resolve) => {
        let child;
        try {
            child = cp.spawn(candidate.cmd, candidate.args.concat(['--help']), {
                cwd: candidate.cwd || undefined,
                env: env(),
                windowsHide: true,
            });
        } catch (e) {
            resolve(false);
            return;
        }
        let failed = false;
        child.on('error', () => { failed = true; resolve(false); });
        child.on('close', (code) => { if (!failed) resolve(code === 0); });
        // Живой srmod --help отвечает мгновенно; ждать дольше нечего.
        setTimeout(() => { try { child.kill(); } catch (e) { /* уже умер */ } }, 15000);
    });
}

/** Найти рабочий способ запуска. null — не нашли ни одного. */
async function resolve(context, log) {
    if (cached) {
        return cached;
    }
    for (const candidate of candidates(context)) {
        if (await probe(candidate)) {
            cached = candidate;
            if (log) {
                log(`srmod: запускаю как «${candidate.how}»`);
            }
            return candidate;
        }
    }
    return null;
}

/**
 * Запустить подкоманду. onLine получает каждую строку вывода.
 * Возвращает {code, lines}.
 */
function spawn(runner, args, options, onLine) {
    return new Promise((resolve, reject) => {
        const child = cp.spawn(runner.cmd, runner.args.concat(args), {
            cwd: (options && options.cwd) || runner.cwd || undefined,
            env: env(),
            windowsHide: true,
        });
        const lines = [];
        let rest = { out: '', err: '' };

        const feed = (key) => (chunk) => {
            rest[key] += chunk.toString('utf8');
            const parts = rest[key].split(/\r?\n/);
            rest[key] = parts.pop();
            for (const line of parts) {
                lines.push(line);
                if (onLine) {
                    onLine(line);
                }
            }
        };
        child.stdout.on('data', feed('out'));
        child.stderr.on('data', feed('err'));

        child.on('error', reject);
        child.on('close', (code) => {
            for (const key of ['out', 'err']) {
                if (rest[key]) {
                    lines.push(rest[key]);
                    if (onLine) {
                        onLine(rest[key]);
                    }
                }
            }
            resolve({ code, lines });
        });
        if (options && options.register) {
            options.register(child);
        }
    });
}

module.exports = { resolve, spawn, env, repoRoot, invalidate };
