// Страница «Планировка»: загрузка файлов дизайн-проекта и их просмотр.

import { api, el, showError, toast, plural, formatDate } from './api.js';

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
        ? 'Среди загруженного есть векторный чертёж — значит, размеры программа сможет '
        + 'прочитать точно. Следующий шаг — «Разобрать план»: программа найдёт комнаты, '
        + 'стены, окна и мебель. Этот шаг появится в ближайшем обновлении.'
        : 'Загружены только изображения. Программа сможет распознать по ним планировку, '
        + 'но размеры нужно будет подтвердить вручную. Если у дизайнера есть PDF — '
        + 'загрузите лучше его.';
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
