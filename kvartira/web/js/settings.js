// Страница «Настройки»: что подключено, какие модели выбраны, проверка доступа.

import { api, el, showError, toast } from './api.js';

const content = document.getElementById('content');

function statusBadge(ready) {
    return ready
        ? el('span', { class: 'badge badge-ok', text: 'ключ вписан' })
        : el('span', { class: 'badge badge-warn', text: 'ключа нет' });
}

// ── Карточка сервиса ─────────────────────────────────────────────────

function serviceCard(service, explain, aiSettings, key) {
    const info = aiSettings[key];
    return el('div', { class: 'card' }, [
        el('div', { class: 'row', style: 'justify-content:space-between' }, [
            el('h3', { text: service.title }),
            statusBadge(info.ready),
        ]),
        el('div', { class: 'muted small', text: explain }),
        el('div', { class: 'muted small', style: 'margin-top:8px' }, [
            document.createTextNode('Модель: '),
            el('code', { text: info.model }),
        ]),
        el('div', { class: 'muted small' }, [
            document.createTextNode('Адрес сервиса: '),
            el('code', { text: info.base_url }),
        ]),
        el('div', { class: 'small', id: `result-${key}`, style: 'margin-top:10px' }),
    ]);
}

// ── Проверка доступа ─────────────────────────────────────────────────

function renderCheck(key, report) {
    const box = document.getElementById(`result-${key}`);
    if (!box) return;
    const cls = report.ok ? 'badge badge-ok' : 'badge badge-warn';
    box.replaceChildren(
        el('span', { class: cls, text: report.ok ? 'работает' : 'не работает' }),
        el('div', { style: 'margin-top:6px' }, [
            el('strong', { text: report.message }),
        ]),
        report.hint ? el('div', { class: 'muted', text: report.hint }) : null,
    );
}

async function runCheck(button) {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'Проверяю…';
    ['plan', 'image'].forEach((key) => {
        const box = document.getElementById(`result-${key}`);
        if (box) box.replaceChildren(el('span', { class: 'spinner' }));
    });
    try {
        const report = await api.post('/api/ai/check', {});
        renderCheck('plan', report.plan);
        renderCheck('image', report.image);
        if (report.plan.ok && report.image.ok) {
            toast('Всё подключено', 'Можно разбирать план и создавать визуализации', 'ok');
        }
    } catch (err) {
        showError(err);
        ['plan', 'image'].forEach((key) => {
            const box = document.getElementById(`result-${key}`);
            if (box) box.replaceChildren();
        });
    } finally {
        button.disabled = false;
        button.textContent = original;
    }
}

// ── Выбор моделей ────────────────────────────────────────────────────

function modelField(labelText, hintText, listId, value) {
    const input = el('input', {
        type: 'text', value: value || '', list: listId,
        placeholder: 'начните вводить название',
    });
    return {
        input,
        node: el('div', { class: 'field' }, [
            el('label', { text: labelText }),
            input,
            el('datalist', { id: listId }),
            el('div', { class: 'hint', text: hintText }),
        ]),
    };
}

function fillList(listId, models) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.replaceChildren(...models.map((m) => el('option', {
        value: m.id,
        label: m.vendor ? `${m.id} · ${m.vendor}` : m.id,
    })));
}

function modelsCard(aiSettings) {
    const plan = modelField(
        'Модель, которая читает чертёж',
        'Подойдёт Claude — он хорошо разбирает планы. Список подставится сам.',
        'list-text', aiSettings.plan.model,
    );
    const image = modelField(
        'Модель, которая рисует визуализации',
        'Нужна такая, что принимает картинку на вход, — например FLUX.2.',
        'list-image', aiSettings.image.model,
    );

    const save = el('button', { class: 'btn btn-primary', text: 'Сохранить' });
    const reload = el('button', { class: 'btn', text: 'Обновить список моделей' });
    const note = el('div', { class: 'muted small', style: 'margin-top:10px' });

    save.addEventListener('click', async () => {
        save.disabled = true;
        try {
            await api.patch('/api/ai/settings', {
                plan_model: plan.input.value.trim(),
                image_model: image.input.value.trim(),
            });
            toast('Модели сохранены', '', 'ok');
        } catch (err) {
            showError(err);
        } finally {
            save.disabled = false;
        }
    });

    async function loadModels() {
        note.replaceChildren(el('span', { class: 'spinner' }));
        try {
            const catalogue = await api.get('/api/ai/models');
            fillList('list-text', catalogue.text);
            fillList('list-image', catalogue.image);
            note.textContent =
                `Сервис отдал ${catalogue.text.length} текстовых моделей `
                + `и ${catalogue.image.length} рисующих. `
                + 'Щёлкните по полю, чтобы выбрать из списка.';
        } catch (err) {
            note.replaceChildren(
                el('strong', { text: (err && err.error) || 'Список моделей не получен.' }),
                el('div', { class: 'muted', text: (err && err.hint) || '' }),
            );
        }
    }

    reload.addEventListener('click', loadModels);

    const card = el('div', { class: 'card' }, [
        el('h3', { text: 'Какие модели использовать' }),
        el('p', {
            class: 'muted small',
            text: 'Названия берутся прямо из каталога вашего сервиса — '
                + 'переписывать их вручную не нужно.',
        }),
        plan.node,
        image.node,
        el('div', { class: 'row' }, [save, reload]),
        note,
    ]);

    // Список подгружаем сразу, если ключ уже вписан.
    if (aiSettings.plan.ready) setTimeout(loadModels, 0);

    return card;
}

// ── Сборка страницы ──────────────────────────────────────────────────

async function load() {
    try {
        const [status, aiSettings] = await Promise.all([
            api.get('/api/status'),
            api.get('/api/ai/settings'),
        ]);

        const check = el('button', { class: 'btn btn-primary', text: 'Проверить доступ' });
        check.addEventListener('click', () => runCheck(check));

        const blocks = [
            el('div', { class: 'cards cards-2' }, [
                serviceCard(status.services.plan,
                    'Читает ваш PDF или фото плана и находит комнаты, стены, окна и мебель.',
                    aiSettings, 'plan'),
                serviceCard(status.services.image,
                    'Превращает кадр из 3D-модели в фотореалистичную картинку комнаты.',
                    aiSettings, 'image'),
            ]),
            el('div', { class: 'row', style: 'margin-top:16px' }, [
                check,
                el('span', {
                    class: 'muted small',
                    text: 'Проверка почти бесплатна — уходит меньше копейки.',
                }),
            ]),
        ];

        if (!aiSettings.plan.ready || !aiSettings.image.ready) {
            blocks.push(el('div', { class: 'notice notice-warn', style: 'margin-top:20px' }, [
                el('strong', { text: 'Как вписать ключ' }),
                el('div', { class: 'small' }, [
                    document.createTextNode('Откройте файл '),
                    el('code', { text: '.env' }),
                    document.createTextNode(' рядом с программой Блокнотом или TextEdit, '),
                    document.createTextNode('впишите ключ после знака = в строках '),
                    el('code', { text: 'PLAN_API_KEY' }),
                    document.createTextNode(' и '),
                    el('code', { text: 'IMAGE_API_KEY' }),
                    document.createTextNode(' — если сервис один, ключ в обеих строках '),
                    document.createTextNode('одинаковый. Сохраните файл и перезапустите программу.'),
                ]),
            ]));
        }

        blocks.push(el('h2', { text: 'Модели' }), modelsCard(aiSettings));

        blocks.push(
            el('h2', { text: 'Расходы' }),
            el('div', { class: 'card' }, [
                el('div', { class: 'stat-row' }, [
                    el('div', { class: 'stat' }, [
                        el('div', { class: 'k', text: 'Дневной лимит' }),
                        el('div', { class: 'v', text: '$' + status.daily_limit_usd }),
                    ]),
                    el('div', { class: 'stat' }, [
                        el('div', { class: 'k', text: 'Потрачено сегодня' }),
                        el('div', { class: 'v', text: '$0,00' }),
                    ]),
                ]),
                el('div', {
                    class: 'muted small', style: 'margin-top:12px',
                    text: 'Дойдёт до лимита — программа остановится и спросит вас. '
                        + 'Лимит меняется в файле .env, строка DAILY_LIMIT_USD.',
                }),
            ]),

            el('h2', { text: 'Значения по умолчанию' }),
            el('div', { class: 'card' }, [
                el('div', { class: 'stat-row' }, [
                    el('div', { class: 'stat' }, [
                        el('div', { class: 'k', text: 'Высота потолка' }),
                        el('div', {
                            class: 'v',
                            text: (status.ceiling_height_mm / 1000).toFixed(2).replace('.', ',') + ' м',
                        }),
                    ]),
                    el('div', { class: 'stat' }, [
                        el('div', { class: 'k', text: 'Версия программы' }),
                        el('div', { class: 'v', text: status.version }),
                    ]),
                ]),
            ]),
        );

        content.replaceChildren(...blocks);
    } catch (err) {
        content.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не удалось прочитать настройки.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
    }
}

load();
