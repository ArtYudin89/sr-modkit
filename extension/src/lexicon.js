'use strict';
/**
 * Подсказки по встроенному языку скриптов: 977 имён (678 функций движка,
 * 41 из UtilityFunctionsPack, 242 константы, 16 ключевых слов) с арностью и
 * коротким описанием. Данные — `extension/data/lexicon.json`, компактная
 * выжимка из корневого `data/lexicon.json` (tools/sync_extension.py), который
 * собирается из схемы RScript + мануала + корпуса (tools/build_lexicon.py).
 *
 * Это дополнение к dsl.js: там декларации мода (planet/state/…), здесь — то,
 * что зовётся внутри `function() { … }`.
 */
const fs = require('fs');
const path = require('path');
const vscode = require('vscode');

const { openQuote } = require('./dsl');

let lex = null;

function load(context) {
    if (lex) {
        return lex;
    }
    try {
        lex = JSON.parse(fs.readFileSync(
            path.join(context.extensionPath, 'data', 'lexicon.json'), 'utf8')).entries;
    } catch (e) {
        lex = {};
    }
    return lex;
}

const KIND = {
    function: vscode.CompletionItemKind ? vscode.CompletionItemKind.Function : 'fn',
    function_custom: vscode.CompletionItemKind ? vscode.CompletionItemKind.Function : 'fn',
    constant: vscode.CompletionItemKind ? vscode.CompletionItemKind.Constant : 'const',
    keyword: vscode.CompletionItemKind ? vscode.CompletionItemKind.Keyword : 'kw',
};

function signature(name, entry) {
    if (entry.kind === 'constant' || entry.kind === 'keyword') {
        return name;
    }
    const params = entry.params || [];
    if (params.length) {
        return `${name}(${params.map((p, i) => p ? `${i + 1}: ${p}` : `arg${i + 1}`).join(', ')})`;
    }
    if (entry.min !== undefined) {
        return `${name}(${entry.min === entry.max ? entry.min
            : `${entry.min}–${entry.max === undefined ? '…' : entry.max}`} аргум.)`;
    }
    return `${name}(…)`;
}

function docFor(name, entry) {
    const parts = [`\`${signature(name, entry)}\``];
    if (entry.kind === 'function_custom') {
        parts.push('_из UtilityFunctionsPack — мод должен быть у игрока_');
    }
    if (entry.summary) {
        parts.push(entry.summary);
    }
    return parts.join('\n\n');
}

function completionProvider(context) {
    return {
        provideCompletionItems(document, position) {
            const entries = load(context);
            const line = document.lineAt(position).text.slice(0, position.character);
            // Внутри строки и сразу после ключа поля подсказывает dsl.js — там
            // свои списки значений, мешать их с функциями нельзя.
            if (openQuote(line) !== null || /[:.]\s*[A-Za-z0-9_]*$/.test(line)) {
                return [];
            }
            return Object.entries(entries).map(([name, entry]) => {
                const item = new vscode.CompletionItem(name, KIND[entry.kind] || KIND.function);
                item.detail = entry.kind === 'function_custom'
                    ? 'UtilityFunctionsPack' : undefined;
                item.documentation = new vscode.MarkdownString(docFor(name, entry));
                if (entry.kind === 'function' || entry.kind === 'function_custom') {
                    item.insertText = new vscode.SnippetString(`${name}($0)`);
                }
                return item;
            });
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
            const entry = load(context)[word];
            if (!entry) {
                return null;
            }
            return new vscode.Hover(new vscode.MarkdownString(docFor(word, entry)), range);
        },
    };
}

module.exports = { load, completionProvider, hoverProvider, signature, docFor };
