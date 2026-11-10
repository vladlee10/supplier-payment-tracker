"""Exact money arithmetic: database values are hundredths of a won."""
from i18n import translate as _, get_language
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

MAX_MONEY = 2**63 - 1


def decimal_value(value):
    text = str(value).strip().replace('\u00a0', '').replace('\u202f', '').replace(' ', '')
    if ',' in text and '.' in text:
        raise ValueError(_('Используйте один десятичный разделитель: запятую или точку.'))
    text = text.replace(',', '.')
    if len(text) > 30 or not re.fullmatch(r'\d+(?:\.\d+)?', text):
        raise ValueError(_('Укажите положительное число.'))
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(_('Некорректное число.')) from exc
    if not result.is_finite():
        raise ValueError(_('Некорректное число.'))
    return result


def parse_money(value, *, allow_zero=False):
    number = decimal_value(value)
    if number.as_tuple().exponent < -2:
        raise ValueError(_('В сумме допускается не более двух знаков после запятой.'))
    result = int(number * 100)
    if not (0 if allow_zero else 1) <= result <= MAX_MONEY:
        raise ValueError(_('Укажите корректную сумму.'))
    return result


def money_text(minor):
    whole, fraction = divmod(abs(int(minor)), 100)
    value = f'{whole:,}' if get_language() == 'en' else f'{whole:,}'.replace(',', ' ')
    if fraction:
        value += ('.' if get_language() == 'en' else ',') + f'{fraction:02d}'
    return ('−' if int(minor) < 0 else '') + value


def money_input(minor):
    return f'{Decimal(int(minor)) / 100:.2f}'


def line_amount(quantity, unit_price_minor):
    return int((quantity * unit_price_minor).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
