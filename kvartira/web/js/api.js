// Общие помощники для всех страниц: запросы к программе, сообщения, мелочи.

export async function request(method, url, body) {
    const options = { method, headers: {} };
    if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
    }

    let response;
    try {
        response = await fetch(url, options);
    } catch {
        // Сервер не отвечает — чаще всего окно программы просто закрыли.
        throw {
            error: 'Программа не отвечает.',
            hint: 'Проверьте, что окно запуска ещё открыто, и обновите страницу.',
        };
    }

    if (response.status === 204) return null;

    let data = null;
    try {
        data = await response.json();
    } catch {
        data = null;
    }

    if (!response.ok) {
        throw data && data.error
            ? data
            : { error: 'Не получилось выполнить действие.', hint: 'Попробуйте ещё раз.' };
    }
    return data;
}

export const api = {
    get: (url) => request('GET', url),
    post: (url, body) => request('POST', url, body),
    patch: (url, body) => request('PATCH', url, body),
    del: (url) => request('DELETE', url),
};

// ── Всплывающие сообщения ────────────────────────────────────────────

function toastArea() {
    let area = document.querySelector('.toast-area');
    if (!area) {
        area = document.createElement('div');
        area.className = 'toast-area';
        document.body.appendChild(area);
    }
    return area;
}

export function toast(message, hint = '', kind = '') {
    const node = document.createElement('div');
    node.className = 'toast' + (kind ? ` toast-${kind}` : '');
    const title = document.createElement('strong');
    title.textContent = message;
    node.appendChild(title);
    if (hint) {
        const line = document.createElement('div');
        line.className = 'hint';
        line.textContent = hint;
        node.appendChild(line);
    }
    toastArea().appendChild(node);
    setTimeout(() => node.remove(), kind === 'error' ? 8000 : 4500);
}

export function showError(err) {
    const message = (err && err.error) || 'Что-то пошло не так.';
    const hint = (err && err.hint) || '';
    toast(message, hint, 'error');
    console.error(err);
}

// ── Мелкие помощники ─────────────────────────────────────────────────

export function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key === 'html') node.innerHTML = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
        if (child) node.appendChild(child);
    }
    return node;
}

export function plural(n, one, few, many) {
    const a = Math.abs(n) % 100;
    const b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
}

export function formatDate(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (isNaN(date)) return '';
    return date.toLocaleDateString('ru-RU', {
        day: 'numeric', month: 'long', year: 'numeric',
    });
}

export function formatMm(mm) {
    if (!mm && mm !== 0) return '—';
    return (mm / 1000).toFixed(2).replace('.', ',') + ' м';
}

export function formatArea(m2) {
    if (!m2 && m2 !== 0) return '—';
    return Number(m2).toFixed(2).replace('.', ',') + ' м²';
}
