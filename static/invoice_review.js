const rows = document.getElementById('invoice-rows');
const reviewForm = document.getElementById('review-form');
const editingInvoice = reviewForm.dataset.editing === 'true';
if (editingInvoice) document.getElementById('printed_total').readOnly = true;

function scaledNumber(value, digits) {
    const cleaned = value.replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');
    const match = cleaned.match(/^(\d+)(?:\.(\d+))?$/);
    if (!match || (match[2] || '').length > digits) throw new Error(uiTranslate('Проверьте числа в таблице.'));
    return BigInt(match[1]) * 10n ** BigInt(digits) + BigInt((match[2] || '').padEnd(digits, '0'));
}
function inputMoney(value) {
    return `${value / 100n}.${(value % 100n).toString().padStart(2, '0')}`;
}
function displayMoney(value) {
    const english = document.documentElement.lang === 'en';
    return `${(value / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, english ? ',' : ' ')}${english ? '.' : ','}${(value % 100n).toString().padStart(2, '0')}`;
}
function updateCheck(event) {
    const tr = event?.target.closest('tr');
    if (tr && ['item_quantity', 'item_unit_price', 'item_discount'].includes(event.target.name)) {
        try {
            const quantity = scaledNumber(tr.querySelector('[name=item_quantity]').value, 3);
            const price = scaledNumber(tr.querySelector('[name=item_unit_price]').value, 2);
            const discount = scaledNumber(tr.querySelector('[name=item_discount]').value || '0', 2);
            const amount = (quantity * price + 500n) / 1000n - discount;
            if (amount < 0n) throw new Error(uiTranslate('Скидка превышает стоимость товара.'));
            tr.querySelector('[name=item_line_total]').value = inputMoney(amount);
        } catch (_) { /* Incomplete edits are validated below and on the server. */ }
    }
    const hasDiscount = [...rows.querySelectorAll('[name=item_discount]')].some(input => {
        try { return scaledNumber(input.value || '0', 2) > 0n; } catch (_) { return true; }
    });
    document.getElementById('discounts-check').hidden = !hasDiscount;
    reviewForm.elements.discounts_reviewed.required = hasDiscount;
    const status = document.getElementById('invoice-check');
    try {
        let total = 0n;
        for (const row of rows.children) {
            const get = name => row.querySelector(`[name=item_${name}]`).value;
            const amount = scaledNumber(get('line_total'), 2);
            const expected = (scaledNumber(get('quantity'), 3) * scaledNumber(get('unit_price'), 2) + 500n) / 1000n - scaledNumber(get('discount') || '0', 2);
            if (amount !== expected) throw new Error(uiTranslate('Количество × цена − скидка отличается от суммы строки.'));
            total += amount;
        }
        if (document.getElementById('vat_mode').value === 'excluded') total += scaledNumber(document.getElementById('vat_total').value, 2);
        if (editingInvoice) document.getElementById('printed_total').value = inputMoney(total);
        const printed = scaledNumber(document.getElementById('printed_total').value, 2);
        const same = total === printed;
        const result = editingInvoice ? uiTranslate('Итог пересчитан по текущим товарам.') : same ? uiTranslate('Итог совпадает с документом.') : uiTranslate('Итог отличается от документа — проверьте строки.');
        status.textContent = uiTranslate('Строк: {count}. Итого по товарам{vat}: {total} ₩. {result}', {
            count: rows.children.length,
            vat: document.getElementById('vat_mode').value === 'excluded' ? uiTranslate(' с НДС') : '',
            total: displayMoney(total), result
        });
        status.classList.toggle('sum-mismatch', !same);
    } catch (error) {
        status.textContent = uiTranslate('Строк: {count}. {error} Заполните суммы и итог накладной для сверки.', {count: rows.children.length, error: error.message});
        status.classList.add('sum-mismatch');
    }
}
document.getElementById('add-row').addEventListener('click', () => {
    if (rows.children.length >= 250) return;
    const row = document.createElement('tr');
    const fields = [['name', uiTranslate('Название товара')], ['quantity', uiTranslate('Количество')], ['unit', uiTranslate('Единица измерения')], ['unit_price', uiTranslate('Цена за единицу')], ['discount', uiTranslate('Скидка на строку')], ['vat_rate', uiTranslate('Ставка НДС')], ['vat_amount', uiTranslate('Сумма НДС')], ['line_total', uiTranslate('Сумма строки')]];
    for (const [key, label] of fields) {
        const td = document.createElement('td');
        const input = document.createElement('input');
        input.name = `item_${key}`;
        if (key === 'discount') input.value = '0.00';
        input.setAttribute('aria-label', label);
        input.required = ['name', 'quantity', 'unit_price', 'line_total'].includes(key);
        if (!['name', 'unit'].includes(key)) input.inputMode = 'decimal';
        td.append(input); row.append(td);
    }
    const td = document.createElement('td');
    const remove = document.createElement('button');
    remove.type = 'button'; remove.textContent = '×'; remove.className = 'remove-row secondary-button'; remove.setAttribute('aria-label', uiTranslate('Удалить товарную строку'));
    td.append(remove); row.append(td); rows.append(row); updateCheck();
});
rows.addEventListener('click', event => {
    if (event.target.closest('.remove-row')) { event.target.closest('tr').remove(); updateCheck(); }
});
reviewForm.addEventListener('input', updateCheck);
reviewForm.addEventListener('change', updateCheck);
reviewForm.addEventListener('submit', () => { document.getElementById('save-invoice').disabled = true; });
updateCheck();
