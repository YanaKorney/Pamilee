// Страница одного проекта: главное меню квартиры и её настройки.

import { api, el, showError, toast, formatArea } from './api.js';

const projectId = Number(window.location.pathname.split('/').pop());
const content = document.getElementById('content');
const crumb = document.getElementById('crumb');

// Разделы из главного меню. Появляются по мере готовности этапов.
const SECTIONS = [
    {
        key: 'plan', icon: '📄', title: 'Планировка',
        text: 'Загрузите PDF или фото плана — программа найдёт комнаты, стены, окна и мебель.',
        href: (id) => `/project/${id}/plan`, ready: true,
    },
    {
        key: 'rooms', icon: '🚪', title: 'Комнаты',
        text: 'Проверьте и поправьте то, что распознала программа. Здесь же — размеры и отделка.',
        href: (id) => `/project/${id}/rooms`, ready: false,
    },
    {
        key: 'refs', icon: '🖼️', title: 'Референсы',
        text: 'Фотографии интерьеров, мебели и материалов, которые вам нравятся, — по комнатам.',
        href: (id) => `/project/${id}/references`, ready: false,
    },
    {
        key: 'model', icon: '🧱', title: '3D-модель',
        text: 'Объёмная модель квартиры, построенная по вашему плану. Можно крутить и рассматривать.',
        href: (id) => `/project/${id}/viewer`, ready: false,
    },
    {
        key: 'renders', icon: '✨', title: 'Визуализации',
        text: 'Фотореалистичные картинки комнат с нескольких ракурсов. С версиями и сравнением.',
        href: (id) => `/project/${id}/renders`, ready: false,
    },
    {
        key: 'tour', icon: '🚶', title: '3D-тур',
        text: 'Прогулка по квартире от первого лица: WASD и мышь на компьютере, касания на телефоне.',
        href: (id) => `/project/${id}/tour`, ready: false,
    },
];

function metres(mm) {
    return (mm / 1000).toFixed(2).replace('.', ',') + ' м';
}

function sectionTile(section, project) {
    const children = [
        el('div', { class: 'tile-icon', text: section.icon }),
        el('h3', { text: section.title }),
        el('div', { class: 'muted small', text: section.text }),
    ];
    if (!section.ready) {
        children.push(el('div', { style: 'margin-top:10px' }, [
            el('span', { class: 'badge badge-soon', text: 'скоро' }),
        ]));
        return el('div', { class: 'card tile tile-off' }, children);
    }
    return el('a', { class: 'card tile', href: section.href(project.id) }, children);
}

function facts(project) {
    const items = [
        ['Высота потолка', metres(project.ceiling_height_mm)],
        ['Общая площадь', project.declared_area_m2 ? formatArea(project.declared_area_m2) : 'не указана'],
        ['Листов плана', String(project.plan_pages ?? 0)],
        ['Комнат найдено', String(project.room_count ?? 0)],
        ['Файлов на диске', String(project.disk_mb ?? 0).replace('.', ',') + ' МБ'],
    ];
    return el('div', { class: 'stat-row' }, items.map(([k, v]) =>
        el('div', { class: 'stat' }, [
            el('div', { class: 'k', text: k }),
            el('div', { class: 'v', text: v }),
        ])
    ));
}

function settingsCard(project) {
    const name = el('input', { type: 'text', value: project.name, maxlength: '120' });
    const height = el('input', {
        type: 'number', value: String(project.ceiling_height_mm),
        min: '1500', max: '6000', step: '10',
    });
    const area = el('input', {
        type: 'number', step: '0.01', min: '0', max: '10000',
        value: project.declared_area_m2 ?? '',
        placeholder: 'например 74,37',
    });
    const save = el('button', { class: 'btn btn-primary', text: 'Сохранить' });

    save.addEventListener('click', async () => {
        save.disabled = true;
        try {
            const updated = await api.patch(`/api/projects/${project.id}`, {
                name: name.value.trim() || 'Моя квартира',
                ceiling_height_mm: Number(height.value) || project.ceiling_height_mm,
                declared_area_m2: area.value ? Number(String(area.value).replace(',', '.')) : null,
            });
            toast('Сохранено', '', 'ok');
            crumb.textContent = updated.name;
            document.title = updated.name + ' · Моя квартира';
        } catch (err) {
            showError(err);
        } finally {
            save.disabled = false;
        }
    });

    return el('div', { class: 'card' }, [
        el('h3', { text: 'Параметры квартиры' }),
        el('p', { class: 'muted small', text: 'Их можно менять в любой момент — на уже созданные картинки это не повлияет.' }),
        el('div', { class: 'field' }, [el('label', { text: 'Название' }), name]),
        el('div', { class: 'field-pair' }, [
            el('div', { class: 'field' }, [
                el('label', { text: 'Высота потолка, мм' }), height,
                el('div', { class: 'hint', text: 'Влияет на 3D-модель и ощущение объёма.' }),
            ]),
            el('div', { class: 'field' }, [
                el('label', { text: 'Общая площадь, м²' }), area,
                el('div', { class: 'hint', text: 'Программа сверит с ней расчёт по плану.' }),
            ]),
        ]),
        el('div', { class: 'row' }, [save]),
    ]);
}

function startHere(project) {
    const done = (project.plan_files ?? 0) > 0;
    return el('div', { class: 'notice', style: 'margin-top:26px' }, [
        el('strong', { text: done ? 'План загружен' : 'С чего начать' }),
        el('div', { class: 'small', text: done
            ? 'Дизайн-проект загружен. Следующий шаг — разобрать его на комнаты; '
              + 'этот шаг появится в ближайшем обновлении программы.'
            : 'Первый шаг — загрузить дизайн-проект: план квартиры, план мебели, '
              + 'развертки стен. Подойдут PDF и фотографии.' }),
        el('div', { style: 'margin-top:12px' }, [
            el('a', {
                class: done ? 'btn' : 'btn btn-primary',
                href: `/project/${project.id}/plan`,
                text: done ? 'Открыть планировку' : 'Загрузить дизайн-проект',
            }),
        ]),
    ]);
}

async function load() {
    try {
        const project = await api.get(`/api/projects/${projectId}`);
        crumb.textContent = project.name;
        document.title = project.name + ' · Моя квартира';

        content.replaceChildren(
            el('h1', { text: project.name }),
            facts(project),

            startHere(project),

            el('h2', { text: 'Разделы' }),
            el('div', { class: 'cards cards-3' }, SECTIONS.map((s) => sectionTile(s, project))),

            el('h2', { text: 'Настройки проекта' }),
            settingsCard(project),
        );
    } catch (err) {
        content.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Проект не открылся.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
            el('div', { style: 'margin-top:12px' }, [
                el('a', { class: 'btn', href: '/', text: 'Вернуться к списку' }),
            ]),
        ]));
    }
}

load();
