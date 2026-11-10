const search = document.getElementById('supplier-search');
const supplierRows = [...document.querySelectorAll('[data-supplier-row]')];
const filterButtons = [...document.querySelectorAll('[data-supplier-filter]')];
const emptyResult = document.getElementById('no-supplier-results');
const visibleCount = document.getElementById('supplier-visible-count');
let activeFilter = 'all';

function filterSuppliers() {
    const query = search.value.trim().toLocaleLowerCase(document.documentElement.lang);
    let visible = 0;
    for (const row of supplierRows) {
        const debt = row.dataset.hasDebt === 'true';
        const matches = row.dataset.name.includes(query) && (activeFilter === 'all' || (activeFilter === 'debt' ? debt : !debt));
        row.hidden = !matches;
        if (matches) visible++;
    }
    emptyResult.hidden = supplierRows.length === 0 || visible > 0;
    visibleCount.textContent = query || activeFilter !== 'all' ? uiTranslate('Найдено: {count} из {total}', {count: visible, total: supplierRows.length}) : uiTranslate('Всего поставщиков: {count}', {count: supplierRows.length});
}
for (const button of filterButtons) {
    button.addEventListener('click', () => {
        activeFilter = button.dataset.supplierFilter;
        for (const item of filterButtons) {
            const selected = item === button;
            item.classList.toggle('is-selected', selected);
            item.setAttribute('aria-pressed', selected.toString());
        }
        filterSuppliers();
    });
}
search.addEventListener('input', filterSuppliers);
document.getElementById('overview-month').addEventListener('change', event => {
    if (event.target.value && event.target.checkValidity()) event.target.form.requestSubmit();
});
document.querySelector('.month-form').classList.add('month-form-ready');
