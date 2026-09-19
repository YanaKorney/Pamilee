// Страница «Планировка»: загрузка файлов дизайн-проекта и их просмотр.

import { api, el, showError, toast, plural, formatDate, formatArea } from './api.js';

const projectId = Number(window.location.pathname.split('/')[2]);
const docsBox = document.getElementById('docs');
const dropzone = document.getElementById('dropzone');
const picker = document.getElementById('picker');
const progress = document.getElementById('progress');
const progressText = document.getElementById('progress-text');
const lightbox = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightbox-img');

// ── Просмотр страницы во весь экран ──────────────────────────────────

function openLightbox(src) {
    lightboxImg.src = src;
    lightbox.hidden = false;
}
function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.src = '';
}
lightbox.addEventListener('click', closeLightbox);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLightbox(); });

// ── Отрисовка ────────────────────────────────────────────────────────

function pageCard(page, documentName) {
    const thumb = el('img', {
        class: 'thumb',
        src: `/api/files/${page.id}/preview`,
        alt: `${documentName}, страница ${page.page_no}`,
        loading: 'lazy',
    });
    thumb.addEventListener('click', () => openLightbox(`/api/files/${page.id}/preview`));

    const label = el('input', {
        type: 'text',
        value: page.label || '',
        placeholder: 'Что на листе?',
        title: 'Например: план мебели, развертка стен, план полов',
    });
    let timer = null;
    label.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
            try {
                await api.patch(`/api/files/${page.id}/label`, { label: label.value });
            } catch (err) {
                showError(err);
            }
        }, 600);
    });

    return el('div', { class: 'page-card' }, [
        thumb,
        el('div', { class: 'meta' }, [
            el('div', { class: 'no', text: `Страница ${page.page_no}` }),
            label,
        ]),
    ]);
}

// ── Разбор листа ─────────────────────────────────────────────────────

function roomRow(room) {
    const parts = [el('strong', { text: room.name })];
    parts.push(el('span', { class: 'muted', text: ` — ${formatArea(room.area_m2)}` }));
    if (room.declared_area_m2) {
        const off = Math.abs(room.deviation_percent || 0);
        parts.push(el('span', {
            class: off <= 5 ? 'badge badge-ok' : 'badge badge-warn',
            style: 'margin-left:8px',
            text: off <= 5
                ? 'совпало'
                : `на чертеже ${formatArea(room.declared_area_m2)}`,
        }));
    }
    return el('div', { class: 'small', style: 'margin-bottom:4px' }, parts);
}

function analysisResult(data) {
    const blocks = [el('strong', { text: 'Что распознано на листе' })];

    if (data.warnings && data.warnings.length) {
        data.warnings.forEach((w) => blocks.push(
            el('div', { class: 'small', style: 'color:var(--warn)', text: '⚠ ' + w })
        ));
    }

    blocks.push(el('div', { class: 'small', style: 'margin:8px 0 6px' }, [
        document.createTextNode(
            `Помещений: ${data.rooms.length} · предметов: ${data.items.length} · `
            + `масштаб ${data.scale_source}`),
    ]));

    data.rooms.forEach((r) => blocks.push(roomRow(r)));

    const sum = el('div', { class: 'small', style: 'margin-top:8px' }, [
        document.createTextNode('Площадь квартиры: '),
        el('strong', { text: formatArea(data.total_area_m2) }),
    ]);
    if (data.declared_total_m2) {
        const off = Math.abs(data.total_area_m2 - data.declared_total_m2)
            / data.declared_total_m2 * 100;
        sum.appendChild(document.createTextNode(
            ` · по документам ${formatArea(data.declared_total_m2)} `));
        sum.appendChild(el('span', {
            class: off <= 2 ? 'badge badge-ok' : 'badge badge-warn',
            text: off <= 2 ? 'сходится' : `расхождение ${off.toFixed(1).replace('.', ',')} %`,
        }));
    }
    blocks.push(sum);

    if (data.outside_area_m2 > 0) {
        blocks.push(el('div', { class: 'muted small' }, [
            document.createTextNode(
                `Балкон или лоджия: ${formatArea(data.outside_area_m2)} — в общую площадь не входит.`),
        ]));
    }

    if (data.notes) {
        blocks.push(el('div', { class: 'muted small', style: 'margin-top:8px',
            text: 'Замечания модели: ' + data.notes }));
    }

    blocks.push(el('div', { class: 'muted small', style: 'margin-top:10px',
        text: `Потрачено на разбор: ${String(data.cost_rub).replace('.', ',')} ₽ · `
            + `сегодня всего ${String(data.spent_today_rub).replace('.', ',')} ₽` }));

    return el('div', { class: 'notice', style: 'margin-top:14px' }, blocks);
}

function analyseButton(pageId, host) {
    const button = el('button', { class: 'btn btn-primary', text: 'Разобрать план' });
    button.addEventListener('click', async () => {
        if (!confirm(
            'Программа отправит этот лист в AI-сервис и найдёт на нём комнаты, '
            + 'двери, окна и мебель.\n\nОбычно это стоит 40–60 рублей. Продолжить?'
        )) return;
        button.disabled = true;
        const original = button.textContent;
        button.textContent = 'Разбираю… это займёт до минуты';
        try {
            const data = await api.post(
                `/api/projects/${projectId}/files/${pageId}/analyse`, {});
            host.replaceChildren(analysisResult(data));
            toast('План разобран', `Найдено помещений: ${data.rooms.length}`, 'ok');
        } catch (err) {
            showError(err);
        } finally {
            button.disabled = false;
            button.textContent = original;
        }
    });
    return button;
}

function metre(mm) {
    return (mm / 1000).toFixed(2).replace('.', ',') + ' м';
}

// Карточка «что прочитано из чертежа» — появляется под векторным PDF.
function geometryCard(pageId) {
    const box = el('div', { class: 'notice', style: 'margin-top:14px' }, [
        el('span', { class: 'spinner' }),
    ]);

    api.get(`/api/files/${pageId}/geometry`).then((g) => {
        if (!g.is_vector || !g.scale_is_reliable) {
            box.replaceChildren(
                el('strong', { text: 'Размеры из этого листа прочитать не удалось' }),
                el('div', {
                    class: 'small muted',
                    text: 'Ничего страшного: масштаб можно будет задать вручную, '
                        + 'указав длину одной стены.',
                }),
            );
            return;
        }

        const rows = [];
        rows.push(el('div', { class: 'small' }, [
            document.createTextNode('Масштаб определён по '),
            el('strong', { text: `${g.dimensions_matched} размерным линиям` }),
            document.createTextNode(`, расхождение не больше ${String(g.scale_worst_error_mm).replace('.', ',')} мм.`),
        ]));

        if (g.total_area_m2) {
            rows.push(el('div', { class: 'small' }, [
                document.createTextNode('Общая площадь по чертежу: '),
                el('strong', { text: formatArea(g.total_area_m2) }),
                document.createTextNode('.'),
            ]));
        }

        if (g.room_areas && g.room_areas.length) {
            const list = g.room_areas
                .slice().sort((a, b) => b - a)
                .map((v) => formatArea(v)).join(' · ');
            rows.push(el('div', { class: 'small' }, [
                document.createTextNode(
                    `Помещений: ${g.room_areas.length} — `),
                el('span', { class: 'muted', text: list }),
            ]));
        }

        if (g.extra_areas && g.extra_areas.length) {
            rows.push(el('div', {
                class: 'small muted',
                text: 'Вне общей площади (балкон или лоджия): '
                    + g.extra_areas.map((v) => formatArea(v)).join(' · '),
            }));
        }

        if (g.declared_area_m2) {
            const diff = Math.abs(g.declared_area_m2 - (g.total_area_m2 || 0));
            rows.push(el('div', { class: 'small' }, [
                el('span', {
                    class: diff < 0.05 ? 'badge badge-ok' : 'badge badge-warn',
                    text: diff < 0.05 ? 'сходится с вашей площадью' : 'расходится с вашей площадью',
                }),
            ]));
        }

        box.replaceChildren(
            el('strong', { text: 'Что программа прочитала из чертежа' }),
            ...rows,
        );
    }).catch((err) => {
        box.replaceChildren(
            el('strong', { text: (err && err.error) || 'Не удалось разобрать чертёж.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        );
    });

    return box;
}

function documentCard(doc) {
    const badge = doc.is_vector
        ? el('span', {
            class: 'badge badge-ok',
            text: 'векторный чертёж',
            title: 'В таком файле хранятся настоящие линии стен и подписи размеров — '
                 + 'программа прочитает их точно, без распознавания картинки.',
        })
        : el('span', {
            class: 'badge badge-soon',
            text: 'изображение',
            title: 'В картинке есть только пиксели. Размеры придётся распознавать, '
                 + 'и вы сможете их поправить.',
        });

    const remove = el('button', { class: 'btn btn-quiet btn-danger', text: 'Удалить' });
    remove.addEventListener('click', async () => {
        const count = doc.page_count;
        const what = count > 1
            ? `все ${count} ${plural(count, 'страницу', 'страницы', 'страниц')}`
            : 'этот файл';
        if (!confirm(`Удалить «${doc.original_name}»?\n\nУдалится ${what}.`)) return;
        try {
            await api.del(`/api/projects/${projectId}/documents/${encodeURIComponent(doc.stored_name)}`);
            toast('Файл удалён', '', 'ok');
            load();
        } catch (err) {
            showError(err);
        }
    });

    const facts = [
        `${doc.page_count} ${plural(doc.page_count, 'страница', 'страницы', 'страниц')}`,
        formatDate(doc.created_at),
    ];

    return el('div', { class: 'doc' }, [
        el('div', { class: 'doc-head' }, [
            el('div', { class: 'grow' }, [
                el('div', { class: 'doc-name', text: doc.original_name }),
                el('div', { class: 'muted small', text: facts.join(' · ') }),
                el('div', { style: 'margin-top:7px' }, [badge]),
            ]),
            remove,
        ]),
        el('div', { class: 'pages' }, doc.pages.map((p) => pageCard(p, doc.original_name))),
        doc.is_vector ? geometryCard(doc.pages.find((p) => p.is_vector).id) : null,
        analyseBlock(doc),
    ]);
}

function analyseBlock(doc) {
    const host = el('div');
    const page = doc.pages[0];
    return el('div', { style: 'margin-top:14px' }, [
        el('div', { class: 'row' }, [
            analyseButton(page.id, host),
            el('span', {
                class: 'muted small',
                text: 'Найдёт комнаты, двери, окна и мебель. Результат можно будет поправить.',
            }),
        ]),
        host,
    ]);
}

function emptyState() {
    return el('div', { class: 'empty' }, [
        el('div', { class: 'icon', text: '📁' }),
        el('h3', { text: 'Пока ничего не загружено' }),
        el('p', {
            class: 'muted',
            text: 'Начните с общего плана квартиры и плана расстановки мебели. '
                + 'Если есть PDF от дизайнера — загружайте именно его: '
                + 'из PDF размеры читаются точнее, чем из фотографии.',
        }),
    ]);
}

function nextStepNote(documents) {
    const hasVector = documents.some((d) => d.is_vector);
    const lines = hasVector
        ? 'Среди загруженного есть векторный чертёж — значит, размеры программа '
        + 'прочитала точно. Теперь нажмите «Разобрать план» на нужном листе: '
        + 'программа найдёт комнаты, двери, окна и мебель, а площади сверит '
        + 'с теми, что уже прочитаны.'
        : 'Загружены только изображения. Программа сможет распознать по ним планировку, '
        + 'но масштаб будет определён по размерным подписям на картинке — это менее '
        + 'надёжно. Если у дизайнера есть PDF, загрузите лучше его.';
    return el('div', { class: hasVector ? 'notice' : 'notice notice-warn' }, [
        el('strong', { text: 'Что дальше' }),
        el('div', { class: 'small', text: lines }),
    ]);
}

async function load() {
    try {
        const [project, documents] = await Promise.all([
            api.get(`/api/projects/${projectId}`),
            api.get(`/api/projects/${projectId}/documents`),
        ]);

        const crumb = document.getElementById('crumb-project');
        crumb.textContent = project.name;
        crumb.href = `/project/${projectId}`;

        docsBox.replaceChildren();
        if (documents.length === 0) {
            docsBox.appendChild(emptyState());
            return;
        }
        docsBox.appendChild(el('h2', { text: 'Загруженные файлы' }));
        documents.forEach((d) => docsBox.appendChild(documentCard(d)));
        docsBox.appendChild(nextStepNote(documents));
    } catch (err) {
        docsBox.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не удалось открыть раздел.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
    }
}

// ── Загрузка ─────────────────────────────────────────────────────────

async function upload(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) return;

    progress.hidden = false;
    progressText.textContent = files.length === 1
        ? `Загружаю «${files[0].name}»…`
        : `Загружаю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}…`;

    const form = new FormData();
    files.forEach((f) => form.append('files', f));

    try {
        const response = await fetch(`/api/projects/${projectId}/files`, {
            method: 'POST',
            body: form,
        });
        const data = await response.json().catch(() => null);
        if (!response.ok) {
            throw data && data.error
                ? data
                : { error: 'Не удалось загрузить файлы.', hint: 'Попробуйте ещё раз.' };
        }

        const pages = data.added.reduce((sum, d) => sum + d.pages, 0);
        if (data.added.length > 0) {
            toast(
                `Загружено: ${data.added.length} ${plural(data.added.length, 'файл', 'файла', 'файлов')}`,
                `${pages} ${plural(pages, 'страница', 'страницы', 'страниц')} готово к разбору`,
                'ok'
            );
        }
        (data.problems || []).forEach((p) => showError({ error: `${p.name}: ${p.error}`, hint: p.hint }));
        load();
    } catch (err) {
        showError(err);
    } finally {
        progress.hidden = true;
        picker.value = '';
    }
}

picker.addEventListener('change', () => upload(picker.files));

['dragenter', 'dragover'].forEach((name) => {
    dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        dropzone.classList.add('over');
    });
});
['dragleave', 'drop'].forEach((name) => {
    dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        dropzone.classList.remove('over');
    });
});
dropzone.addEventListener('drop', (e) => upload(e.dataTransfer.files));

load();
