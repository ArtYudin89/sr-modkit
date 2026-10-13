'use strict';
/**
 * Отчёт sr-lab → замечания для панели «Проблемы».
 *
 * sr-lab — отдельный инструмент (не часть sr-modkit): расширение запускает
 * `python <sr-lab>/sr.py test local <мод>/build --as Группа\Мод --json <файл>`
 * и читает только этот файл. Здесь нет ни строки логики sr-lab — лишь перевод
 * полей его отчёта (results[].stages.*, results[].reasons) в «файл, строка,
 * важность, текст». Модуль не зависит от VS Code: его проверяет
 * test/srlab.test.js простым node.
 *
 * Находки sr-lab указывают на файлы СОБРАННОГО мода (build/…), а модер правит
 * исходники (src/…), поэтому место переносится:
 *   - скрипт .scr → строки исходников .rsm/.rson с тем же кодом (поле `code`
 *     находки: сравнение без пробелов, регистра и комментариев); нет такой
 *     строки — точка входа скрипта, строка 1, а код — в тексте замечания;
 *   - build/X.dat → src/X.txt (датник собирается из текста), build/X → src/X;
 *   - всё, что не легло на файл мода, и итог теста — на srmod.json.
 */
const fs = require('fs');
const path = require('path');

const SEVERITY = { 'ошибка': 'error', 'внимание': 'warning' };
const FAILED = new Set(['fail', 'error', 'crash', 'hang', 'timeout']);
const MAX_PLACES = 10;   // мест одной находки (одинаковая строка кода повторяется)

/** Код строки без пробелов, регистра и комментария `//…` (вне кавычек). */
function normalizeCode(text) {
    let out = '';
    let quote = null;
    for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        if (quote) {
            if (ch === quote) {
                quote = null;
            }
        } else if (ch === '"' || ch === "'") {
            quote = ch;
        } else if (ch === '/' && text[i + 1] === '/') {
            break;
        }
        out += ch;
    }
    return out.replace(/\s+/g, '').toLowerCase();
}

function readText(file) {
    try {
        const text = fs.readFileSync(file, 'utf8');
        return text.charCodeAt(0) === 0xFEFF ? text.slice(1) : text;
    } catch (e) {
        return null;
    }
}

function walk(dir, exts, out) {
    let entries;
    try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (e) {
        return out;
    }
    for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            walk(full, exts, out);
        } else if (exts.includes(path.extname(entry.name).toLowerCase())) {
            out.push(full);
        }
    }
    return out;
}

/** Внутри ли `child` папки `parent` (регистр букв на Windows не важен). */
function inside(parent, child) {
    const rel = path.relative(parent, child);
    return Boolean(rel) && !rel.startsWith('..') && !path.isAbsolute(rel);
}

/** Скрипты мода из srmod.json: [{name, entry}] — как их понимает `srmod build`. */
function projectScripts(projectDir) {
    let cfg = {};
    try {
        cfg = JSON.parse(readText(path.join(projectDir, 'srmod.json')) || '{}');
    } catch (e) {
        cfg = {};
    }
    const listed = Array.isArray(cfg.scripts) && cfg.scripts.length ? cfg.scripts
        : [{ name: cfg.name, entry: (cfg.script || {}).entry }];
    const scriptDir = path.join(projectDir, 'src', 'DATA', 'Script');
    return listed.filter((s) => s && s.name).map((s) => {
        const candidates = [];
        if (s.entry) {
            candidates.push(path.join(projectDir, s.entry));
        }
        candidates.push(path.join(scriptDir, `${s.name}.src`, 'main.rsm'));
        candidates.push(path.join(scriptDir, `${s.name}.rsm`));
        return { name: s.name, entry: candidates.find((c) => fs.existsSync(c)) || null };
    });
}

/** Где в исходниках строка кода находки: [{file, line}] (line с нуля). */
function findCode(code, files, cache) {
    const want = normalizeCode(code || '');
    if (!want) {
        return [];
    }
    const exact = [];
    const partial = [];
    for (const file of files) {
        if (!cache.has(file)) {
            const text = readText(file);
            cache.set(file, text === null ? [] : text.split(/\r?\n/).map(normalizeCode));
        }
        cache.get(file).forEach((line, index) => {
            if (line === want) {
                exact.push({ file, line: index });
            } else if (line.includes(want)) {
                partial.push({ file, line: index });
            }
        });
    }
    return (exact.length ? exact : partial).slice(0, MAX_PLACES);
}

/**
 * Замечания из отчёта sr-lab: [{file, line, severity, message, code}].
 * `ctx`: {projectDir, buildDir?, modId?}. Важность: 'error' | 'warning' | 'info'.
 */
function problems(report, ctx) {
    const projectDir = ctx.projectDir;
    const buildDir = ctx.buildDir || path.join(projectDir, 'build');
    const srcDir = path.join(projectDir, 'src');
    const manifest = path.join(projectDir, 'srmod.json');
    const scripts = projectScripts(projectDir);
    const scriptSources = walk(path.join(srcDir, 'DATA', 'Script'), ['.rsm', '.rson'], []);
    const cache = new Map();
    const out = [];

    const add = (file, line, severity, message, code) => {
        out.push({ file, line: Math.max(0, line || 0), severity, message, code: code || '' });
    };

    // build/… → src/…: тот же путь или текст, из которого собран датник
    const sourceOf = (built) => {
        const rel = path.relative(buildDir, built);
        const plain = path.join(srcDir, rel);
        if (fs.existsSync(plain)) {
            return plain;
        }
        if (/\.dat$/i.test(rel)) {
            const text = path.join(srcDir, rel.replace(/\.dat$/i, '.txt'));
            if (fs.existsSync(text)) {
                return text;
            }
        }
        return null;
    };

    const place = (finding, severity) => {
        const label = finding.rule ? ` [${finding.rule}]` : '';
        const message = `${finding.message || finding.error || finding.msg || 'замечание sr-lab'}${label}`;
        let file = finding.path || finding.file || '';
        if (file && !path.isAbsolute(file)) {
            file = path.join(buildDir, file);
        }
        if (!file || !inside(buildDir, file)) {
            // файл другого мода набора или вне мода — показываем на srmod.json
            add(manifest, 0, severity, file ? `${message} (${file})` : message, finding.rule);
            return;
        }
        if (/\.scr$/i.test(file)) {
            const name = path.basename(file, path.extname(file)).toLowerCase();
            const own = scripts.find((s) => s.name.toLowerCase() === name);
            const ownDir = own && own.entry ? path.dirname(own.entry) : null;
            const mine = ownDir ? scriptSources.filter((f) => f === own.entry || inside(ownDir, f))
                : [];
            let places = findCode(finding.code, mine, cache);
            if (!places.length) {
                places = findCode(finding.code, scriptSources, cache);
            }
            if (places.length) {
                for (const p of places) {
                    add(p.file, p.line, severity, message, finding.rule);
                }
                return;
            }
            const where = finding.code ? ` — в собранном скрипте: ${finding.code}` : '';
            add((own && own.entry) || manifest, 0, severity, `${message}${where}`, finding.rule);
            return;
        }
        const source = sourceOf(file);
        if (source) {
            const places = finding.code ? findCode(finding.code, [source], cache) : [];
            add(source, places.length ? places[0].line : lineOf(finding), severity, message,
                finding.rule);
            return;
        }
        add(fs.existsSync(file) ? file : manifest, lineOf(finding), severity, message, finding.rule);
    };

    const results = (report && report.results) || [];
    const wanted = ctx.modId ? results.filter((r) => sameMod(r.mod, ctx.modId)) : [];
    for (const result of wanted.length ? wanted : results) {
        const stages = result.stages || {};
        const statics = stages.static || {};
        const registry = stages.registry || {};
        for (const f of (statics.errors || []).concat(registry.errors || [])) {
            place(f, SEVERITY[f.level] || 'error');
        }
        for (const f of (statics.warnings || []).concat(registry.warnings || [])) {
            place(f, SEVERITY[f.level] || 'warning');
        }
        for (const file of statics.unreadable || []) {
            place({ path: file, message: 'sr-lab не смог прочитать этот скрипт' }, 'warning');
        }
        for (const f of ((stages.dats || {}).unreadable || [])) {
            place({ file: f.file, message: `датник не читается: ${f.error}` }, 'warning');
        }
        const quests = stages.quests || {};
        for (const f of (quests.errors || []).concat(quests.unreadable || [])) {
            place(typeof f === 'string' ? { path: f, message: 'квест не читается' } : f, 'warning');
        }
        // итог: почему мод не прошёл (игра, проверки) — одной строкой на srmod.json
        const reasons = (result.reasons || []).map((r) => r.text || String(r)).filter(Boolean);
        if (reasons.length || FAILED.has(result.verdict)) {
            const severity = FAILED.has(result.verdict) ? 'error'
                : (result.verdict === 'pass' ? 'info' : 'warning');
            add(manifest, 0, severity,
                `sr-lab: ${result.mod || ''} — ${verdictText(result.verdict)}`
                + (reasons.length ? `: ${reasons.join('; ')}` : ''), 'sr-lab');
        }
    }
    return out;
}

function lineOf(finding) {
    return finding.line ? finding.line - 1 : 0;
}

function sameMod(a, b) {
    const norm = (s) => String(s || '').replace(/\//g, '\\').toLowerCase();
    return norm(a) === norm(b);
}

function verdictText(verdict) {
    return {
        pass: 'прошёл', fail: 'не прошёл', error: 'ошибка прогона', crash: 'игра упала',
        hang: 'игра зависла', timeout: 'не уложился во время', skipped: 'пропущен', warn: 'с замечаниями',
    }[verdict] || verdict || 'итог неизвестен';
}

/** Итог отчёта одной строкой для строки состояния. */
function summary(report, list) {
    const results = (report && report.results) || [];
    const verdicts = results.map((r) => verdictText(r.verdict)).join(', ') || 'нет итога';
    const found = list.filter((p) => p.code !== 'sr-lab');   // без строки итога на srmod.json
    const errors = found.filter((p) => p.severity === 'error').length;
    const warnings = found.filter((p) => p.severity === 'warning').length;
    return `${verdicts}; ошибок ${errors}, предупреждений ${warnings}`;
}

module.exports = { problems, summary, normalizeCode, projectScripts, findCode };
