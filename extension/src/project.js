'use strict';
/**
 * Какой мод считаем «текущим». Проект мода — папка с srmod.json; в одном окне
 * их может лежать несколько (папка с модами), поэтому: сначала смотрим на
 * открытый файл (мод определяется однозначно), потом на прошлый выбор, и лишь
 * потом спрашиваем.
 */
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const STATE_KEY = 'srmod.currentProject';

function findUpward(startFsPath) {
    let dir = startFsPath;
    try {
        if (fs.statSync(dir).isFile()) {
            dir = path.dirname(dir);
        }
    } catch (e) {
        return null;
    }
    for (;;) {
        if (fs.existsSync(path.join(dir, 'srmod.json'))) {
            return dir;
        }
        const up = path.dirname(dir);
        if (up === dir) {
            return null;
        }
        dir = up;
    }
}

async function discover() {
    const found = await vscode.workspace.findFiles(
        '**/srmod.json', '**/{node_modules,build,.git,.srmod}/**', 50);
    const dirs = found.map((uri) => path.dirname(uri.fsPath));
    return Array.from(new Set(dirs)).sort();
}

function remembered(context) {
    const saved = context.workspaceState.get(STATE_KEY);
    if (saved && fs.existsSync(path.join(saved, 'srmod.json'))) {
        return saved;
    }
    return null;
}

function remember(context, dir) {
    return context.workspaceState.update(STATE_KEY, dir);
}

function label(dir) {
    try {
        const raw = fs.readFileSync(path.join(dir, 'srmod.json'), 'utf8').replace(/^﻿/, '');
        const name = JSON.parse(raw).name;
        if (name) {
            return name;
        }
    } catch (e) {
        // srmod.json может быть недописан прямо сейчас — не повод падать
    }
    return path.basename(dir);
}

async function pick(context, dirs) {
    const items = dirs.map((dir) => ({
        label: label(dir),
        description: vscode.workspace.asRelativePath(dir),
        dir,
    }));
    const chosen = await vscode.window.showQuickPick(items, {
        title: 'С каким модом работаем?',
        placeHolder: 'Папка с srmod.json',
    });
    if (!chosen) {
        return null;
    }
    await remember(context, chosen.dir);
    return chosen.dir;
}

/**
 * Текущий мод. options.ask=false — не спрашивать (для фоновых действий вроде
 * проверки при сохранении).
 */
async function current(context, options) {
    const ask = !options || options.ask !== false;
    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document.uri.scheme === 'file') {
        const fromFile = findUpward(editor.document.uri.fsPath);
        if (fromFile) {
            return fromFile;
        }
    }
    const saved = remembered(context);
    if (saved) {
        return saved;
    }
    const dirs = await discover();
    if (dirs.length === 1) {
        await remember(context, dirs[0]);
        return dirs[0];
    }
    if (!dirs.length || !ask) {
        return null;
    }
    return pick(context, dirs);
}

module.exports = { current, discover, pick, remember, label, findUpward };
