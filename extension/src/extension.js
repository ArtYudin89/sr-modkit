'use strict';
/**
 * Расширение VS Code для sr-modkit: подсветка .rsm (грамматика собирается
 * tools/build_grammar.py) плюс кнопки поверх CLI `srmod`.
 *
 * Логика сборки НЕ дублируется на JS: всё, что делает расширение, — запускает
 * те же команды, что модер набрал бы руками, и показывает их вывод. Замечания
 * `srmod lint` переносятся в панель «Проблемы» разбором его же строк
 * `путь:строка: текст` — тот же формат, что у problemMatcher в задачах.
 */
const cp = require('child_process');
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const runner = require('./runner');
const project = require('./project');
const dsl = require('./dsl');
const lexicon = require('./lexicon');
const definition = require('./definition');

let output;
let diagnostics;
let statusItem;
let watchChild = null;

function log(line) {
    output.appendLine(line);
}

function showOutput() {
    output.show(true);
}

async function needRunner(context) {
    const found = await runner.resolve(context, log);
    if (found) {
        return found;
    }
    const choice = await vscode.window.showErrorMessage(
        'Не нашлось, чем запускать srmod. Установите его (`pip install -e <папка sr-modkit>`) '
        + 'или укажите путь в настройках.',
        'Открыть настройки', 'Показать вывод');
    if (choice === 'Открыть настройки') {
        vscode.commands.executeCommand('workbench.action.openSettings', 'srmod');
    } else if (choice === 'Показать вывод') {
        showOutput();
    }
    return null;
}

async function needProject(context) {
    const dir = await project.current(context);
    if (dir) {
        return dir;
    }
    const choice = await vscode.window.showErrorMessage(
        'Не найден мод: в открытых папках нет ни одного srmod.json.',
        'Создать мод…');
    if (choice === 'Создать мод…') {
        vscode.commands.executeCommand('srmod.new');
    }
    return null;
}

/** Запустить подкоманду srmod с индикатором прогресса. Возвращает {code, lines}. */
async function run(context, args, title, options) {
    const found = await needRunner(context);
    if (!found) {
        return null;
    }
    log(`\n$ srmod ${args.join(' ')}`);
    return vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `srmod: ${title}` },
        () => runner.spawn(found, args, options || {}, log));
}

function reportResult(result, okMessage) {
    if (!result) {
        return;
    }
    if (result.code === 0) {
        vscode.window.setStatusBarMessage(`srmod: ${okMessage}`, 5000);
        return;
    }
    const tail = result.lines.filter((l) => l.trim()).slice(-1)[0] || `код возврата ${result.code}`;
    vscode.window.showErrorMessage(`srmod: ${tail}`, 'Показать вывод').then((choice) => {
        if (choice === 'Показать вывод') {
            showOutput();
        }
    });
}

// --------------------------------------------------------------------- lint

// `путь:строка: важность: текст` — важность появилась вместе со сверкой по
// rsm-dsl.json и необязательна: строки без неё остаются предупреждениями.
const LINT_LINE = /^(.+?):(\d+):\s*(?:(error|warning):\s*)?(.*)$/;

function applyLint(projectDir, lines) {
    const byFile = new Map();
    for (const line of lines) {
        const m = LINT_LINE.exec(line);
        if (!m || line.startsWith('OK:') || line.startsWith('---')) {
            continue;
        }
        const file = path.isAbsolute(m[1]) ? m[1] : path.join(projectDir, m[1]);
        const uri = vscode.Uri.file(file);
        const lineNo = Math.max(0, parseInt(m[2], 10) - 1);
        const range = new vscode.Range(lineNo, 0, lineNo, Number.MAX_SAFE_INTEGER);
        const severity = m[3] === 'error'
            ? vscode.DiagnosticSeverity.Error
            : vscode.DiagnosticSeverity.Warning;
        const diag = new vscode.Diagnostic(range, m[4], severity);
        diag.source = 'srmod lint';
        const key = uri.toString();
        if (!byFile.has(key)) {
            byFile.set(key, { uri, items: [] });
        }
        byFile.get(key).items.push(diag);
    }
    diagnostics.clear();
    for (const { uri, items } of byFile.values()) {
        diagnostics.set(uri, items);
    }
    return byFile.size;
}

async function lint(context, options) {
    const silent = options && options.silent;
    const dir = silent ? await project.current(context, { ask: false }) : await needProject(context);
    if (!dir) {
        return;
    }
    const found = silent ? await runner.resolve(context, log) : await needRunner(context);
    if (!found) {
        return;
    }
    const result = await runner.spawn(found, ['lint', dir], {}, silent ? null : log);
    const files = applyLint(dir, result.lines);
    if (!silent) {
        const count = result.lines.filter((l) => LINT_LINE.test(l) && !l.startsWith('OK:')).length;
        vscode.window.setStatusBarMessage(
            count ? `srmod lint: замечаний — ${count} (в ${files} файле(ах))` : 'srmod lint: чисто',
            5000);
    }
}

// --------------------------------------------------------------------- watch

function killTree(child) {
    if (!child || child.killed) {
        return;
    }
    if (process.platform === 'win32') {
        // srmod watch запускает rsmc/BlockParEditor — гасить надо всё дерево,
        // иначе внуки переживут родителя и продолжат держать файлы сборки.
        try {
            cp.execFileSync('taskkill', ['/pid', String(child.pid), '/T', '/F'],
                { windowsHide: true, stdio: 'ignore' });
            return;
        } catch (e) {
            // процесс уже мог завершиться — падать тут не на чем
        }
    }
    try {
        child.kill();
    } catch (e) {
        /* уже мёртв */
    }
}

function setWatching(on) {
    vscode.commands.executeCommand('setContext', 'srmod.watching', on);
    updateStatus(on);
}

async function watchStart(context) {
    if (watchChild) {
        vscode.window.showInformationMessage('srmod watch уже работает.');
        return;
    }
    const dir = await needProject(context);
    if (!dir) {
        return;
    }
    const found = await needRunner(context);
    if (!found) {
        return;
    }
    log(`\n$ srmod watch ${dir}`);
    showOutput();
    setWatching(true);
    runner.spawn(found, ['watch', dir], {
        register: (child) => { watchChild = child; },
    }, log).then(() => {
        watchChild = null;
        setWatching(false);
        log('srmod watch: остановлен');
    }, (err) => {
        watchChild = null;
        setWatching(false);
        log(`srmod watch: ошибка запуска — ${err.message}`);
    });
}

function watchStop() {
    if (!watchChild) {
        vscode.window.showInformationMessage('srmod watch не запущен.');
        return;
    }
    killTree(watchChild);
}

// --------------------------------------------------------------------- new/open

async function newMod(context) {
    const name = await vscode.window.showInputBox({
        title: 'Имя нового мода',
        prompt: 'Латиницей, без пробелов — оно же scriptName и имя .scr',
        validateInput: (value) => (/^[A-Za-z][A-Za-z0-9_]*$/.test(value || '')
            ? null : 'Разрешены буквы, цифры и _, первая — буква'),
    });
    if (!name) {
        return;
    }
    const parent = await vscode.window.showOpenDialog({
        title: 'Где создать папку мода',
        canSelectFolders: true,
        canSelectFiles: false,
        openLabel: 'Создать здесь',
    });
    if (!parent || !parent.length) {
        return;
    }
    const dest = path.join(parent[0].fsPath, name);
    const result = await run(context, ['new', dest, '--name', name], `создаю ${name}`);
    if (result && result.code === 0) {
        await project.remember(context, dest);
        const choice = await vscode.window.showInformationMessage(
            `Мод ${name} создан.`, 'Открыть папку', 'Показать вывод');
        if (choice === 'Открыть папку') {
            vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(dest), true);
        } else if (choice === 'Показать вывод') {
            showOutput();
        }
    } else {
        reportResult(result, '');
    }
}

async function openMod(context) {
    const source = await vscode.window.showOpenDialog({
        title: 'Папка готового мода (как в Mods\\ игры)',
        canSelectFolders: true,
        canSelectFiles: false,
        openLabel: 'Разобрать этот мод',
    });
    if (!source || !source.length) {
        return;
    }
    const parent = await vscode.window.showOpenDialog({
        title: 'Где создать проект с исходниками',
        canSelectFolders: true,
        canSelectFiles: false,
        openLabel: 'Создать здесь',
    });
    if (!parent || !parent.length) {
        return;
    }
    const dest = path.join(parent[0].fsPath, path.basename(source[0].fsPath));
    showOutput();
    const result = await run(context, ['open', source[0].fsPath, dest],
        `разбираю ${path.basename(source[0].fsPath)}`);
    if (result && result.code === 0) {
        await project.remember(context, dest);
        const choice = await vscode.window.showInformationMessage(
            'Мод разобран в исходники.', 'Открыть папку');
        if (choice === 'Открыть папку') {
            vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(dest), true);
        }
    } else {
        reportResult(result, '');
    }
}

// --------------------------------------------------------------------- тест в sr-lab

/**
 * Тест мода идёт через sr-lab — отдельный инструмент (своя сборка движка, новая игра
 * и ходы без окна, проверки скриптов). Расширение лишь зовёт его
 * `python <sr-lab>/sr.py test local <мод>/build --as Группа\Мод`, если путь к sr-lab
 * указан в настройке srmod.tools.srlab; без неё команда недоступна.
 */
function modId(dir) {
    try {
        const raw = fs.readFileSync(path.join(dir, 'srmod.json'), 'utf8').replace(/^\uFEFF/, '');
        const parts = String(JSON.parse(raw).install || '').split(/[\\/]+/).filter(Boolean);
        if (parts.length && parts[0].toLowerCase() === 'mods') {
            parts.shift();
        }
        // игра знает моды только как Группа\Мод; "Mods/Мод" без группы — имени не узнать
        if (parts.length >= 2) {
            return `${parts[0]}\\${parts[1]}`;
        }
    } catch (e) {
        // srmod.json может быть недописан прямо сейчас — скажем ниже
    }
    return null;
}

/** Как запустить тест мода или null, если sr-lab не настроен. */
function srlabTest(dir) {
    const cfg = vscode.workspace.getConfiguration('srmod');
    const srlab = cfg.get('tools.srlab');
    if (!srlab || !fs.existsSync(path.join(srlab, 'sr.py'))) {
        return null;
    }
    const id = modId(dir);
    const args = [path.join(srlab, 'sr.py'), 'test', 'local', path.join(dir, 'build')];
    if (id) {
        args.push('--as', id);
    }
    args.push('--turns', String(cfg.get('test.turns') ?? 15));
    const where = cfg.get('test.where');
    if (where) {
        args.push('--where', where);
    }
    args.push('--deps', cfg.get('test.deps') || 'steam');
    return { cmd: cfg.get('pythonPath') || 'python', args, cwd: srlab, id };
}

async function testMod(context) {
    const dir = await needProject(context);
    if (!dir) {
        return;
    }
    const test = srlabTest(dir);
    if (!test) {
        const choice = await vscode.window.showWarningMessage(
            'Тест мода идёт через sr-lab: укажите его папку (в ней sr.py) в настройке '
            + 'srmod.tools.srlab.', 'Открыть настройки');
        if (choice) {
            vscode.commands.executeCommand('workbench.action.openSettings', 'srmod.tools.srlab');
        }
        return;
    }
    if (!test.id) {
        vscode.window.showErrorMessage(
            'В srmod.json поле install должно быть Mods\\Группа\\Мод — без группы не знаю, '
            + 'как мод зовут в игре.');
        return;
    }
    if (!fs.existsSync(path.join(dir, 'build', 'ModuleInfo.txt'))) {
        vscode.window.showErrorMessage('Сначала соберите мод: в build/ нет ModuleInfo.txt.');
        return;
    }
    showOutput();
    log(`\n$ python ${test.args.join(' ')}`);
    const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Window, title: `srmod: тест ${test.id} в sr-lab` },
        () => runner.spawn({ cmd: test.cmd, args: [], cwd: test.cwd }, test.args, {}, log));
    reportResult(result, `тест ${test.id} пройден`);
}

// --------------------------------------------------------------------- задачи

function taskProvider(context) {
    return {
        async provideTasks() {
            const found = await runner.resolve(context, log);
            const dir = await project.current(context, { ask: false });
            if (!found || !dir) {
                return [];
            }
            const specs = [
                { task: 'build', title: 'собрать' },
                { task: 'deploy', title: 'поставить в игру' },
                { task: 'lint', title: 'проверить скрипты' },
                { task: 'verify', title: 'гейт доверия' },
                { task: 'doctor', title: 'проверить инструменты' },
            ];
            const tasks = specs.map((spec) => {
                const task = new vscode.Task(
                    { type: 'srmod', task: spec.task },
                    vscode.TaskScope.Workspace,
                    `${spec.task} (${project.label(dir)})`,
                    'srmod',
                    new vscode.ProcessExecution(found.cmd, found.args.concat([spec.task, dir]), {
                        cwd: found.cwd || dir,
                        env: runner.env(),
                    }),
                    spec.task === 'lint' ? '$srmod-lint' : undefined);
                task.detail = `srmod ${spec.task} — ${spec.title}`;
                if (spec.task === 'build') {
                    task.group = vscode.TaskGroup.Build;
                }
                return task;
            });
            const test = srlabTest(dir);
            if (test && test.id) {
                const task = new vscode.Task(
                    { type: 'srmod', task: 'test' },
                    vscode.TaskScope.Workspace,
                    `test (${project.label(dir)})`,
                    'srmod',
                    new vscode.ProcessExecution(test.cmd, test.args, { cwd: test.cwd, env: runner.env() }));
                task.detail = 'sr-lab — новая игра с модом и ходы своей сборкой, вердикт';
                task.group = vscode.TaskGroup.Test;
                tasks.push(task);
            }
            return tasks;
        },
        resolveTask(task) {
            return task;
        },
    };
}

// --------------------------------------------------------------------- статус

function updateStatus(watching) {
    if (!statusItem) {
        return;
    }
    if (!vscode.workspace.getConfiguration('srmod').get('statusBar')) {
        statusItem.hide();
        return;
    }
    statusItem.text = watching ? '$(sync~spin) srmod: watch' : '$(tools) srmod';
    statusItem.tooltip = watching
        ? 'srmod watch работает — нажмите, чтобы остановить'
        : 'Собрать мод (srmod build)';
    statusItem.command = watching ? 'srmod.watchStop' : 'srmod.build';
    statusItem.show();
}

// --------------------------------------------------------------------- вход

function activate(context) {
    output = vscode.window.createOutputChannel('srmod');
    diagnostics = vscode.languages.createDiagnosticCollection('srmod');
    statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    context.subscriptions.push(output, diagnostics, statusItem);
    updateStatus(false);

    const command = (id, handler) => context.subscriptions.push(
        vscode.commands.registerCommand(id, handler));

    command('srmod.build', async () => {
        const dir = await needProject(context);
        if (dir) {
            reportResult(await run(context, ['build', dir], 'сборка'), 'сборка завершена');
            await lint(context, { silent: true });
        }
    });
    command('srmod.buildAndDeploy', async () => {
        const dir = await needProject(context);
        if (dir) {
            reportResult(await run(context, ['build', dir, '--deploy'], 'сборка и установка'),
                'мод собран и поставлен в игру');
        }
    });
    command('srmod.deploy', async () => {
        const dir = await needProject(context);
        if (dir) {
            reportResult(await run(context, ['deploy', dir], 'установка'), 'мод поставлен в игру');
        }
    });
    command('srmod.lint', () => lint(context));
    command('srmod.doctor', async () => {
        showOutput();
        await run(context, ['doctor'], 'проверка инструментов');
    });
    command('srmod.verify', async () => {
        const dir = await needProject(context);
        if (dir) {
            showOutput();
            reportResult(await run(context, ['verify', dir, '--gate'], 'гейт доверия'),
                'обе ветки сборки совпали');
        }
    });
    command('srmod.test', () => testMod(context));
    command('srmod.new', () => newMod(context));
    command('srmod.open', () => openMod(context));
    command('srmod.watchStart', () => watchStart(context));
    command('srmod.watchStop', () => watchStop());
    command('srmod.showOutput', () => showOutput());
    command('srmod.selectProject', async () => {
        const dirs = await project.discover();
        if (!dirs.length) {
            vscode.window.showWarningMessage('В открытых папках нет ни одного srmod.json.');
            return;
        }
        await project.pick(context, dirs);
    });

    context.subscriptions.push(
        vscode.languages.registerCompletionItemProvider(
            'rangers-script', dsl.completionProvider(context), '.', ':', '"', "'", '{', ','),
        vscode.languages.registerHoverProvider('rangers-script', dsl.hoverProvider(context)),
        // Встроенные функции движка (977 имён) — отдельным провайдером: dsl.js
        // отвечает за декларации мода, lexicon.js — за то, что зовут в коде.
        vscode.languages.registerCompletionItemProvider(
            'rangers-script', lexicon.completionProvider(context)),
        vscode.languages.registerHoverProvider('rangers-script', lexicon.hoverProvider(context)),
        vscode.languages.registerDefinitionProvider(
            'rangers-script', definition.definitionProvider(context)),
        vscode.tasks.registerTaskProvider('srmod', taskProvider(context)));

    context.subscriptions.push(vscode.workspace.onDidSaveTextDocument(async (doc) => {
        if (doc.languageId !== 'rangers-script') {
            return;
        }
        const cfg = vscode.workspace.getConfiguration('srmod');
        if (cfg.get('buildOnSave')) {
            const dir = await project.current(context, { ask: false });
            if (dir) {
                reportResult(await run(context, ['build', dir], 'сборка'), 'сборка завершена');
            }
        }
        if (cfg.get('lintOnSave')) {
            await lint(context, { silent: true });
        }
    }));

    context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
        if (e.affectsConfiguration('srmod')) {
            runner.invalidate();
            updateStatus(Boolean(watchChild));
        }
    }));

    // Первая проверка при открытии проекта — чтобы «Проблемы» не были пустыми
    // до первого сохранения.
    if (vscode.workspace.getConfiguration('srmod').get('lintOnSave')) {
        lint(context, { silent: true });
    }
}

function deactivate() {
    killTree(watchChild);
}

module.exports = { activate, deactivate };
