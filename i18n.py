"""Russian/English UI strings; ledger data and OCR drafts keep their original text."""
import json
from pathlib import Path
import re
from string import Formatter
from urllib.parse import unquote, urlsplit

from flask import has_request_context, redirect, render_template, request, url_for


ENGLISH = json.loads((Path(__file__).parent / 'translations' / 'en.json').read_text(encoding='utf-8'))
LANGUAGES = ('ru', 'en')
CLIENT_KEYS = (' с НДС', 'Всего поставщиков: {count}', 'Единица измерения', 'Итог отличается от документа — проверьте строки.', 'Итог пересчитан по текущим товарам.', 'Итог совпадает с документом.', 'Количество', 'Количество × цена − скидка отличается от суммы строки.', 'Название товара', 'Найдено: {count} из {total}', 'Проверьте числа в таблице.', 'Скидка на строку', 'Скидка превышает стоимость товара.', 'Ставка НДС', 'Строк: {count}. {error} Заполните суммы и итог накладной для сверки.', 'Строк: {count}. Итого по товарам{vat}: {total} ₩. {result}', 'Сумма НДС', 'Сумма строки', 'Удалить товарную строку', 'Цена за единицу')


def get_language():
    if has_request_context():
        value = request.cookies.get('ui_language', 'ru')
        if value in LANGUAGES:
            return value
    return 'ru'


def translate(message, **values):
    text = ENGLISH.get(message, message) if get_language() == 'en' else message
    return text.format(**values) if values else text


# Previously saved OCR drafts contain canonical Russian warnings. Match only
# known application messages; supplier names, item names and notes are not passed here.
_warning_patterns = []
for source, target in ENGLISH.items():
    if '{' not in source or '{{' in source:
        continue
    pattern = ''
    fields = []
    for literal, name, _, _ in Formatter().parse(source):
        pattern += re.escape(literal)
        if name is not None:
            pattern += '(.*?)'
            fields.append(name)
    _warning_patterns.append((re.compile(pattern, re.DOTALL), fields, target))


def localize_message(message):
    if get_language() != 'en':
        return message
    if message in ENGLISH:
        return ENGLISH[message]
    for pattern, fields, target in _warning_patterns:
        match = pattern.fullmatch(message)
        if match:
            return target.format(**dict(zip(fields, match.groups())))
    return message


def safe_return_path(value):
    # Accept local relative paths only, including after percent decoding.
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    try:
        parts = urlsplit(decoded)
    except ValueError:
        return url_for('index')
    if (not decoded.startswith('/') or decoded.startswith('//') or '\\' in decoded
            or parts.scheme or parts.netloc or any(ord(c) < 32 for c in decoded)):
        return url_for('index')
    return value


def register_i18n(app):
    app.jinja_env.globals.update(_=translate)
    app.jinja_env.filters['localize_message'] = localize_message

    @app.context_processor
    def language_context():
        return dict(language=get_language(), js_translations={key: ENGLISH[key] for key in CLIENT_KEYS} if get_language() == 'en' else {})

    @app.post('/language')
    def change_language():
        language = request.form.get('language', '')
        if language not in LANGUAGES:
            return render_template('language_error.html'), 400
        response = redirect(safe_return_path(request.form.get('next', '/')), code=303)
        response.set_cookie('ui_language', language, max_age=365 * 24 * 60 * 60,
                            httponly=True, secure=request.is_secure, samesite='Lax')
        return response

    @app.after_request
    def language_headers(response):
        if response.mimetype == 'text/html':
            response.headers['Content-Language'] = get_language()
            response.vary.add('Cookie')
        return response

