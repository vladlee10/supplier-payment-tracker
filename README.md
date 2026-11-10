# Supplier Payment Tracker · Учёт поставщиков

A local Flask app for tracking supplier deliveries, invoice balances and payments in Korean won. It supports photo import on macOS, monthly Excel statements and a Russian/English interface.

Локальное приложение для магазина: хранит накладные и оплаты, показывает долги и авансы, формирует месячную сверку по поставщику. Использует Python, Flask и SQLite.

## Запуск

Для запуска на macOS или Linux используйте Python 3.11 или новее:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Откройте http://127.0.0.1:5002. При первом запуске приложение создаст пустую базу. Поставщиков, поставки и оплаты можно добавлять вручную.

Для импорта фотографий на macOS установите инструменты командной строки Apple и выполните `python build_ocr.py` из папки проекта. Распознавание использует Apple Vision и не требует API-ключа.

## Накладная по фотографии

1. Откройте «Загрузить накладную» и выберите JPG или PNG до 15 МБ.
2. Проверьте поставщика, дату, товары, количество, цены и НДС по фото. Если на документе есть номер, проверьте и его. Поле номера можно оставить пустым: сайт покажет внутренний номер записи.
3. Исправьте неточности. Таблица допускает добавление и удаление строк.
4. Итог по товарам должен совпадать с итогом на документе. Отметьте проверку и сохраните.
5. В «Оплатах» выберите поставщика и нужную накладную. Кнопка записывает оплату в учёт; банковские переводы приложение не выполняет.

Распознавание работает локально в двух режимах: русский/английский и, если таблица не найдена, корейский/английский. Поддерживаются русские заголовки исходного образца и корейские «품목», «수량», «단가», «공급가액», в том числе отдельный столбец коробок. Чёрные поля скриншота обрезаются перед чтением. Обработка может занять несколько минут.

Скидка показывается отдельной суммой на строку; исходная цена сохраняется. Разница между количеством × ценой и напечатанной суммой — только предположение о скидке, его нужно отдельно подтвердить по документу. Прежний долг на корейской накладной не прибавляется к новой поставке. Если сумма строки не прочитана, расчёт помечается для проверки; расходящийся итог сохранить нельзя. Для незнакомого макета или обрезанного фото некоторые поля придётся исправить вручную. НДС переносится из документа; приложение не определяет применимые налоговые ставки.

Денежные суммы хранятся точно до двух знаков после запятой, без вычислений с float. Старые целые суммы при обновлении автоматически переводятся в новый формат без изменения их стоимости. Миграция выполняется один раз и атомарно.

Данные находятся в `supplier_payments.db`, фотографии и черновики — в `invoice_files/`. Для полной резервной копии сохраните и базу, и эту папку. Не публикуйте их в репозитории.

## Главная страница

Раздел «Обзор» открывается по адресу `/`. Поставки и оплаты показаны за выбранный месяц; текущий долг и открытые накладные — за всё время. Аванс одному поставщику не уменьшает долг другому. В таблице работают поиск по названию и фильтры по долгу. Управление поставщиками находится на `/suppliers`.

В списке поставщиков доступны «Редактировать» и «Удалить». Переименование сохраняет номер карточки и все связанные накладные и оплаты. Удаление требует отдельного подтверждения и разрешено только для карточки без поставок и оплат; это проверяется повторно при сохранении. Закрытые расчёты и авансы тоже считаются историей. Если страница редактирования долго открыта или сервер перезапущен, проверьте данные и повторите действие в обновлённой форме.

## Исправление ошибок

В истории оплат нажмите «Отменить запись», проверьте затронутые накладные и укажите причину. Оплата и её прежние привязки останутся в истории со статусом «Отменена», но перестанут уменьшать долг и участвовать в сводке. Банковский перевод эта кнопка не отменяет. Повторная отмена той же записи не меняет расчёты. При необходимости можно записать новую правильную оплату.

В поставках или карточке накладной нажмите «Редактировать». Для накладных с товарными строками доступны номер, дата, покупатель, товары, количество, цены, скидки и НДС; итог пересчитывается по товарам. Для старых поставок без строк доступны дата, общая сумма и примечание. Поставщик остаётся прежним. Сумму нельзя уменьшить ниже активных оплат, привязанных к накладной. Причина исправления обязательна, прежняя версия и фотография сохраняются. В карточке есть история с версиями «До исправления» и «После исправления».

Перед сохранением повторно проверяются связанные оплаты и версия записи. Устаревшая форма не перезаписывает более свежие данные. Изменение накладной и её истории сохраняется целиком либо отменяется целиком при ошибке.

Проверки запускаются командой `python -m unittest discover -v` и используют отдельные тестовые базы.


### Язык интерфейса / Interface language

Переключатель **RU / EN** находится в боковом меню. Русский используется по умолчанию.
Выбор хранится в браузере один год и сохраняется после перезапуска сайта; для другого браузера
или компьютера язык выбирается отдельно. При переключении сохраняются текущая страница,
выбранный поставщик и месяц. Сначала сохраните незавершённую форму: смена языка перезагружает страницу.

The **RU / EN** switch in the sidebar changes all application pages, form labels,
validation messages and OCR review hints. Supplier names, product names, notes, original
photos and ledger data are preserved. Native date pickers and file dialogs follow the browser language.
Displayed numbers use Russian or English separators; stored amounts stay in exact minor units.

Translations live in `translations/en.json`. Server templates and messages call
`i18n.translate`; dynamic browser messages use the same catalog through `uiTranslate`.
Cached OCR warnings remain in their original canonical form and are localized when displayed.
No external translation service, API key or extra package is required.

### Отчёт одного поставщика за месяц / Monthly supplier statement

В боковом меню откройте **Отчёты**, выберите поставщика и месяц, нажмите **Показать отчёт**.
Страница показывает начальный остаток, поставки и оплаты за выбранный календарный месяц,
а также конечный остаток. Кнопка **Скачать Excel** сохраняет эту сверку в `.xlsx`.
Положительный остаток означает долг магазина поставщику, отрицательный — аванс.

Opening balance includes all deliveries minus active payments before the month. Monthly payments
are included by payment date, even when allocated to older invoices; allocations never duplicate
the payment amount. Unallocated payments are included as advances. Cancelled payments are excluded.
The report uses a consistent read-only snapshot of current records, so later invoice corrections
and payment cancellations affect a newly generated historical report. It is not a frozen month-end archive.

The export follows the selected RU / EN interface language and keeps supplier names and notes unchanged.
Its single worksheet includes dated entries, running balances, payment allocations and cached Excel formulas.
The styled template is in `report_assets/statement-template.xlsx`; the app fills it using Python's standard
library and does not need another package or a Node runtime on the user's computer.
Excel's numeric precision is checked before export; values beyond its reliable limit remain available
on the site and are never silently rounded into the file. Downloading changes no ledger records.

