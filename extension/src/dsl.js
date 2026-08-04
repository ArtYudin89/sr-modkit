'use strict';
/**
 * Подсказки по языку .rsm: какие поля есть у декларации и какие значения
 * допустимы. Данные — `data/rsm-dsl.json`, сгенерированный из самого rsmc
 * (tools/build_dsl_schema.py): списки полей из его таблицы строк, значения
 * перечислений — опросом живого компилятора. Ничего не захардкожено здесь.
 *
 * Поля, помеченные `validated: false`, rsmc не проверяет: значения показываем
 * как подсказку, но выдавать их за исчерпывающий список нельзя.
 */
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

let schema = null;

function load(context) {
    if (schema) {
        return schema;
    }
    const file = path.join(context.extensionPath, 'data', 'rsm-dsl.json');
    try {
        schema = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (e) {
        schema = { declarations: {}, exports: [], varTypes: [], unvalidatedFields: [] };
    }
    return schema;
}

/** Заменить содержимое строк и комментариев пробелами, сохранив длину. */
function mask(text) {
    const out = text.split('');
    let i = 0;
    while (i < out.length) {
        const ch = text[i];
        if (ch === '/' && text[i + 1] === '/') {
            while (i < out.length && text[i] !== '\n') { out[i] = ' '; i += 1; }
        } else if (ch === '/' && text[i + 1] === '*') {
            out[i] = ' '; out[i + 1] = ' '; i += 2;
            while (i < out.length && !(text[i] === '*' && text[i + 1] === '/')) {
                if (text[i] !== '\n') { out[i] = ' '; }
                i += 1;
            }
            i += 2;
        } else if (ch === '"' || ch === "'") {
            // Строки в этом языке однострочные: незакрытая гаснет на конце строки.
            i += 1;
            while (i < out.length && text[i] !== ch && text[i] !== '\n') {
                if (text[i] === '\\') { out[i] = ' '; i += 1; }
                if (i < out.length) { out[i] = ' '; i += 1; }
            }
            i += 1;
        } else {
            i += 1;
        }
    }
    return out.join('');
}

/**
 * Внутри какой декларации стоит курсор: идём назад до незакрытой `(`.
 * Незакрытые `{`/`[` по дороге пропускаем — это объект опций и массивы внутри
 * того же вызова.
 */
function enclosingDecl(masked, offset) {
    let depth = 0;
    const limit = Math.max(0, offset - 20000);
    for (let i = offset - 1; i >= limit; i -= 1) {
        const ch = masked[i];
        if (ch === ')' || ch === ']' || ch === '}') {
            depth += 1;
        } else if (ch === '(') {
            if (depth === 0) {
                const before = masked.slice(Math.max(0, i - 64), i);
                const m = /([A-Za-z_][A-Za-z0-9_]*)\s*$/.exec(before);
                return m ? m[1] : null;
            }
            depth -= 1;
        } else if (ch === '[' || ch === '{') {
            if (depth > 0) {
                depth -= 1;
            }
        } else if (ch === ';' && depth === 0) {
            return null;                     // предыдущая инструкция закончилась
        }
    }
    return null;
}

/**
 * Открыта ли строковая кавычка к концу строки текста. Считаем парность, а не
 * «есть ли кавычка левее»: в `planet("P", {` кавычки закрыты, и подсказывать
 * там надо поля, а не значения.
 */
function openQuote(line) {
    let quote = null;
    for (let i = 0; i < line.length; i += 1) {
        const ch = line[i];
        if (quote) {
            if (ch === '\\') {
                i += 1;
            } else if (ch === quote) {
                quote = null;
            }
        } else if (ch === '"' || ch === "'") {
            quote = ch;
        } else if (ch === '/' && line[i + 1] === '/') {
            return null;                      // дальше комментарий до конца строки
        }
    }
    return quote;
}

function enumFor(context, declName, field) {
    const decl = load(context).declarations[declName];
    if (!decl || !decl.enums) {
        return null;
    }
    return decl.enums[field] || null;
}

function fieldDoc(context, declName, field) {
    const decl = load(context).declarations[declName];
    if (!decl) {
        return null;
    }
    const lines = [];
    if ((decl.required || []).includes(field)) {
        lines.push('**обязательное поле**');
    }
    const en = enumFor(context, declName, field);
    if (en) {
        lines.push(en.validated
            ? 'Допустимые значения (проверяет rsmc):'
            : 'Встречающиеся значения (rsmc это поле НЕ проверяет — опечатка соберётся молча):');
        lines.push(en.values.map((v) => `\`${v}\``).join(', '));
    }
    if (decl.entryShapes && decl.entryShapes[field]) {
        lines.push(`Массив объектов с ключами: ${decl.entryShapes[field]
            .map((k) => `\`${k}\``).join(', ')}`);
    }
    if ((decl.codeFields || []).includes(field)) {
        lines.push('Значение — `function() { … }` без параметров.');
    }
    return lines.length ? lines.join('\n\n') : null;
}

function declDoc(context, declName) {
    const decl = load(context).declarations[declName];
    if (!decl) {
        return null;
    }
    const parts = [];
    const args = (decl.positional || []).concat(decl.fields.length ? ['{…}'] : []);
    parts.push(`\`${declName}(${args.join(', ')})\``);
    if (decl.required && decl.required.length) {
        parts.push(`Обязательные поля: ${decl.required.map((f) => `\`${f}\``).join(', ')}`);
    }
    if (decl.fields.length) {
        parts.push(`Поля: ${decl.fields.map((f) => `\`${f}\``).join(', ')}`);
    }
    return parts.join('\n\n');
}

function completionProvider(context) {
    return {
        provideCompletionItems(document, position) {
            const data = load(context);
            const line = document.lineAt(position).text.slice(0, position.character);
            const text = document.getText(new vscode.Range(new vscode.Position(0, 0), position));
            const masked = mask(text);
            const declName = enclosingDecl(masked, masked.length);

            // 1. Значение перечисления: курсор внутри строкового литерала,
            //    ключ поля стоит слева (в т.ч. внутри массива `owner: ["…`).
            const inString = openQuote(line) !== null;
            if (inString && declName) {
                const key = /([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:\[[^\]]*)?["'][^"']*$/.exec(line);
                if (key) {
                    const en = enumFor(context, declName, key[1]);
                    if (en) {
                        return en.values.map((value) => {
                            const item = new vscode.CompletionItem(
                                value, vscode.CompletionItemKind.EnumMember);
                            item.detail = en.validated
                                ? `${declName}.${key[1]}`
                                : `${declName}.${key[1]} (rsmc не проверяет)`;
                            return item;
                        });
                    }
                }
                return [];
            }
            if (inString) {
                return [];
            }

            // 2. Имя поля внутри объекта опций.
            if (declName && /[{,]\s*[A-Za-z0-9_]*$/.test(line)) {
                const decl = data.declarations[declName];
                if (decl) {
                    return decl.fields.map((field) => {
                        const item = new vscode.CompletionItem(
                            field, vscode.CompletionItemKind.Property);
                        item.insertText = new vscode.SnippetString(
                            (decl.codeFields || []).includes(field)
                                ? `${field}: function() {\n    $0\n}`
                                : `${field}: $0`);
                        if ((decl.required || []).includes(field)) {
                            item.detail = 'обязательное';
                            item.sortText = `0${field}`;
                        }
                        const doc = fieldDoc(context, declName, field);
                        if (doc) {
                            item.documentation = new vscode.MarkdownString(doc);
                        }
                        return item;
                    });
                }
                return [];
            }

            // 3. Имя декларации в начале инструкции.
            if (/(?:^|[;{}])\s*[A-Za-z0-9_]*$/.test(line)) {
                return Object.keys(data.declarations).map((name) => {
                    const item = new vscode.CompletionItem(
                        name, vscode.CompletionItemKind.Function);
                    item.documentation = new vscode.MarkdownString(declDoc(context, name));
                    return item;
                });
            }
            return [];
        },
    };
}

function hoverProvider(context) {
    return {
        provideHover(document, position) {
            const range = document.getWordRangeAtPosition(position, /[A-Za-z_][A-Za-z0-9_]*/);
            if (!range) {
                return null;
            }
            const word = document.getText(range);
            const data = load(context);

            const after = document.lineAt(position).text.slice(range.end.character);
            if (data.declarations[word] && /^\s*\(/.test(after)) {
                return new vscode.Hover(
                    new vscode.MarkdownString(declDoc(context, word)), range);
            }
            const head = document.getText(new vscode.Range(new vscode.Position(0, 0), range.start));
            const declName = enclosingDecl(mask(head), head.length);
            if (declName) {
                const doc = fieldDoc(context, declName, word);
                if (doc) {
                    const md = new vscode.MarkdownString(`\`${declName}.${word}\`\n\n${doc}`);
                    return new vscode.Hover(md, range);
                }
            }
            return null;
        },
    };
}

module.exports = { load, completionProvider, hoverProvider, mask, enclosingDecl };
