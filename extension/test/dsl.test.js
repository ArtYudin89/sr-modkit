'use strict';
/**
 * Проверка чистой логики подсказок (src/dsl.js) без запуска VS Code: модуль
 * 'vscode' подменяется заглушкой, документ — объектом с теми же тремя методами,
 * которыми пользуется провайдер. Данные берутся из настоящего data/rsm-dsl.json,
 * поэтому тест заодно ловит расхождение схемы с ожиданиями.
 *
 *     node extension/test/dsl.test.js
 */
const Module = require('module');
const path = require('path');
const assert = require('assert');

class Position { constructor(line, character) { this.line = line; this.character = character; } }
class Range { constructor(a, b, c, d) { this.start = a instanceof Position ? a : new Position(a, b); this.end = a instanceof Position ? b : new Position(c, d); } }
class CompletionItem { constructor(label, kind) { this.label = label; this.kind = kind; } }
class MarkdownString { constructor(v) { this.value = v; } }
class SnippetString { constructor(v) { this.value = v; } }
class Hover { constructor(c, r) { this.contents = c; this.range = r; } }

class Location { constructor(uri, pos) { this.uri = uri; this.range = pos; } }

const fakeVscode = {
    Position, Range, CompletionItem, MarkdownString, SnippetString, Hover, Location,
    CompletionItemKind: {
        EnumMember: 'enum', Property: 'prop', Function: 'fn',
        Constant: 'const', Keyword: 'kw',
    },
    Uri: { file: (p) => ({ fsPath: p, toString: () => `file://${p}` }) },
};

const origLoad = Module._load;
Module._load = function (request, parent, isMain) {
    if (request === 'vscode') { return fakeVscode; }
    return origLoad.apply(this, arguments);
};

const EXT = path.resolve(__dirname, '..');
const dsl = require(path.join(EXT, 'src', 'dsl.js'));
const context = { extensionPath: EXT };

// --- фальшивый документ -------------------------------------------------
function doc(text) {
    const lines = text.split('\n');
    return {
        lineAt: (p) => ({ text: lines[p.line] }),
        getText: (range) => {
            if (!range) { return text; }
            const out = [];
            for (let i = range.start.line; i <= range.end.line; i += 1) {
                let line = lines[i];
                if (i === range.end.line) { line = line.slice(0, range.end.character); }
                if (i === range.start.line) { line = line.slice(range.start.character); }
                out.push(line);
            }
            return out.join('\n');
        },
        getWordRangeAtPosition: (p, re) => {
            const line = lines[p.line];
            let m;
            const rx = new RegExp(re.source, 'g');
            while ((m = rx.exec(line))) {
                if (m.index <= p.character && p.character <= m.index + m[0].length) {
                    return new Range(new Position(p.line, m.index), new Position(p.line, m.index + m[0].length));
                }
            }
            return null;
        },
    };
}

function labels(items) { return (items || []).map((i) => i.label); }

let failures = 0;
function check(name, fn) {
    try { fn(); console.log('  OK  ' + name); } catch (e) {
        failures += 1; console.log('  FAIL ' + name + ': ' + e.message);
    }
}

console.log('== mask');
check('строка не влияет на скобочный баланс', () => {
    const masked = dsl.mask('text: "a ) { b", code:');
    assert.strictEqual(masked.length, 'text: "a ) { b", code:'.length);
    assert.ok(!masked.includes(')'), 'скобка внутри строки должна быть погашена: ' + masked);
});
check('// гаснет только до конца строки', () => {
    const masked = dsl.mask('a // ) comment\nb )');
    assert.ok(masked.endsWith('b )'), masked);
});
check('/* */ тянется через строки', () => {
    const masked = dsl.mask('a /* )\n) */ b )');
    assert.strictEqual((masked.match(/\)/g) || []).length, 1);
});

console.log('== enclosingDecl');
const cases = [
    ['planet("P", {star: "S", ', 'planet'],
    ['state("St", {move: "none", code: function() {\n  if(x == 1) { y = 2; }\n  ', 'state'],
    ['group("G", {owner: ["Maloc", ', 'group'],
    ['planet("P", {star: "S"});\n', null],
    ['dialogMsg("M", {text: "a) {b", ', 'dialogMsg'],
];
for (const [text, expect] of cases) {
    check(JSON.stringify(text.slice(0, 34)) + ' -> ' + expect, () => {
        assert.strictEqual(dsl.enclosingDecl(dsl.mask(text), text.length), expect);
    });
}

console.log('== completion');
const provider = dsl.completionProvider(context);
function complete(text) {
    const lines = text.split('\n');
    const pos = new Position(lines.length - 1, lines[lines.length - 1].length);
    return labels(provider.provideCompletionItems(doc(text), pos));
}

check('поля planet', () => {
    const got = complete('planet("P", {');
    assert.ok(got.includes('star') && got.includes('government'), got.join(','));
    assert.ok(!got.includes('move'), 'поля другой декларации: ' + got.join(','));
});
check('значения planet.government внутри кавычек', () => {
    const got = complete('planet("P", {star: "S", government: "');
    assert.deepStrictEqual(got, ['GroupToggle', 'Anarchy', 'Dictatorship', 'Monarchy', 'Republic', 'Democracy']);
});
check('значения внутри массива owner: ["', () => {
    const got = complete('group("G", {planet: "P", state: "S", owner: ["');
    assert.ok(got.includes('ByPlayer') && got.includes('Kling'), got.join(','));
});
check('второй элемент массива тоже дополняется', () => {
    const got = complete('group("G", {owner: ["Maloc", "');
    assert.ok(got.includes('Peleng'), got.join(','));
});
check('имя декларации в начале строки', () => {
    const got = complete('scriptName("X");\n');
    assert.ok(got.includes('dialogMsg') && got.includes('starLink'), got.join(','));
});
check('внутри обычной строки подсказок нет', () => {
    assert.deepStrictEqual(complete('dialogMsg("M", {text: "прив'), []);
});
check('state.move валидируется, item.useless — нет', () => {
    const items = provider.provideCompletionItems(
        doc('state("S", {move: "'), new Position(0, 'state("S", {move: "'.length));
    assert.ok(items.length >= 6, 'move: ' + labels(items).join(','));
    assert.ok(!/не проверяет/.test(items[0].detail), items[0].detail);
    const un = provider.provideCompletionItems(
        doc('item("i", {useless: "'), new Position(0, 'item("i", {useless: "'.length));
    assert.ok(un.length === 0 || /не проверяет/.test(un[0].detail || ''), 'useless без пометки');
});

console.log('== hover');
const hover = dsl.hoverProvider(context);
check('hover по декларации', () => {
    const text = 'planet("P", {star: "S"});';
    const h = hover.provideHover(doc(text), new Position(0, 2));
    assert.ok(h && /Обязательные поля/.test(h.contents.value), JSON.stringify(h));
});
check('hover по полю внутри декларации', () => {
    const text = 'planet("P", {government: "Anarchy"});';
    const h = hover.provideHover(doc(text), new Position(0, 16));
    assert.ok(h && /Democracy/.test(h.contents.value), JSON.stringify(h && h.contents));
});

console.log('== форма флаговых полей');
check('флаговое поле вставляется массивом', () => {
    const items = provider.provideCompletionItems(
        doc('planet("P", {'), new Position(0, 'planet("P", {'.length));
    const race = items.find((i) => i.label === 'race');
    assert.ok(race && /\["\$0"\]/.test(race.insertText.value),
        'race должен вставляться массивом: ' + (race && race.insertText.value));
    const rangeMin = items.find((i) => i.label === 'rangeMin');
    assert.ok(!/\[/.test(rangeMin.insertText.value), rangeMin.insertText.value);
});
check('hover флагового поля предупреждает про потерю строки', () => {
    const text = 'planet("P", {race: ["Maloc"]});';
    const h = hover.provideHover(doc(text), new Position(0, 15));
    assert.ok(h && /только массивом/.test(h.contents.value), JSON.stringify(h && h.contents));
});

console.log('== сниппеты против схемы');
// Списки значений в сниппетах писались раньше схемы и разъезжались с ней
// (в place были выдуманные inSpace/nearStar) — теперь это ловит тест.
const snippets = require(path.join(EXT, 'snippets', 'rangers-script.json'));
const schema = dsl.load(context);
for (const [name, snip] of Object.entries(snippets)) {
    const decl = schema.declarations[name];
    if (!decl || !decl.enums) { continue; }
    const body = [].concat(snip.body).join('\n');
    const rx = /([A-Za-z_][A-Za-z0-9_]*):\s*\\?"?\$\{\d+\|([^|]+)\|\}/g;
    let m;
    while ((m = rx.exec(body))) {
        const [, field, choices] = m;
        const en = decl.enums[field];
        if (!en) { continue; }
        check(`${name}.${field}: значения сниппета есть в схеме`, () => {
            const bad = choices.split(',').map((s) => s.trim())
                .filter((v) => !en.values.includes(v));
            assert.deepStrictEqual(bad, [], `нет в схеме: ${bad.join(', ')}`);
        });
    }
}

console.log('== встроенные функции (lexicon)');
const lexicon = require(path.join(EXT, 'src', 'lexicon.js'));
const lexComplete = lexicon.completionProvider(context);
check('ChangeState есть в лексиконе с описанием', () => {
    const entry = lexicon.load(context).ChangeState;
    assert.ok(entry && /state|стэйт/i.test(entry.summary || ''), JSON.stringify(entry));
    assert.strictEqual(entry.min, 1);
});
check('функции подсказываются в теле function', () => {
    const text = 'state("S", {code: function() {\n    Change';
    const lines = text.split('\n');
    const items = lexComplete.provideCompletionItems(
        doc(text), new Position(lines.length - 1, lines[lines.length - 1].length));
    assert.ok(labels(items).includes('ChangeState'), 'нет ChangeState');
    assert.ok(items.length > 500, 'подозрительно мало: ' + items.length);
});
check('внутри строки функций не предлагаем', () => {
    const text = 'state("S", {move: "no';
    const items = lexComplete.provideCompletionItems(doc(text), new Position(0, text.length));
    assert.deepStrictEqual(items, []);
});
check('hover по встроенной функции', () => {
    const text = '    ChangeState("St1");';
    const h = lexicon.hoverProvider(context).provideHover(doc(text), new Position(0, 8));
    assert.ok(h && /ChangeState/.test(h.contents.value), JSON.stringify(h));
});

console.log('== переход к определению');
const definition = require(path.join(EXT, 'src', 'definition.js'));
check('строковый литерал -> декларация с этим именем', () => {
    const text = 'state("StGreeter", {move: "none"});\nChangeState("StGreeter");';
    const found = definition.declarationsIn(text, new Set(['state', 'planet']));
    assert.deepStrictEqual(found.map((d) => [d.decl, d.name, d.line]),
        [['state', 'StGreeter', 0]]);
});
check('вызов внутри кода за декларацию не считается', () => {
    const text = 'state("A", {code: function() {\n    Trace("planet");\n}});';
    const found = definition.declarationsIn(text, new Set(['state', 'planet']));
    assert.deepStrictEqual(found.map((d) => d.name), ['A']);
});

console.log(failures ? `\nПРОВАЛОВ: ${failures}` : '\nвсё зелёное');
process.exit(failures ? 1 : 0);
