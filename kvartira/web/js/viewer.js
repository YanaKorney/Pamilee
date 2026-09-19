// Страница «3D-модель»: показ квартиры в объёме.

import { api, el, showError, formatArea } from './api.js';
import { THREE, buildApartment, addLighting, metres } from './scene3d.js';
import { OrbitControls } from '/static/vendor/OrbitControls.js';

const projectId = Number(window.location.pathname.split('/')[2]);
const content = document.getElementById('content');

function emptyState(message, hint) {
    return el('div', { class: 'empty' }, [
        el('div', { class: 'icon', text: '🧱' }),
        el('h3', { text: message }),
        el('p', { class: 'muted', text: hint }),
        el('a', {
            class: 'btn btn-primary',
            href: `/project/${projectId}/plan`,
            text: 'Перейти к планировке',
        }),
    ]);
}

function toolbarButton(label, onClick, pressed = false) {
    const button = el('button', {
        class: pressed ? 'btn btn-primary' : 'btn',
        text: label,
    });
    button.addEventListener('click', () => onClick(button));
    return button;
}

function setPressed(button, on) {
    button.className = on ? 'btn btn-primary' : 'btn';
}

async function load() {
    let data;
    try {
        data = await api.get(`/api/projects/${projectId}/scene`);
    } catch (err) {
        content.replaceChildren(el('div', { class: 'notice notice-error' }, [
            el('strong', { text: (err && err.error) || 'Не удалось открыть модель.' }),
            el('div', { class: 'small muted', text: (err && err.hint) || '' }),
        ]));
        return;
    }

    const crumb = document.getElementById('crumb-project');
    crumb.textContent = data.name || 'Проект';
    crumb.href = `/project/${projectId}`;
    document.title = (data.name || 'Проект') + ' · 3D-модель';

    if (!data.rooms || data.rooms.length === 0) {
        content.replaceChildren(emptyState(
            'Модель пока не из чего строить',
            'Сначала загрузите план и нажмите «Разобрать план» — программа найдёт '
            + 'комнаты, и они появятся здесь в объёме.'
        ));
        return;
    }

    render(data);
}

function render(data) {
    const canvasBox = el('div', {
        class: 'viewport',
        style: 'position:relative',
    });
    const info = el('div', { class: 'viewport-info' });
    const toolbar = el('div', { class: 'row', style: 'margin-bottom:14px' });

    content.replaceChildren(toolbar, canvasBox, el('div', { id: 'stats' }));
    canvasBox.appendChild(info);

    // ── Сцена ────────────────────────────────────────────────────────
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf2efea);
    addLighting(scene);

    const parts = buildApartment(data);
    scene.add(parts.root);

    // Земля под квартирой — чтобы падала тень и был «низ»
    const ground = new THREE.Mesh(
        new THREE.PlaneGeometry(200, 200),
        new THREE.MeshStandardMaterial({ color: 0xe6e2db, roughness: 1 })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.01;
    ground.receiveShadow = true;
    scene.add(ground);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    canvasBox.appendChild(renderer.domElement);

    const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 500);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI / 2 - 0.02;
    controls.minDistance = 1.5;
    controls.maxDistance = 80;

    const bounds = data.bounds;
    const width = metres(bounds.width) || 10;
    const depth = metres(bounds.depth) || 8;
    const span = Math.max(width, depth);

    // Подписи комнат лежат на полу и нужны на виде сверху.
    // В перспективе они мешают: читаются боком и просвечивают сквозь стены.
    function viewOverview() {
        parts.labels.visible = false;
        camera.position.set(span * 0.75, span * 0.85, span * 0.95);
        controls.target.set(0, metres(data.ceiling_height_mm) / 2, 0);
        controls.update();
    }
    function viewTop() {
        parts.labels.visible = true;
        camera.position.set(0, span * 1.5, 0.001);
        controls.target.set(0, 0, 0);
        controls.update();
    }
    function focusRoom(room) {
        parts.labels.visible = false;
        const xs = room.polygon.map((p) => metres(p[0]));
        const ys = room.polygon.map((p) => metres(p[1]));
        const cx = (Math.min(...xs) + Math.max(...xs)) / 2 + parts.root.position.x;
        const cz = (Math.min(...ys) + Math.max(...ys)) / 2 + parts.root.position.z;
        const size = Math.max(Math.max(...xs) - Math.min(...xs),
                              Math.max(...ys) - Math.min(...ys), 2);
        controls.target.set(cx, metres(data.ceiling_height_mm) / 2, cz);
        camera.position.set(cx + size * 0.9, size * 1.1, cz + size * 0.9);
        controls.update();
    }

    viewOverview();

    function resize() {
        const width = canvasBox.clientWidth;
        const height = Math.max(380, Math.round(window.innerHeight * 0.62));
        renderer.setSize(width, height, false);
        renderer.domElement.style.width = '100%';
        renderer.domElement.style.height = height + 'px';
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
    }
    resize();
    window.addEventListener('resize', resize);

    // ── Подсказка при наведении ──────────────────────────────────────
    const pointer = new THREE.Vector2();
    const raycaster = new THREE.Raycaster();
    renderer.domElement.addEventListener('pointermove', (event) => {
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
        pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
        raycaster.setFromCamera(pointer, camera);
        const hits = raycaster.intersectObjects([parts.items, parts.floors], true);
        let text = '';
        for (const hit of hits) {
            let node = hit.object;
            while (node && !node.userData?.kind) node = node.parent;
            if (!node) continue;
            if (node.userData.kind === 'item') { text = node.userData.label; break; }
            if (node.userData.kind === 'floor') { text = node.userData.roomName; break; }
        }
        info.textContent = text;
        info.style.display = text ? 'block' : 'none';
    });

    // ── Кнопки ───────────────────────────────────────────────────────
    let wallsVisible = true;
    let ceilingVisible = false;

    const wallsButton = toolbarButton('Скрыть стены', (button) => {
        wallsVisible = !wallsVisible;
        parts.walls.visible = wallsVisible;
        button.textContent = wallsVisible ? 'Скрыть стены' : 'Показать стены';
        setPressed(button, !wallsVisible);
    });
    const ceilingButton = toolbarButton('Показать потолок', (button) => {
        ceilingVisible = !ceilingVisible;
        parts.ceilings.visible = ceilingVisible;
        button.textContent = ceilingVisible ? 'Скрыть потолок' : 'Показать потолок';
        setPressed(button, ceilingVisible);
    });

    const roomSelect = el('select', { style: 'width:auto; min-width:180px' });
    roomSelect.appendChild(el('option', { value: '', text: 'Вся квартира' }));
    data.rooms.forEach((room, index) => {
        roomSelect.appendChild(el('option', {
            value: String(index),
            text: `${room.name} · ${formatArea(room.area_m2)}`,
        }));
    });
    roomSelect.addEventListener('change', () => {
        const value = roomSelect.value;
        if (value === '') viewOverview();
        else focusRoom(data.rooms[Number(value)]);
    });

    toolbar.append(
        toolbarButton('Вид сверху', viewTop),
        toolbarButton('Общий вид', viewOverview),
        wallsButton,
        ceilingButton,
        roomSelect,
    );

    // ── Сводка ───────────────────────────────────────────────────────
    const totalArea = data.rooms.reduce((sum, r) => sum + (r.area_m2 || 0), 0);
    document.getElementById('stats').replaceChildren(
        el('div', { class: 'stat-row', style: 'margin-top:16px' }, [
            el('div', { class: 'stat' }, [
                el('div', { class: 'k', text: 'Помещений' }),
                el('div', { class: 'v', text: String(data.rooms.length) }),
            ]),
            el('div', { class: 'stat' }, [
                el('div', { class: 'k', text: 'Площадь' }),
                el('div', { class: 'v', text: formatArea(totalArea) }),
            ]),
            el('div', { class: 'stat' }, [
                el('div', { class: 'k', text: 'Стен' }),
                el('div', { class: 'v', text: String(data.walls.length) }),
            ]),
            el('div', { class: 'stat' }, [
                el('div', { class: 'k', text: 'Предметов' }),
                el('div', { class: 'v', text: String(data.items.length) }),
            ]),
            el('div', { class: 'stat' }, [
                el('div', { class: 'k', text: 'Потолок' }),
                el('div', {
                    class: 'v',
                    text: (data.ceiling_height_mm / 1000).toFixed(2).replace('.', ',') + ' м',
                }),
            ]),
        ]),
        el('div', { class: 'muted small', style: 'margin-top:12px',
            text: 'Мебель показана правильными габаритами на своих местах. '
                + 'Внешний вид появится на этапе визуализаций.' }),
    );

    function animate() {
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }
    animate();
}

load();
