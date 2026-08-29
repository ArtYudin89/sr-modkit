# Черновики issue для автора rsmc (indiemagpie)

Каждый пункт — отдельный issue. Все примеры написаны с нуля специально для
этих проверок и воспроизводятся на чистом окружении: нужны только `rsmc` и
`RScript 4.14f`. Проверено 2026-08-04, rsmc из релиза rsmc.zip (флага
`--version` у программы нет), Windows 10.

Правило (см. память `feedback_upstream_issues`): репро — только свои
минимальные моды, чужие можно упоминать лишь как «замечено в таких-то модах»;
текст простой, без внутреннего жаргона.

Общая заготовка `main.rsm` для issue 1 и 2 (минимальный собираемый мод —
звезда, планета, корабль игрока, группа, состояние):

```js
scriptName("TestCT");

star("MainStar", {noKling: false, noComeKling: false});
planet("MainPlanet", {star: "MainStar", race: ["Maloc", "Peleng", "People", "Fei", "Gaal"], owner: ["GroupToggle", "Maloc", "Peleng", "People", "Fei", "Gaal"], economy: ["Agriculture", "Industrial", "Mixed"], government: ["Anarchy", "Dictatorship", "Monarchy", "Republic", "Democracy"], rangeMin: 0, rangeMax: 100});
ship({star: "MainStar", player: true, count: 1, owner: ["Maloc", "Peleng", "People", "Fei", "Gaal"], type: ["Ranger", "Warrior", "Pirate", "Transport", "Liner", "Diplomat"], speedMin: 0, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, strengthMin: 0, strengthMax: 0, ruins: ""});
group("Greeter", {planet: "MainPlanet", state: "GreetState", owner: ["Maloc", "Peleng", "People", "Fei", "Gaal"], type: ["GroupToggle", "Ranger"], countMin: 1, countMax: 1, speedMin: 100, speedMax: 10000, weapon: 0, cargoHook: 0, emptySpace: 0, addPlayer: false, statusTraderMin: 0, statusTraderMax: 100, statusWarriorMin: 0, statusWarriorMax: 100, statusPirateMin: 0, statusPirateMax: 100, distSearch: 10000, strengthMin: 0, strengthMax: 0, ruins: ""});
state("GreetState", {move: "none", takeAllItem: false, onTalk: "dHello"});

export function onGlobal() {
  //;
}
```

---

## 1. Готовый ключ текста внутри `CT("...")` заносится в таблицу текстов как обычный текст

**Что происходит.** Если в коде диалогового сообщения уже стоит вызов
`CT("Script.Имя.Номер")` (готовая ссылка на текст в Lang.dat), rsmc считает
строку в кавычках обычным текстом: кладёт её в свою таблицу текстов и
подставляет в код новый ключ. В собранном скрипте получается вложенный вызов
`CT(CT("..."))`, а в таблице — строка-ключ вместо настоящего текста.

**Как воспроизвести.** К заготовке выше добавить диалог:

```js
dialog("dHello", {code: function() {
    DChange(0);
    exit;
}});

dialogMsg("MsgHello", {text: "", code: function() {
    DText(CT("Script.TestCT.5"));
    DAdd(0);
}});

dialogAnswer("AnsBye", {text: "Пока.", answerCommand: "exit"});
```

```
rsmc build main.rsm -o out.scr
```

В таблице текстов `out.txt` первая запись — строка-ключ вместо текста:

```
0=Script.TestCT.5
1=
2=Пока.
```

Код собранного сообщения (виден через `RScript --cli -d out.scr out.rson`):

```
DText(CT("Script.TestCT.1"));
DText(CT(CT("Script.TestCT.0")));
DAdd(0);
```

Заодно видно, что rsmc добавил свой `DText` из поля `text:` первым
оператором, хотя `text` пустой.

**Почему это важно.** Ломается заявленный сценарий «разобрать существующий
мод через `RScript --cli -x`, поправить, собрать rsmc»: у большинства
распространяемых модов тексты лежат в Lang.dat, и весь их код диалогов
состоит из таких `CT("...")`. В игре двойной вызов случайно работает (внешний
`CT` находит текст по старому ключу), но стоит слить таблицу rsmc в ту же
секцию Lang.dat через `--lang-txt`/`--lang-dat` — старые тексты перетираются
строками-ключами, и игрок видит «Script.TestCT.5» вместо реплик.

**Ожидание.** Строку внутри `CT(...)` не трогать — это уже ссылка на текст, а
не текст. И не генерировать `DText` из пустого `text:`.

---

## 2. `ChangeState(1)` переходит в состояние по ИМЕНИ «1», а не по номеру 1

**Что происходит.** Если в моде есть состояние, имя которого — число, вызов
`ChangeState` с числом компилируется как переход в это состояние, а не в
состояние с таким порядковым номером.

**Как воспроизвести.** К заготовке выше (scriptName заменить на "TestState")
добавить три состояния; группа ссылается на первое:

```js
state("StateA", {move: "none", takeAllItem: false, code: function() {
    ChangeState(1);
}});
state("StateB", {move: "none", takeAllItem: false});
state("1", {move: "none", takeAllItem: false});
```

```
rsmc build main.rsm -o out.scr
RScript --cli -d out.scr out.rson
```

В `out.rson` вызов превратился в `ChangeState(2)` — это номер состояния,
которое НАЗЫВАЕТСЯ «1». Ожидался переход в состояние номер 1 («StateB»), как
это делает RScript.

**Почему это важно.** Подмена молчаливая — мод собирается без предупреждений,
но логика переходов другая. Состояния с числовыми именами встречаются в
распространяемых модах.

**Ожидание.** Число — всегда порядковый номер (как в RScript), либо ошибка
компиляции при неоднозначности.

---

## 3. Код с лишней «{» : RScript собирает, rsmc — нет, и ошибка указывает не туда

**Что происходит.** Редактор RScript прощает лишнюю открывающую скобку в коде
(блок открыт и не закрыт до конца кода). rsmc такой код не принимает. Само по
себе это честнее, но: (а) моды, собранные автором в RScript, после
`RScript --cli -x` не собираются обратно; (б) ошибка указывает на конец
файла, а не на строку с незакрытой скобкой.

**Как воспроизвести.** В RScript в любом диалоге написать код с незакрытым
блоком — редактор собирает мод без замечаний:

```
if(1 == 1) {
DChange(0);
exit;
```

Экспортировать этот мод: `RScript --cli -x mod.rson mod.rsm --split` — код
попадает в `dialogs.rsm` как есть. Сборка:

```
rsmc build main.rsm -o out.scr
```

```
...\dialogs.rsm(line=5, symbol=3): expected "}"
```

Строка 5 — конец блока кода; незакрытая скобка — на строке 2.

**Ожидание.** В ошибке — строка незакрытой «{» (а не место, где кончился
файл) и, в идеале, подсказка, что RScript этот код принимал: без неё автор
мода не понимает, почему его же мод перестал собираться.

---

## 4. Предупреждение «no existing Script block — skipped» завершает сборку с кодом 0

**Что происходит.** Ошибки rsmc честно возвращают ненулевой код завершения
(например, недоступный путь у `-o` даёт `error: TFileEC.CreateNew`, код 8).
Но предупреждение о том, что тексты НЕ слиты в языковой файл, завершает
сборку с кодом 0:

```
rsmc build main.rsm -o out.scr --lang-txt Lang.txt
warning: "Lang.txt" has no existing Script^{ TestCT^{...} } block to merge into - skipped
OK: wrote out.scr
(код завершения 0)
```

**Почему это важно.** В скрипте сборки такой запуск выглядит успешным, а мод
уезжает к игрокам без текстов: в игре вместо реплик — «Script.Имя.Номер».
Приходится разбирать текст stdout, чтобы поймать это.

**Ожидание.** Ненулевой код завершения для предупреждений, которые дают
заведомо неработающий результат (или флаг `--strict`).

---

## 5. `--lang-txt` не находит блок, записанный в одну строку

**Что происходит.** Блок для слияния ищется построчно, а не разбором
формата. Однострочная запись — валидный текстовый BlockPar, но rsmc её не
видит.

**Как воспроизвести.** Два языковых файла (кодировка UTF-16 LE с BOM),
отличается только расстановка переносов.

`lang_oneline.txt`:

```
Script ^{ TestCT ^{ } }
```

`lang_multiline.txt`:

```
Script ^{
    TestCT ^{
    }
}
```

```
rsmc build main.rsm -o out.scr --lang-txt lang_oneline.txt
warning: ... has no existing Script^{ TestCT^{...} } block to merge into - skipped

rsmc build main.rsm -o out.scr --lang-txt lang_multiline.txt
OK: merged script block "TestCT" into lang_multiline.txt
```

**Ожидание.** Либо разбирать формат честно (перенос строки — просто
пробельный символ), либо явно описать требование к форматированию в README —
и тогда давать ошибку, а не предупреждение с кодом 0 (см. пункт 4).

---

## 6. Значение поля-набора, записанное строкой, теряется молча

Проверено 2026-08-28, тем же rsmc.

**Что происходит.** Поля-наборы (`race`, `owner`, `economy`, `government`,
`type` у ship и group) компилятор проверяет только внутри квадратных скобок.
Если написать значение просто строкой, сборка проходит без единого сообщения,
но значение в скрипт не попадает: файл получается точно такой же, как если бы
там стояла бессмыслица.

**Как воспроизвести.** Взять минимальный мод и собрать его трижды, меняя одну
строку планеты:

```js
planet("MainPlanet", {star: "MainStar", race: ["People"], ...});   // (а)
planet("MainPlanet", {star: "MainStar", race: "People", ...});     // (б)
planet("MainPlanet", {star: "MainStar", race: "Zzz", ...});        // (в)
```

```
(а) OK: wrote out.scr
(б) OK: wrote out.scr
(в) OK: wrote out.scr
```

Файлы (б) и (в) совпадают побайтово, а (а) от них отличается: в форме строки
значение не учитывается вообще. Для сравнения, в форме списка неверное значение
честно отвергается:

```
race: ["Zzz"]
main.rsm(line=3, symbol=101): unknown flag "Zzz" for "race" (expected one of: GroupToggle, Maloc, Peleng, People, Fei, Gaal)
```

**Ожидание.** В форме строки — либо принимать значение как набор из одного
элемента, либо давать ту же ошибку. Сейчас автор видит успешную сборку и
получает мод, в котором раса/владелец/тип заданы «ничем».

---

## 7. Неизвестное поле у большинства деклараций проходит молча

Проверено 2026-08-28, тем же rsmc.

**Что происходит.** У `state(...)` опечатка в имени поля — ошибка сборки, и это
удобно:

```
state("GreetState", {move: "none", zzz: 1});
main.rsm(line=5, symbol=34): unknown state(...) field "zzz"
```

А у `planet`, `group`, `star`, `ship`, `place`, `item`, `ether`, `groupLink`,
`starLink` то же самое проходит без сообщения:

```
planet("MainPlanet", {star: "MainStar", zzz: 1, ...});
OK: wrote out.scr
```

**Как воспроизвести.** В минимальный мод добавить `zzz: 1` в любую из этих
деклараций и собрать.

**Ожидание.** Одинаковое поведение у всех деклараций — сообщение
`unknown <декларация>(...) field "zzz"`. Сейчас опечатка в имени поля (`rangeMinn`
вместо `rangeMin`) доезжает до игры незамеченной.

---

## 8. Текст диалогового сообщения с подстановками `<0>`, `<1>` собирается с числами вместо переменных

Проверено 2026-08-29, тем же rsmc.

**Что происходит.** Если текст сообщения содержит места подстановки `<0>`,
`<1>`, rsmc собирает вызов `Format`, где на месте подставляемых значений стоят
номера самих подстановок:

```
DText(Format(CT("Script.TestFmt.2"),"<0>",0,"<1>",1))
```

То есть в игре игрок увидит «Привет, 0! Твой корабль: 1.» Задать, ЧТО
подставлять (переменную, вызов функции), в `.rsm` нечем: в `dialogMsg` есть
только `text`, `command`, `code` и `answers`.

**Как воспроизвести.** Минимальный мод (заготовка из начала файла, `scriptName`
заменён на `TestFmt`), к ней:

```js
localVar("temp_str", "str", "");
localVar("temp_str2", "str", "");

dialog("dHello", {code: function() {
    temp_str = "Иванов";
    temp_str2 = "Мираж";
    DChange(0);
    exit;
}});

dialogMsg("MsgHello", {text: "Привет, <0>! Твой корабль: <1>.", code: function() {
    DAdd(0);
}});

dialogAnswer("AnsBye", {text: "Пока.", answerCommand: "exit"});
```

```
rsmc build main.rsm -o out.scr
RScript --cli -d out.scr out.rson
```

В `out.rson` у сообщения:

```
DText(Format(CT("Script.TestFmt.2"),"<0>",0,"<1>",1))
```

Два обхода, которые не работают:

1. Поле `command` со строкой `DText(Format(CT(<<0>>),"<0>",temp_str,"<1>",temp_str2))`
   собирается без ошибки, но попадает в имя (`Name`) узла сообщения, а на
   собранный `DText` не влияет — он тот же, с `0` и `1`.
2. Явный вызов в `code`:

```js
dialogMsg("MsgHello", {text: "Привет, <0>! Твой корабль: <1>.", code: function() {
    DText(Format(CT("Script.TestFmt.0"),"<0>",temp_str,"<1>",temp_str2));
    DAdd(0);
}});
```

   Вызов пропадает из собранного кода целиком (в скрипте остаётся только
   `DAdd(0);`), а обе строки из него уходят в таблицу текстов как обычные
   тексты и сдвигают нумерацию остальных:

```
0=Мираж
1=Иванов
2=<0>
3=Script.TestFmt.0
4=Привет, <0>! Твой корабль: <1>.
5=Пока.
```

   (записи 2 и 3 — потерянный `DText`; см. также issue 1.)

**Почему это важно.** Параметризованный текст — обычное дело в диалоговых
модах: «<0>» — имя собеседника, название книги, номер страницы. Такой мод
нельзя ни написать в `.rsm` с нуля, ни разобрать существующий через
`RScript --cli -x` и собрать обратно: подстановки превращаются в числа, и
пересобранный мод показывает игроку «0» вместо текста. У нас это главная
причина, по которой диалоговые моды приходится оставлять готовым `.scr`.

**Ожидание.** Способ задать аргументы подстановки — например
`dialogMsg("M", {text: "Привет, <0>!", args: [temp_str]})` — или сохранение
явного `DText(Format(...))` из `code` вместо генерации своего.
