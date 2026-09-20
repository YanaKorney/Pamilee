// Страница «Как будет выглядеть»: комната плюс направление — и картинка.

import { api, el, showError, technicalNote, toast, formatArea } from './api.js';

const projectId = Number(window.location.pathname.split('/')[2]);
const workBox = document.getElementById('work');
const refsBox = document.getElementById('refs');
const dropzone = document.getElementById('dropzone');
const picker = document.getElementById('picker');
const galleryBox = document.getElementById('gallery');
const lightbox = document.getElementById('lightbox');
const lightboxImg = document.getElementById('lightbox-img');

let state = null;
let chosenRoom = null;
let chosenStyle = 'scandi';
let references = [];
let chosenRefs = new Set();

// ── Картинка во весь экран ───────────────────────────────────────────

lightbox.addEventListener('click', () => {
    lightbox.hidden = true;
    lightboxImg.src = '';
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') lightbox.click();
});

function openBig(src) {
    lightboxImg.src = src;
    lightbox.hidden = false;
}

// ── Выбор комнаты ────────────────────────────────────────────────────

function roomButton(room) {
    const button = el('button', {
        class: chosenRoom === room.id ? 'chip chip-on' : 'chip',
        text: `${room.name} · ${formatArea(room.area_m2)}`,
    });
    button.addEventListener('click', () => {
        chosenRoom = room.id;
        draw();
    });
    return button;
}

function styleButton(style) {
    const short = style.title.split('—')[0].trim();
    const button = el('button', {
        class: chosenStyle === style.id ? 'chip chip-on' : 'chip',
        text: short,
        title: style.title,
    });
    button.addEventListener('click', () => {
        chosenStyle = style.id;
        draw();
    });
    return button;
}

// ── Экран работы ─────────────────────────────────────────────────────

function draw() {
    const blocks = [];

    if (!state.ready) {
        blocks.push(el('div', { class: 'notice notice-warn' }, [
            el('strong', { text: 'Сервис рисования не настроен.' }),
            el('div', { class: 'small' }, [
                document.createTextNode('Откройте '),
                el('a', { href: '/settings', text: '«Настройки»' }),
                document.createTextNode(' и вставьте ключ доступа.'),
            ]),
        ]));
    }

    if (!state.rooms.length) {
        blocks.push(el('div', { class: 'notice notice-warn' }, [
            el('strong', { text: 'Комнаты ещё не распознаны.' }),
            el('div', { class: 'small' }, [
                document.createTextNode('Сначала загрузите чертёж и нажмите «Разобрать план» в разделе '),
                el('a', { href: `/project/${projectId}/plan`, text: '«Планировка»' }),
                document.createTextNode('.'),
            ]),
        ]));
        workBox.replaceChildren(...blocks);
        return;
    }

    blocks.push(el('h2', { text: 'Комната' }));
    blocks.push(el('div', { class: 'chips' }, state.rooms.map(roomButton)));

    const room = state.rooms.find((r) => r.id === chosenRoom);
    if (room) {
        blocks.push(el('div', { class: 'notice', style: 'margin-top:12px' }, [
            el('strong', { text: 'Что программа знает об этой комнате' }),
            el('div', { class: 'small', style: 'margin-top:4px', text: room.facts }),
            el('div', { class: 'muted small', style: 'margin-top:6px',
                text: 'Эти числа прочитаны с вашего чертежа — картинка будет '
                    + 'рисоваться по ним.' }),
        ]));
    }

    blocks.push(el('h2', { style: 'margin-top:22px', text: 'Направление' }));
    blocks.push(el('div', { class: 'chips' }, state.styles.map(styleButton)));

    const wishes = el('textarea', {
        id: 'wishes', rows: '3',
        placeholder: 'Например: хочу тёплые тона, много растений, '
            + 'большой мягкий диван, без ковров',
    });
    blocks.push(el('h2', { style: 'margin-top:22px', text: 'Пожелания' }));
    blocks.push(el('div', { class: 'muted small',
        text: 'Необязательно. Пишите своими словами — программа переведёт '
            + 'их в задание художнику.' }));
    blocks.push(wishes);

    const button = el('button', {
        class: 'btn btn-primary', style: 'margin-top:16px',
        text: 'Показать, как будет выглядеть',
    });
    button.disabled = !room || !state.ready;
    button.addEventListener('click', () => make(button, wishes));

    blocks.push(el('div', { style: 'margin-top:16px' }, [
        button,
        el('span', { class: 'muted small', style: 'margin-left:12px',
            text: `Одна картинка — около ${String(state.price_rub).replace('.', ',')} ₽. `
                + `Сегодня потрачено ${String(state.spent_today_rub).replace('.', ',')} ₽.` }),
    ]));

    if (chosenRefs.size) {
        blocks.push(el('div', { class: 'small', style: 'margin-top:8px',
            text: `Рисуем по вашим картинкам: ${chosenRefs.size}. `
                + 'Они отмечены галочкой ниже. Цвета и материалы возьмутся '
                + 'с них, а не из названия направления.' }));
    } else if (references.length) {
        blocks.push(el('div', { class: 'small', style: 'color:var(--warn);margin-top:8px',
            text: '⚠ Ни одна из загруженных картинок не отмечена — рисовать '
                + 'будем только по названию направления. Чтобы рисовать по '
                + 'картинке, нажмите под ней «не участвует».' }));
    }

    blocks.push(el('div', { id: 'result', style: 'margin-top:18px' }));
    workBox.replaceChildren(...blocks);
}

// ── Рисование ────────────────────────────────────────────────────────

async function make(button, wishes) {
    const resultBox = document.getElementById('result');
    const started = Date.now();
    const original = button.textContent;
    button.disabled = true;

    const tick = () => {
        const seconds = Math.round((Date.now() - started) / 1000);
        button.textContent = `Рисую… ${seconds} с`;
    };
    tick();
    const timer = setInterval(tick, 1000);
    resultBox.replaceChildren(el('div', { class: 'spinner' }));

    try {
        const made = await api.post(`/api/projects/${projectId}/design`, {
            room_id: chosenRoom,
            style: chosenStyle,
            wishes: wishes.value,
            reference_ids: [...chosenRefs],
        });
        resultBox.replaceChildren(picture(made, true));
        toast('Готово', `${made.room}: ${made.style.split('—')[0].trim()}`, 'ok');
        await load(false);
    } catch (err) {
        showError(err);
        resultBox.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не получилось нарисовать.' }),
            el('div', { class: 'small', text: (err && err.hint) || '' }),
            technicalNote(err && err.technical),
        ]));
    } finally {
        clearInterval(timer);
        button.disabled = false;
        button.textContent = original;
    }
}

// ── Показ картинки ───────────────────────────────────────────────────

function picture(made, fresh) {
    const image = el('img', {
        src: made.image, alt: `${made.room}: ${made.style}`,
        class: 'render',
    });
    image.addEventListener('click', () => openBig(made.image));

    const parts = [image, el('div', { class: 'small', style: 'margin-top:6px',
        text: `${made.room} · ${made.style}` })];

    const used = made.references || [];
    parts.push(el('div', { class: 'muted small',
        text: used.length
            ? `Нарисовано по вашим картинкам: ${used.join(', ')}`
            : 'Нарисовано только по названию направления, без ваших картинок' }));

    if (fresh) {
        parts.push(el('div', { class: 'muted small',
            text: `Потрачено: ${String(made.cost_rub).replace('.', ',')} ₽ · `
                + `сегодня всего ${String(made.spent_today_rub).replace('.', ',')} ₽` }));
        parts.push(el('details', { class: 'tech' }, [
            el('summary', { text: 'Что программа попросила нарисовать' }),
            el('pre', { text: made.prompt }),
        ]));
    }
    return el('div', { class: 'card-picture' }, parts);
}

function gallery() {
    if (!state.pictures.length) {
        galleryBox.replaceChildren();
        return;
    }
    const rows = state.pictures.map((made) => {
        const card = picture(made, false);
        const remove = el('button', { class: 'btn btn-quiet btn-small',
            text: 'Удалить' });
        remove.addEventListener('click', async () => {
            if (!confirm('Удалить эту картинку?')) return;
            try {
                await api.del(`/api/renders/${made.id}`);
                await load(false);
            } catch (err) { showError(err); }
        });
        card.appendChild(remove);
        return card;
    });
    galleryBox.replaceChildren(
        el('h2', { text: 'Нарисованное раньше' }),
        el('div', { class: 'pictures' }, rows),
    );
}

// ── Референсы ────────────────────────────────────────────────────────

function referenceCard(picture) {
    const chosen = chosenRefs.has(picture.id);
    const image = el('img', {
        src: picture.image, alt: picture.name, class: 'render',
        loading: 'lazy',
    });

    const pick = el('button', {
        class: chosen ? 'chip chip-on' : 'chip',
        style: 'margin-top:8px',
        text: chosen ? '✓ рисуем по этой' : '☐ не участвует',
        title: chosen
            ? 'Эта картинка уйдёт художнику. Нажмите, чтобы убрать.'
            : 'Нажмите, чтобы рисовать по этой картинке.',
    });
    pick.addEventListener('click', () => {
        if (chosenRefs.has(picture.id)) {
            chosenRefs.delete(picture.id);
        } else if (chosenRefs.size >= 4) {
            toast('Больше четырёх не нужно',
                  'Программа начнёт усреднять вместо того, чтобы взять главное.');
            return;
        } else {
            chosenRefs.add(picture.id);
        }
        drawReferences();
        draw();
    });

    const remove = el('button', {
        class: 'btn btn-quiet btn-small', style: 'margin-left:8px',
        text: 'Удалить',
    });
    remove.addEventListener('click', async () => {
        if (!confirm(`Удалить «${picture.name}»?`)) return;
        try {
            await api.del(`/api/references/${picture.id}`);
            chosenRefs.delete(picture.id);
            await loadReferences();
            draw();
        } catch (err) { showError(err); }
    });

    if (!chosen) image.classList.add('faded');
    const card = el('div', { class: chosen ? 'card-picture card-chosen' : 'card-picture' },
        [image]);
    if (picture.room) {
        card.appendChild(el('div', { class: 'muted small', style: 'margin-top:6px',
            text: `Для комнаты: ${picture.room}` }));
    }
    card.appendChild(el('div', { style: 'margin-top:4px' }, [pick, remove]));
    return card;
}

function drawReferences() {
    if (!references.length) {
        refsBox.replaceChildren(el('div', { class: 'muted small',
            text: 'Пока ничего не загружено. Без референсов тоже можно — '
                + 'тогда программа возьмёт только выбранное направление.' }));
        return;
    }
    refsBox.replaceChildren(
        el('div', { class: 'pictures' }, references.map(referenceCard)),
    );
}

async function loadReferences() {
    try {
        const data = await api.get(`/api/projects/${projectId}/references`);
        references = data.references;
        // Выбранное могло исчезнуть вместе с картинкой.
        const alive = new Set(references.map((r) => r.id));
        chosenRefs = new Set([...chosenRefs].filter((id) => alive.has(id)));
        drawReferences();
    } catch (err) {
        refsBox.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: 'Не удалось показать референсы.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
    }
}

// ── Загрузка референсов с компьютера ─────────────────────────────────

async function upload(files) {
    if (!files.length) return;
    const form = new FormData();
    for (const file of files) form.append('files', file);
    if (chosenRoom) form.append('room_id', String(chosenRoom));

    dropzone.classList.add('busy');
    try {
        const answer = await fetch(`/api/projects/${projectId}/references`, {
            method: 'POST', body: form,
        });
        const data = await answer.json();
        if (!answer.ok) throw data;
        if (data.problems && data.problems.length) {
            toast('Часть картинок не загрузилась',
                  data.problems.map((p) => p.name).join(', '));
        } else {
            toast('Загружено', `Картинок: ${data.added.length}. `
                + 'Рисовать будем по ним.', 'ok');
        }
        // Раз картинку загрузили — значит, хотят по ней и рисовать.
        // Заставлять искать кнопку «взять за основу» незачем.
        for (const added of data.added) {
            for (const id of added.ids || []) {
                if (chosenRefs.size < 4) chosenRefs.add(id);
            }
        }
        await loadReferences();
        draw();
    } catch (err) {
        showError(err);
    } finally {
        dropzone.classList.remove('busy');
        picker.value = '';
    }
}

picker.addEventListener('change', () => upload([...picker.files]));
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
dropzone.addEventListener('drop', (e) => upload([...e.dataTransfer.files]));

// ── Загрузка ─────────────────────────────────────────────────────────

async function load(withSpinner = true) {
    if (withSpinner) workBox.replaceChildren(el('div', { class: 'spinner' }));
    try {
        const [project, data] = await Promise.all([
            api.get(`/api/projects/${projectId}`),
            api.get(`/api/projects/${projectId}/design`),
        ]);
        const crumb = document.getElementById('crumb-project');
        crumb.textContent = project.name;
        crumb.href = `/project/${projectId}`;

        state = data;
        if (chosenRoom === null && data.rooms.length) chosenRoom = data.rooms[0].id;
        draw();
        gallery();
        await loadReferences();
    } catch (err) {
        workBox.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не удалось открыть раздел.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
    }
}

load();
