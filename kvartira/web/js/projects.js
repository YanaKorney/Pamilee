// Страница «Мои проекты»: список, создание, удаление.

import { api, el, showError, toast, plural, formatDate, formatArea } from './api.js';

const list = document.getElementById('list');
const dialog = document.getElementById('dialog');
const form = document.getElementById('form');

function projectCard(project) {
    const facts = [];
    if (project.declared_area_m2) facts.push(formatArea(project.declared_area_m2));
    facts.push(`потолок ${(project.ceiling_height_mm / 1000).toFixed(2).replace('.', ',')} м`);
    if (project.room_count > 0) {
        facts.push(`${project.room_count} ${plural(project.room_count, 'комната', 'комнаты', 'комнат')}`);
    }
    if (project.plan_files > 0) {
        facts.push(`${project.plan_files} ${plural(project.plan_files, 'файл', 'файла', 'файлов')} плана`);
    }

    return el('div', { class: 'card project-card' }, [
        el('a', {
            class: 'grow',
            href: `/project/${project.id}`,
            style: 'text-decoration:none;color:inherit',
        }, [
            el('div', { class: 'name', text: project.name }),
            el('div', { class: 'muted small', text: facts.join(' · ') }),
            el('div', { class: 'muted small', text: 'Изменён ' + formatDate(project.updated_at) }),
        ]),
        el('button', {
            class: 'btn btn-quiet btn-danger',
            title: 'Удалить проект',
            onclick: () => removeProject(project),
        }, [document.createTextNode('Удалить')]),
    ]);
}

function emptyState() {
    return el('div', { class: 'empty' }, [
        el('div', { class: 'icon', text: '📐' }),
        el('h3', { text: 'Пока ни одного проекта' }),
        el('p', {
            class: 'muted',
            text: 'Создайте первый — это займёт несколько секунд.',
        }),
        el('button', {
            class: 'btn btn-primary',
            onclick: openDialog,
        }, [document.createTextNode('Создать проект')]),
    ]);
}

async function load() {
    try {
        const projects = await api.get('/api/projects');
        list.replaceChildren();
        if (projects.length === 0) {
            list.appendChild(emptyState());
            return;
        }
        const box = el('div', { class: 'cards' });
        projects.forEach((p) => box.appendChild(projectCard(p)));
        list.appendChild(box);
    } catch (err) {
        list.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не удалось загрузить список.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
    }
}

async function removeProject(project) {
    const sure = confirm(
        `Удалить проект «${project.name}»?\n\n` +
        'Вместе с ним удалятся загруженные планы, референсы и созданные картинки. ' +
        'Отменить это будет нельзя.'
    );
    if (!sure) return;
    try {
        await api.del(`/api/projects/${project.id}`);
        toast('Проект удалён', '', 'ok');
        load();
    } catch (err) {
        showError(err);
    }
}

function openDialog() {
    dialog.hidden = false;
    document.getElementById('name').focus();
    document.getElementById('name').select();
}

function closeDialog() {
    dialog.hidden = true;
}

document.getElementById('new-project').addEventListener('click', openDialog);
document.getElementById('cancel').addEventListener('click', closeDialog);
dialog.addEventListener('click', (e) => { if (e.target === dialog) closeDialog(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDialog(); });

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const button = document.getElementById('create');
    button.disabled = true;
    const areaRaw = document.getElementById('area').value.replace(',', '.');
    try {
        const project = await api.post('/api/projects', {
            name: document.getElementById('name').value.trim() || 'Моя квартира',
            ceiling_height_mm: Number(document.getElementById('height').value) || 2900,
            declared_area_m2: areaRaw ? Number(areaRaw) : null,
        });
        window.location.href = `/project/${project.id}`;
    } catch (err) {
        showError(err);
        button.disabled = false;
    }
});

load();
