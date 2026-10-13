'use strict';
/**
 * Перевод отчёта sr-lab в замечания «Проблем» (src/srlab.js) без VS Code: временный
 * проект мода с исходниками и отчёт того же вида, что пишет
 * `sr.py test local … --json` (поля results[].stages.*, results[].reasons).
 *
 *     node extension/test/srlab.test.js
 */
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const EXT = path.resolve(__dirname, '..');
const srlab = require(path.join(EXT, 'src', 'srlab.js'));

let failures = 0;
function check(name, fn) {
    try {
        fn();
        console.log(`  ok   ${name}`);
    } catch (e) {
        failures += 1;
        console.log(`  FAIL ${name}\n       ${e.message}`);
    }
}

// --- проект мода ---------------------------------------------------------
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'srmod-srlab-'));
function put(rel, text) {
    const file = path.join(root, rel);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, text, 'utf8');
    return file;
}
put('srmod.json', JSON.stringify({
    name: 'TowBeam', install: 'Mods/Artem/TowBeam',
    script: { engine: 'rsmc', entry: 'src/DATA/Script/TowBeam.src/main.rsm' },
}));
const main = put('src/DATA/Script/TowBeam.src/main.rsm', 'import from "./code.rsm";\n');
const code = put('src/DATA/Script/TowBeam.src/code.rsm', [
    'function TB_Index(dword obj) {',
    '  int id;',
    '  id = Id(obj);',
    '  vMissing = vMissing + 1;   // счётчик',
    '  result = -1;',
    '}',
    'function TB_Other() {',
    '  vMissing=vMissing+1;',
    '}',
].join('\r\n'));
const mainTxt = put('src/CFG/Main.txt', 'Data {\n}\n');
const langTxt = put('src/CFG/Rus/Lang.txt', 'Script {\n}\n');
put('build/DATA/Script/TowBeam.scr', 'binary');
put('build/CFG/Main.dat', 'binary');
const manifest = path.join(root, 'srmod.json');
const build = path.join(root, 'build');
const scr = path.join(build, 'DATA', 'Script', 'TowBeam.scr');

function report(result) {
    return { info: { local: [build] }, results: [Object.assign({
        mod: 'Artem\\TowBeam', verdict: 'fail', reasons: [], stages: {},
    }, result)] };
}
function where(list) {
    return list.map((p) => [path.relative(root, p.file), p.line, p.severity]);
}

console.log('== srlab: отчёт → «Проблемы»');

check('код из .scr находится в .rsm — во всех местах, без пробелов и комментариев', () => {
    const list = srlab.problems(report({ stages: { static: { errors: [{
        path: scr, line: 13, level: 'ошибка', rule: 'undeclared-name',
        code: 'vMissing = vMissing + 1;', message: 'имя «vMissing» не объявлено',
    }] } } }), { projectDir: root, modId: 'Artem\\TowBeam' });
    const found = list.filter((p) => p.file === code);
    assert.deepStrictEqual(found.map((p) => [p.line, p.severity]), [[3, 'error'], [7, 'error']]);
    assert.ok(found[0].message.includes('[undeclared-name]'), found[0].message);
    assert.strictEqual(found[0].code, 'undeclared-name');
});

check('кода нет в исходниках — точка входа, строка 1, код в тексте', () => {
    const list = srlab.problems(report({ stages: { static: { warnings: [{
        path: scr, line: 40, level: 'внимание', rule: 'cfg-name',
        code: 'FindBonusByNameInCfg("Nope");', message: 'бонуса нет',
    }] } } }), { projectDir: root });
    const item = list.find((p) => p.severity === 'warning');
    assert.deepStrictEqual([item.file, item.line], [main, 0]);
    assert.ok(item.message.includes('FindBonusByNameInCfg("Nope");'), item.message);
});

check('датник build/X.dat → исходник src/X.txt; файл зависимости — на srmod.json', () => {
    const dep = path.join(os.tmpdir(), 'Mods', 'Tweaks', 'Other', 'CFG', 'CacheData.dat');
    const list = srlab.problems(report({ stages: {
        registry: { errors: [
            { rule: 'missing-key', level: 'ошибка', file: 'CFG\\Main.dat', message: 'нет ключа' },
            { rule: 'cache-wrong-kind', level: 'ошибка', file: dep, message: 'зависимость: не тот ключ' },
        ] },
        dats: { unreadable: [{ file: 'CFG\\Rus\\Lang.dat', error: 'обрыв' }] },
    } }), { projectDir: root });
    assert.deepStrictEqual(where(list.filter((p) => p.code !== 'sr-lab')), [
        [path.relative(root, mainTxt), 0, 'error'],
        ['srmod.json', 0, 'error'],
        [path.relative(root, langTxt), 0, 'warning'],
    ]);
    assert.ok(list[1].message.includes(dep), list[1].message);
});

check('итог с причинами — одной строкой на srmod.json', () => {
    const list = srlab.problems(report({ verdict: 'crash', reasons: [
        { stage: 'game', text: 'игра упала на 3-м ходу' }] }), { projectDir: root });
    assert.deepStrictEqual(where(list), [['srmod.json', 0, 'error']]);
    assert.ok(/игра упала.*3-м ходу/.test(list[0].message), list[0].message);
    const passed = srlab.problems(report({ verdict: 'pass' }), { projectDir: root });
    assert.deepStrictEqual(passed, []);
});

check('чужие результаты (другой мод набора) не берутся, если есть свой', () => {
    const data = report({ verdict: 'pass' });
    data.results.push({ mod: 'Tweaks\\Other', verdict: 'fail', reasons: [{ text: 'чужое' }] });
    assert.deepStrictEqual(srlab.problems(data, { projectDir: root, modId: 'artem/towbeam' }), []);
});

check('сравнение кода: комментарий вне кавычек, // внутри строки — часть кода', () => {
    assert.strictEqual(srlab.normalizeCode('  Trace("http://x");  // c'), 'trace("http://x");');
    assert.strictEqual(srlab.normalizeCode('A = B + 1; // x'), 'a=b+1;');
});

check('итог строкой состояния', () => {
    const list = [{ severity: 'error' }, { severity: 'warning' }, { severity: 'warning' }];
    assert.strictEqual(srlab.summary(report({}), list), 'не прошёл; ошибок 1, предупреждений 2');
});

fs.rmSync(root, { recursive: true, force: true });
console.log(failures ? `\nПРОВАЛОВ: ${failures}` : '\nвсё зелёное');
process.exit(failures ? 1 : 0);
