// Построение трёхмерной модели квартиры по плану.
//
// План хранится в миллиметрах: X вправо, Y вниз, как на чертеже.
// В трёхмерной сцене принято иначе: X вправо, Z «вглубь», Y вверх,
// и удобнее метры. Перевод делается здесь, в одном месте.

import * as THREE from '/static/vendor/three.module.min.js';

const MM = 0.001;

// Цвета помещений. Спокойные, чтобы не спорить с будущей отделкой,
// но всё же различимые: иначе сверху квартира выглядит одним пятном
// и понять, где что, невозможно.
const ROOM_COLORS = {
    living: 0xd9c9ae, bedroom: 0xc9bfd6, kids: 0xc2d6c4,
    kitchen: 0xe0cfae, kitchen_living: 0xdcc9a8,
    bathroom: 0xaecbd6, toilet: 0xb6d2d9,
    hall: 0xd6cfc4, corridor: 0xcfc8bd, wardrobe: 0xd3c4b4,
    balcony: 0xc2c8c2, other: 0xd4d0c9,
};

const ITEM_COLORS = {
    furniture: 0xa98d6f, plumbing: 0xdfe6e9,
    appliance: 0xb9bec2, light: 0xf0e2b8, decor: 0xb08d78,
};

const WALL_COLOR = 0xeceae6;
const CEILING_COLOR = 0xf4f2ee;

export function metres(mm) {
    return mm * MM;
}

// ── Материалы ────────────────────────────────────────────────────────

function surface(color, options = {}) {
    return new THREE.MeshStandardMaterial({
        color, roughness: 0.85, metalness: 0.02, ...options,
    });
}

// ── Пол и потолок ────────────────────────────────────────────────────

function roomShape(polygon) {
    const shape = new THREE.Shape();
    polygon.forEach(([x, y], index) => {
        const px = metres(x);
        const pz = metres(y);
        if (index === 0) shape.moveTo(px, pz);
        else shape.lineTo(px, pz);
    });
    shape.closePath();
    return shape;
}

function buildFloor(room) {
    const geometry = new THREE.ShapeGeometry(roomShape(room.polygon));
    geometry.rotateX(Math.PI / 2);
    const mesh = new THREE.Mesh(
        geometry,
        surface(ROOM_COLORS[room.kind] || ROOM_COLORS.other)
    );
    mesh.receiveShadow = true;
    mesh.position.y = 0.002;
    mesh.userData = { kind: 'floor', roomId: room.id, roomName: room.name };
    return mesh;
}

function buildCeiling(room) {
    const geometry = new THREE.ShapeGeometry(roomShape(room.polygon));
    geometry.rotateX(-Math.PI / 2);
    const mesh = new THREE.Mesh(geometry, surface(CEILING_COLOR));
    mesh.position.y = metres(room.height_mm);
    mesh.userData = { kind: 'ceiling', roomId: room.id };
    return mesh;
}

// Подпись комнаты лежит на полу — так она читается на виде сверху
// и не мешает, когда смотришь сбоку.
function buildRoomLabel(room) {
    const xs = room.polygon.map((p) => p[0]);
    const ys = room.polygon.map((p) => p[1]);
    const widthMm = Math.max(...xs) - Math.min(...xs);
    const depthMm = Math.max(...ys) - Math.min(...ys);
    if (Math.min(widthMm, depthMm) < 900) return null;

    const canvas = document.createElement('canvas');
    canvas.width = 512;
    canvas.height = 160;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(29, 27, 25, .82)';
    ctx.font = '600 58px -apple-system, Segoe UI, Roboto, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(room.name || '', canvas.width / 2, 56);
    ctx.fillStyle = 'rgba(29, 27, 25, .55)';
    ctx.font = '400 42px -apple-system, Segoe UI, Roboto, sans-serif';
    const area = room.area_m2 ? String(room.area_m2).replace('.', ',') + ' м²' : '';
    ctx.fillText(area, canvas.width / 2, 116);

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    const plateWidth = Math.min(metres(widthMm) * 0.8, 2.6);
    const plate = new THREE.Mesh(
        new THREE.PlaneGeometry(plateWidth, plateWidth * canvas.height / canvas.width),
        new THREE.MeshBasicMaterial({
            map: texture, transparent: true,
            depthWrite: false, depthTest: false,
        })
    );
    plate.rotation.x = -Math.PI / 2;
    plate.position.set(
        metres((Math.min(...xs) + Math.max(...xs)) / 2),
        0.012,
        metres((Math.min(...ys) + Math.max(...ys)) / 2)
    );
    plate.renderOrder = 3;
    return plate;
}

// ── Стены с проёмами ─────────────────────────────────────────────────

function wallPiece(wall, fromMm, toMm, bottomMm, topMm, material) {
    const length = metres(toMm - fromMm);
    const height = metres(topMm - bottomMm);
    if (length <= 0.001 || height <= 0.001) return null;

    const box = new THREE.BoxGeometry(length, height, metres(wall.thickness_mm));
    const mesh = new THREE.Mesh(box, material);
    mesh.castShadow = true;
    mesh.receiveShadow = true;

    const angle = Math.atan2(wall.y2 - wall.y1, wall.x2 - wall.x1);
    const middle = (fromMm + toMm) / 2;
    mesh.position.set(
        metres(wall.x1) + Math.cos(angle) * metres(middle),
        metres(bottomMm) + height / 2,
        metres(wall.y1) + Math.sin(angle) * metres(middle)
    );
    mesh.rotation.y = -angle;
    return mesh;
}

function buildWall(wall, material) {
    const group = new THREE.Group();
    group.userData = { kind: 'wall', wallKind: wall.kind };

    const length = Math.hypot(wall.x2 - wall.x1, wall.y2 - wall.y1);
    const height = wall.height_mm;
    const openings = (wall.openings || [])
        .slice()
        .sort((a, b) => a.offset_mm - b.offset_mm);

    let cursor = 0;
    for (const opening of openings) {
        const start = Math.max(cursor, opening.offset_mm);
        const end = Math.min(length, start + opening.width_mm);
        if (end <= start) continue;

        // Кусок стены до проёма
        const before = wallPiece(wall, cursor, start, 0, height, material);
        if (before) group.add(before);

        // Над проёмом всегда остаётся перемычка
        const top = opening.sill_mm + opening.height_mm;
        const lintel = wallPiece(wall, start, end, Math.min(top, height), height, material);
        if (lintel) group.add(lintel);

        // У окна есть ещё и стена под подоконником
        if (opening.kind === 'window' && opening.sill_mm > 0) {
            const under = wallPiece(wall, start, end, 0, opening.sill_mm, material);
            if (under) group.add(under);
        }
        cursor = end;
    }

    const last = wallPiece(wall, cursor, length, 0, height, material);
    if (last) group.add(last);
    return group;
}

// ── Мебель ───────────────────────────────────────────────────────────

function box(width, height, depth, color, y = 0) {
    const mesh = new THREE.Mesh(
        new THREE.BoxGeometry(width, height, depth),
        surface(color)
    );
    mesh.position.y = y + height / 2;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    return mesh;
}

// Кровать и диван узнаваемы по форме, поэтому им отдельная сборка:
// именно по ним человек понимает, что это его комната.
function buildBed(w, d, h, color) {
    const group = new THREE.Group();
    group.add(box(w, h * 0.55, d, color));
    const pillowWidth = w * 0.42;
    const pillow = (offset) => {
        const mesh = box(pillowWidth, h * 0.22, d * 0.2, 0xf2ece2, h * 0.55);
        mesh.position.x = offset;
        mesh.position.z = -d / 2 + d * 0.14;
        return mesh;
    };
    group.add(pillow(-w * 0.24));
    group.add(pillow(w * 0.24));
    const headboard = box(w, h * 1.1, d * 0.06, color);
    headboard.position.z = -d / 2;
    group.add(headboard);
    return group;
}

function buildSofa(w, d, h, color) {
    const group = new THREE.Group();
    group.add(box(w, h * 0.45, d, color));
    const back = box(w, h * 0.55, d * 0.22, color, h * 0.45);
    back.position.z = -d / 2 + d * 0.11;
    group.add(back);
    const arm = (offset) => {
        const mesh = box(w * 0.08, h * 0.35, d, color, h * 0.45);
        mesh.position.x = offset;
        return mesh;
    };
    group.add(arm(-w / 2 + w * 0.04));
    group.add(arm(w / 2 - w * 0.04));
    return group;
}

function buildTable(w, d, h, color) {
    const group = new THREE.Group();
    group.add(box(w, h * 0.08, d, color, h * 0.92));
    const leg = (sx, sz) => {
        const mesh = box(w * 0.06, h * 0.92, d * 0.06, color);
        mesh.position.set(sx * (w / 2 - w * 0.06), mesh.position.y, sz * (d / 2 - d * 0.06));
        return mesh;
    };
    [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([sx, sz]) => group.add(leg(sx, sz)));
    return group;
}

function buildItem(item) {
    const w = metres(item.width_mm);
    const d = metres(item.depth_mm);
    const h = metres(item.height_mm);
    const color = ITEM_COLORS[item.category] || ITEM_COLORS.furniture;

    let group;
    if (item.subtype === 'bed') group = buildBed(w, d, h, color);
    else if (item.subtype === 'sofa') group = buildSofa(w, d, h, color);
    else if (item.subtype === 'table' || item.subtype === 'desk') group = buildTable(w, d, h, color);
    else {
        group = new THREE.Group();
        group.add(box(w, h, d, color));
    }

    group.position.set(metres(item.x), metres(item.z_mm || 0), metres(item.y));
    group.rotation.y = -(item.rotation_deg || 0) * Math.PI / 180;
    group.userData = {
        kind: 'item',
        label: item.label || item.subtype,
        confidence: item.confidence,
        roomId: item.room_id,
    };
    return group;
}

// ── Сборка сцены ─────────────────────────────────────────────────────

export function buildApartment(data) {
    const root = new THREE.Group();
    const floors = new THREE.Group();
    const labels = new THREE.Group();
    const ceilings = new THREE.Group();
    const walls = new THREE.Group();
    const items = new THREE.Group();

    (data.rooms || []).forEach((room) => {
        if (!room.polygon || room.polygon.length < 3) return;
        floors.add(buildFloor(room));
        const label = buildRoomLabel(room);
        if (label) labels.add(label);
        ceilings.add(buildCeiling(room));
    });

    const wallMaterial = surface(WALL_COLOR);
    (data.walls || []).forEach((wall) => walls.add(buildWall(wall, wallMaterial)));
    (data.items || []).forEach((item) => items.add(buildItem(item)));

    ceilings.visible = false;
    root.add(floors, labels, ceilings, walls, items);

    // Ставим квартиру центром в начало координат — так удобнее вращать.
    const bounds = data.bounds || { min_x: 0, min_y: 0, width: 0, depth: 0 };
    root.position.set(
        -metres(bounds.min_x + bounds.width / 2),
        0,
        -metres(bounds.min_y + bounds.depth / 2)
    );

    return { root, floors, labels, ceilings, walls, items };
}

export function addLighting(scene) {
    // Небесный свет делает основную работу: иначе стены отбрасывают
    // резкую тень внутрь и комнаты выглядят тёмными ямами.
    scene.add(new THREE.HemisphereLight(0xffffff, 0xe2ddd4, 2.4));

    const sun = new THREE.DirectionalLight(0xfff4e6, 1.0);
    sun.position.set(8, 14, 6);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.camera.left = -14;
    sun.shadow.camera.right = 14;
    sun.shadow.camera.top = 14;
    sun.shadow.camera.bottom = -14;
    sun.shadow.camera.far = 50;
    sun.shadow.bias = -0.0004;
    scene.add(sun);

    const fill = new THREE.DirectionalLight(0xe8eef5, 0.5);
    fill.position.set(-7, 9, -8);
    scene.add(fill);
}

export { THREE };
