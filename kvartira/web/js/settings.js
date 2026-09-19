// Страница «Настройки»: что подключено, а что ещё нет.

import { api, el } from './api.js';

const content = document.getElementById('content');

function serviceCard(service, explain) {
    const badge = service.ready
        ? el('span', { class: 'badge badge-ok', text: 'подключено' })
        : el('span', { class: 'badge badge-warn', text: 'не настроено' });

    return el('div', { class: 'card' }, [
        el('div', { class: 'row', style: 'justify-content:space-between' }, [
            el('h3', { text: service.title }),
            badge,
        ]),
        el('div', { class: 'muted small', text: explain }),
        el('div', { class: 'muted small', style: 'margin-top:8px', text: 'Модель: ' + service.model }),
    ]);
}

async function load() {
    try {
        const status = await api.get('/api/status');
        const both = status.services.plan.ready && status.services.image.ready;

        const blocks = [
            el('div', { class: 'cards cards-2' }, [
                serviceCard(status.services.plan,
                    'Читает ваш PDF или фото плана и находит комнаты, стены, окна и мебель.'),
                serviceCard(status.services.image,
                    'Превращает кадр из 3D-модели в фотореалистичную картинку комнаты.'),
            ]),
        ];

        if (!both) {
            blocks.push(el('div', { class: 'notice notice-warn', style: 'margin-top:20px' }, [
                el('strong', { text: 'Пока подключено не всё — и это нормально' }),
                el('div', { class: 'small' }, [
                    document.createTextNode(
                        'Без ключей программа всё равно работает: можно создавать проекты, '
                        + 'строить 3D-модель и ходить по квартире. Ключи нужны только для '
                        + 'чтения чертежа и создания картинок. Как их вписать — в файле '
                    ),
                    el('code', { text: 'README.md' }),
                    document.createTextNode(', раздел «Ключи доступа».'),
                ]),
            ]));
        }

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
                el('div', { class: 'muted small', style: 'margin-top:12px',
                    text: 'Дойдёт до лимита — программа остановится и спросит вас. '
                        + 'Лимит меняется в файле .env, строка DAILY_LIMIT_USD.' }),
            ]),

            el('h2', { text: 'Значения по умолчанию' }),
            el('div', { class: 'card' }, [
                el('div', { class: 'stat-row' }, [
                    el('div', { class: 'stat' }, [
                        el('div', { class: 'k', text: 'Высота потолка' }),
                        el('div', { class: 'v', text: (status.ceiling_height_mm / 1000).toFixed(2).replace('.', ',') + ' м' }),
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
