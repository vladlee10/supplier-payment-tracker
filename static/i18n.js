(() => {
    const catalog = JSON.parse(document.getElementById('ui-translations').textContent);
    window.uiTranslate = (key, values = {}) => {
        const text = catalog[key] || key;
        return text.replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(values, name) ? String(values[name]) : match);
    };
})();
