'use strict';
/**
 * Переход к определению внутри мода (F12).
 *
 * Два случая, оба вытекают из устройства .rsm:
 * 1. `import from './states.rsm'` — путь относительно ФАЙЛА, где написан
 *    import (имя точки входа компилятору безразлично), открываем файл.
 * 2. Любой строковый литерал, совпавший с именем декларации: `state: "StX"`,
 *    `ChangeState("StX")`, `planet: "MainPlanet"`, `next: "dMsg2"`. Связи в
 *    этом языке именные, поэтому одно правило закрывает их все — отдельная
 *    таблица «какое поле на что ссылается» не нужна и не выдумывается.
 *
 * Индекс строится по всем .rsm рядом с файлом (каталог модуля `<Name>.src/`)
 * — модуль импортирует только своих соседей.
 */
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const dsl = require('./dsl');

const IMPORT_RE = /\bimport\b[^;\n]*?from\s*["']([^"']+)["']/;

/** Позиции деклараций в тексте: name -> {line, column, decl}. */
function declarationsIn(text, declNames) {
    const masked = dsl.mask(text);
    const out = [];
    const re = /\b([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*["']/g;
    let m;
    while ((m = re.exec(masked))) {
        if (!declNames.has(m[1])) {
            continue;
        }
        // Имя лежит в оригинале: маска гасит содержимое строк, но не кавычки.
        const quote = masked[m.index + m[0].length - 1];
        const end = text.indexOf(quote, m.index + m[0].length);
        if (end === -1) {
            continue;
        }
        const name = text.slice(m.index + m[0].length, end);
        const before = text.slice(0, m.index);
        const line = before.split('\n').length - 1;
        out.push({ name, decl: m[1], line, column: m.index - (before.lastIndexOf('\n') + 1) });
    }
    return out;
}

function siblingFiles(file) {
    try {
        return fs.readdirSync(path.dirname(file))
            .filter((f) => f.toLowerCase().endsWith('.rsm'))
            .map((f) => path.join(path.dirname(file), f));
    } catch (e) {
        return [file];
    }
}

function definitionProvider(context) {
    return {
        provideDefinition(document, position) {
            const lineText = document.lineAt(position).text;
            const declNames = new Set(Object.keys(dsl.load(context).declarations || {}));

            const imp = IMPORT_RE.exec(lineText);
            if (imp && lineText.indexOf(imp[1]) <= position.character
                && position.character <= lineText.indexOf(imp[1]) + imp[1].length) {
                const target = path.resolve(path.dirname(document.uri.fsPath), imp[1]);
                if (fs.existsSync(target)) {
                    return new vscode.Location(vscode.Uri.file(target), new vscode.Position(0, 0));
                }
                return null;
            }

            // Слово под курсором внутри строкового литерала.
            const range = document.getWordRangeAtPosition(position, /"[^"]*"|'[^']*'/);
            if (!range) {
                return null;
            }
            const raw = document.getText(range);
            const wanted = raw.slice(1, -1);
            if (!wanted) {
                return null;
            }

            const targets = [];
            for (const file of siblingFiles(document.uri.fsPath)) {
                let text;
                try {
                    text = file === document.uri.fsPath
                        ? document.getText() : fs.readFileSync(file, 'utf8');
                } catch (e) {
                    continue;
                }
                for (const d of declarationsIn(text, declNames)) {
                    if (d.name === wanted) {
                        targets.push(new vscode.Location(
                            vscode.Uri.file(file), new vscode.Position(d.line, d.column)));
                    }
                }
            }
            return targets.length ? targets : null;
        },
    };
}

module.exports = { definitionProvider, declarationsIn };
