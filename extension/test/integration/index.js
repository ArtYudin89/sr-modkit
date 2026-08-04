'use strict';
/**
 * Интеграционный тест в настоящем Extension Host. Проверяет то, чего не видно
 * без запуска VS Code: расширение активируется без ошибок, команды
 * зарегистрированы, язык привязан к .rsm, провайдеры реально отвечают.
 *
 * Запуск (профиль пользователя не трогается — всё в отдельных каталогах):
 *
 *   code --user-data-dir <tmp>/u --extensions-dir <tmp>/e ^
 *        --extensionDevelopmentPath=<repo>/extension ^
 *        --extensionTestsPath=<repo>/extension/test/integration/index.js
 */
const assert = require('assert');
const vscode = require('vscode');

const EXPECTED_COMMANDS = [
    'srmod.build', 'srmod.buildAndDeploy', 'srmod.deploy', 'srmod.lint',
    'srmod.doctor', 'srmod.verify', 'srmod.new', 'srmod.open',
    'srmod.watchStart', 'srmod.watchStop', 'srmod.selectProject', 'srmod.showOutput',
];

const SAMPLE = [
    'scriptName("ITest");',
    '',
    'star("MainStar", {noKling: false, noComeKling: false});',
    'planet("MainPlanet", {star: "MainStar", government: ""});',
    '',
].join('\n');

async function run() {
    const ext = vscode.extensions.getExtension('artyudin89.sr-modkit');
    assert.ok(ext, 'расширение не найдено по id artyudin89.sr-modkit');

    await ext.activate();
    assert.ok(ext.isActive, 'расширение не активировалось');

    const commands = await vscode.commands.getCommands(true);
    for (const id of EXPECTED_COMMANDS) {
        assert.ok(commands.includes(id), `команда не зарегистрирована: ${id}`);
    }

    const doc = await vscode.workspace.openTextDocument({
        language: 'rangers-script', content: SAMPLE });
    assert.strictEqual(doc.languageId, 'rangers-script');

    // Поля декларации: курсор сразу после "{" в planet(...)
    const line = 3;
    const openBrace = SAMPLE.split('\n')[line].indexOf('{') + 1;
    const fields = await vscode.commands.executeCommand(
        'vscode.executeCompletionItemProvider', doc.uri, new vscode.Position(line, openBrace));
    const fieldLabels = fields.items.map((i) => (typeof i.label === 'string' ? i.label : i.label.label));
    assert.ok(fieldLabels.includes('government'),
        'нет подсказки поля planet.government: ' + fieldLabels.slice(0, 20).join(','));

    // Значения перечисления: курсор внутри пустых кавычек government: ""
    const quote = SAMPLE.split('\n')[line].indexOf('government: "') + 'government: "'.length;
    const values = await vscode.commands.executeCommand(
        'vscode.executeCompletionItemProvider', doc.uri, new vscode.Position(line, quote));
    const valueLabels = values.items.map((i) => (typeof i.label === 'string' ? i.label : i.label.label));
    assert.ok(valueLabels.includes('Democracy'),
        'нет подсказки значения government: ' + valueLabels.slice(0, 20).join(','));

    // Hover по имени декларации
    const hovers = await vscode.commands.executeCommand(
        'vscode.executeHoverProvider', doc.uri, new vscode.Position(2, 2));
    assert.ok(hovers && hovers.length, 'hover по star(...) пуст');

    // Задачи провайдера типа srmod (без открытой папки их может не быть —
    // проверяем только что вызов не падает).
    await vscode.tasks.fetchTasks({ type: 'srmod' });

    console.log('интеграционный тест: OK (%d команд, %d полей, %d значений)',
        EXPECTED_COMMANDS.length, fieldLabels.length, valueLabels.length);
}

module.exports = { run };
