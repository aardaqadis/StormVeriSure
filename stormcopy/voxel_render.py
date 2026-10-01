"""Small, allocation-bounded software renderer for the Tk vehicle previews.

Each call consumes every supplied component once and returns one binary PPM
image. Tk can load this directly with ``PhotoImage(data=ppm, format="PPM")``.
The renderer keeps an RGB buffer and a compact per-pixel depth buffer; it does
not create a Tk object (or a Python record) for each component.
"""

from array import array
import math


_BACKGROUND = 255
_OUTLINE = (93, 100, 105)
_RGB = {
    "matched": (57, 168, 90),
    "changed": (229, 186, 55),
    "unmatched": (215, 115, 104),
    "unverified": (183, 190, 196),
}


def _shade(rgb, factor):
    if factor > 1:
        return tuple(min(255, round(v + (255 - v) * (factor - 1))) for v in rgb)
    return tuple(round(v * factor) for v in rgb)


_FACES = {
    label: (_shade(rgb, 1.14), rgb, _shade(rgb, .72))
    for label, rgb in _RGB.items()
}
_RGB_CODES = (_RGB["unverified"], _RGB["matched"],
              _RGB["changed"], _RGB["unmatched"])
_FACE_CODES = (_FACES["unverified"], _FACES["matched"],
               _FACES["changed"], _FACES["unmatched"])
_LABEL_CODES = {"unverified": 0, "matched": 1,
                "changed": 2, "unmatched": 3}


def _label_source(labels):
    """Read compact match codes directly when the overlay supplies them."""
    codes = getattr(labels, "codes", None)
    return iter(codes if codes is not None else labels)


def _palette_entry(palette, coded_palette, label):
    if isinstance(label, int):
        return coded_palette[label] if 0 <= label < len(coded_palette) else coded_palette[0]
    return palette.get(label, palette["unverified"])


def _label_code(label):
    if isinstance(label, int):
        return label if 0 <= label < len(_RGB_CODES) else 0
    return _LABEL_CODES.get(label, 0)


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("Preview render superseded")


def _dedupe_repeated_positions(points, labels, bounds, cancelled):
    """Collapse pathological duplicate grids without scaling memory with input size.

    The last cube at an identical position wins at every visible pixel. The
    bound avoids building a large Python dictionary for ordinary vehicles.
    """
    try:
        count = len(points)
    except TypeError:
        return points, labels
    low, high = bounds
    volume = 1
    for axis in range(3):
        volume *= max(0, high[axis] - low[axis] + 1)
        if volume > 20_000:
            return points, labels
    if count <= 4 * volume:
        return points, labels
    latest = {}
    label_iter = _label_source(labels)
    for index, point in enumerate(points):
        if index & 255 == 0:
            _check_cancelled(cancelled)
        key = (point[0], point[1], point[2])
        # Reinsert so later duplicate cubes keep their original draw order.
        if key in latest:
            del latest[key]
        latest[key] = (point, next(label_iter, "unverified"))
    _check_cancelled(cancelled)
    selected = tuple(latest.values())
    return (tuple(pair[0] for pair in selected),
            tuple(pair[1] for pair in selected))


def _camera(yaw, pitch):
    return (math.sin(yaw) * math.cos(pitch), math.sin(pitch),
            -math.cos(yaw) * math.cos(pitch))


def _projection(view, yaw, pitch):
    """Return two screen basis vectors and one depth basis vector."""
    if view == "Top":
        return (1, 0, 0), (0, 0, 1), (0, 1, 0)
    if view == "Side":
        return (0, 0, 1), (0, -1, 0), (1, 0, 0)
    if view == "Front":
        return (1, 0, 0), (0, -1, 0), (0, 0, 1)
    if view != "3D":
        raise ValueError(f"Unknown preview view: {view}")
    sy, cy = math.sin(yaw), math.cos(yaw)
    sp, cp = math.sin(pitch), math.cos(pitch)
    return (cy, 0, sy), (sp * sy, -cp, -sp * cy), _camera(yaw, pitch)


def _dot(basis, point):
    return (basis[0] * point[0] + basis[1] * point[1] +
            basis[2] * point[2])


def _bounds_projection(bounds, horizontal, vertical):
    low, high = bounds
    projected = [
        (_dot(horizontal, (x, y, z)), _dot(vertical, (x, y, z)))
        for x in (low[0] - .5, high[0] + .5)
        for y in (low[1] - .5, high[1] + .5)
        for z in (low[2] - .5, high[2] + .5)
    ]
    return (min(p[0] for p in projected), max(p[0] for p in projected),
            min(p[1] for p in projected), max(p[1] for p in projected))


def _face_vertices(axis, sign):
    axes = [a for a in range(3) if a != axis]
    vertices = []
    for first, second in ((-.5, -.5), (.5, -.5), (.5, .5), (-.5, .5)):
        point = [0.0, 0.0, 0.0]
        point[axis] = .5 * sign
        point[axes[0]] = first
        point[axes[1]] = second
        vertices.append(point)
    return vertices


def _inside_convex(x, y, vertices):
    positive = negative = False
    for i in range(4):
        x1, y1, _ = vertices[i]
        x2, y2, _ = vertices[(i + 1) % 4]
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        positive |= cross > 1e-7
        negative |= cross < -1e-7
        if positive and negative:
            return False
    return True


def _distance_to_edge(x, y, vertices):
    smallest = float("inf")
    for i in range(4):
        x1, y1, _ = vertices[i]
        x2, y2, _ = vertices[(i + 1) % 4]
        vx, vy = x2 - x1, y2 - y1
        divisor = vx * vx + vy * vy
        if divisor < 1e-10:
            continue
        t = max(0.0, min(1.0, ((x - x1) * vx + (y - y1) * vy) / divisor))
        distance = math.hypot(x - x1 - t * vx, y - y1 - t * vy)
        smallest = min(smallest, distance)
    return smallest


def _face_masks(horizontal, vertical, depth_basis, scale, camera):
    """Rasterize one origin cube to reusable face offsets for this frame."""
    faces = []
    for axis in (2, 0, 1):
        direction = camera[axis]
        if abs(direction) < .03:
            continue
        vertices = [(_dot(horizontal, corner) * scale,
                     _dot(vertical, corner) * scale,
                     _dot(depth_basis, corner))
                    for corner in _face_vertices(axis, 1 if direction > 0 else -1)]
        x0, y0, d0 = vertices[0]
        x1, y1, d1 = vertices[1]
        x2, y2, d2 = vertices[2]
        determinant = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        if abs(determinant) < 1e-9:
            continue
        gradient_x = ((d1 - d0) * (y2 - y0) - (d2 - d0) * (y1 - y0)) / determinant
        gradient_y = ((x1 - x0) * (d2 - d0) - (x2 - x0) * (d1 - d0)) / determinant
        left = math.floor(min(v[0] for v in vertices))
        right = math.ceil(max(v[0] for v in vertices))
        top = math.floor(min(v[1] for v in vertices))
        bottom = math.ceil(max(v[1] for v in vertices))
        shade = 0 if axis == 1 else 1 if axis == 0 else 2
        offsets = []
        for py in range(top, bottom + 1):
            for px in range(left, right + 1):
                if not _inside_convex(px, py, vertices):
                    continue
                delta_depth = d0 + gradient_x * (px - x0) + gradient_y * (py - y0)
                border = scale >= 4 and _distance_to_edge(px, py, vertices) < .6
                offsets.append((px, py, delta_depth, border))
        if offsets:
            faces.append((shade, offsets))
    return faces


def _write_pixel(rgb_buffer, pixel_index, rgb):
    offset = pixel_index * 3
    rgb_buffer[offset] = rgb[0]
    rgb_buffer[offset + 1] = rgb[1]
    rgb_buffer[offset + 2] = rgb[2]


def _render_3d(points, labels, width, height, scale, mid_x, mid_y,
               horizontal, vertical, depth_basis, yaw, pitch, rgb, depth,
               cancelled):
    _check_cancelled(cancelled)
    camera = _camera(yaw, pitch)
    faces = _face_masks(horizontal, vertical, depth_basis, scale, camera)
    _check_cancelled(cancelled)
    # A subpixel cube has no rasterizable face. At this zoom level each
    # projected component contributes to its nearest visible pixel instead.
    if not faces or scale < 1.5:
        label_iter = _label_source(labels)
        hx, hy, hz = horizontal
        vx, vy, vz = vertical
        dx, dy, dz = depth_basis
        for number, point in enumerate(points):
            if number & 255 == 0:
                _check_cancelled(cancelled)
            label = next(label_iter, "unverified")
            x, y, z = point[0], point[1], point[2]
            sx = round(width / 2 + (hx * x + hy * y + hz * z - mid_x) * scale)
            sy = round(height / 2 + (vx * x + vy * y + vz * z - mid_y) * scale)
            if sx < 0 or sx >= width or sy < 0 or sy >= height:
                continue
            index = sy * width + sx
            cube_depth = dx * x + dy * y + dz * z
            if cube_depth >= depth[index]:
                depth[index] = cube_depth
                _write_pixel(rgb, index, _palette_entry(_RGB, _RGB_CODES, label))
        return
    margin = max(2, math.ceil(scale * .9))
    cancel_mask = 15 if sum(len(offsets) for _, offsets in faces) > 128 else 255
    label_iter = _label_source(labels)
    hx, hy, hz = horizontal
    vx, vy, vz = vertical
    dx, dy, dz = depth_basis
    for number, point in enumerate(points):
        if number & cancel_mask == 0:
            _check_cancelled(cancelled)
        label = next(label_iter, "unverified")
        x, y, z = point[0], point[1], point[2]
        sx = round(width / 2 + (hx * x + hy * y + hz * z - mid_x) * scale)
        sy = round(height / 2 + (vx * x + vy * y + vz * z - mid_y) * scale)
        if sx < -margin or sx >= width + margin or sy < -margin or sy >= height + margin:
            continue
        cube_depth = dx * x + dy * y + dz * z
        colors = _palette_entry(_FACES, _FACE_CODES, label)
        for shade, offsets in faces:
            fill = colors[shade]
            for dx, dy, depth_offset, border in offsets:
                px, py = sx + dx, sy + dy
                if px < 0 or px >= width or py < 0 or py >= height:
                    continue
                index = py * width + px
                visible_depth = cube_depth + depth_offset
                if visible_depth >= depth[index]:
                    depth[index] = visible_depth
                    _write_pixel(rgb, index, _OUTLINE if border else fill)


def _render_orthographic(points, labels, width, height, scale, mid_x, mid_y,
                         horizontal, vertical, depth_basis, rgb, depth,
                         cancelled):
    side = max(1, min(18, round(scale * .82)))
    low = side // 2
    high = side - low
    # Sparse previews are cheaper drawn directly and need no extra center
    # buffers. Dense previews benefit from coalescing the occluded squares.
    try:
        sparse = len(points) < width * height // 8
    except TypeError:
        sparse = False
    if sparse:
        label_iter = _label_source(labels)
        hx, hy, hz = horizontal
        vx, vy, vz = vertical
        dx, dy, dz = depth_basis
        for number, point in enumerate(points):
            if number & 63 == 0:
                _check_cancelled(cancelled)
            label = next(label_iter, "unverified")
            x, y, z = point[0], point[1], point[2]
            sx = round(width / 2 + (hx * x + hy * y + hz * z - mid_x) * scale)
            sy = round(height / 2 + (vx * x + vy * y + vz * z - mid_y) * scale)
            left, right = max(0, sx - low), min(width, sx + high)
            top, bottom = max(0, sy - low), min(height, sy + high)
            if left >= right or top >= bottom:
                continue
            cube_depth = dx * x + dy * y + dz * z
            fill = _palette_entry(_RGB, _RGB_CODES, label)
            for py in range(top, bottom):
                row = py * width
                for px in range(left, right):
                    index = row + px
                    if cube_depth >= depth[index]:
                        depth[index] = cube_depth
                        outline = side >= 4 and (px == sx - low or px == sx + high - 1 or
                                                 py == sy - low or py == sy + high - 1)
                        _write_pixel(rgb, index, _OUTLINE if outline else fill)
        return
    # First retain only the frontmost cube for each projected center. A cube
    # behind another with the same center has exactly the same square and
    # cannot contribute any visible pixel. These arrays depend on viewport
    # size, never on the number of XML components.
    padding = side
    stride = width + 2 * padding
    rows = height + 2 * padding
    centers_depth = array("f", [-float("inf")]) * (stride * rows)
    centers_code = bytearray(stride * rows)
    label_iter = _label_source(labels)
    hx, hy, hz = horizontal
    vx, vy, vz = vertical
    dx, dy, dz = depth_basis
    for number, point in enumerate(points):
        if number & 255 == 0:
            _check_cancelled(cancelled)
        label = next(label_iter, "unverified")
        x, y, z = point[0], point[1], point[2]
        sx = round(width / 2 + (hx * x + hy * y + hz * z - mid_x) * scale)
        sy = round(height / 2 + (vx * x + vy * y + vz * z - mid_y) * scale)
        if (sx + high <= 0 or sx - low >= width or
                sy + high <= 0 or sy - low >= height):
            continue
        cube_depth = dx * x + dy * y + dz * z
        center_index = (sy + padding) * stride + sx + padding
        if cube_depth >= centers_depth[center_index]:
            centers_depth[center_index] = cube_depth
            centers_code[center_index] = _label_code(label)
    _check_cancelled(cancelled)
    for cy in range(rows):
        if cy & 31 == 0:
            _check_cancelled(cancelled)
        sy = cy - padding
        center_row = cy * stride
        for cx in range(stride):
            center_index = center_row + cx
            cube_depth = centers_depth[center_index]
            if cube_depth == -float("inf"):
                continue
            sx = cx - padding
            left, right = max(0, sx - low), min(width, sx + high)
            top, bottom = max(0, sy - low), min(height, sy + high)
            fill = _RGB_CODES[centers_code[center_index]]
            for py in range(top, bottom):
                row = py * width
                for px in range(left, right):
                    index = row + px
                    if cube_depth >= depth[index]:
                        depth[index] = cube_depth
                        outline = side >= 4 and (px == sx - low or px == sx + high - 1 or
                                                 py == sy - low or py == sy + high - 1)
                        _write_pixel(rgb, index, _OUTLINE if outline else fill)


def render_voxels(points, labels, bounds, view, yaw, pitch, zoom, width, height,
                  common_span=None, cancelled=None):
    """Render all supplied grid cubes to P6 PPM bytes for a single Tk image.

    ``points`` and ``labels`` may be lazy iterables. Memory is proportional to
    viewport pixels, independent of the XML's component count. Orthographic
    views retain one frontmost cube per projected pixel center, then draw
    depth-tested squares. The 3D view uses three depth-tested faces and
    quantizes each center to a screen pixel. A small, excessively duplicated
    3D grid is coalesced before drawing. Occluded cubes remain processed but
    naturally cannot be visible. ``cancelled`` can abort stale background jobs.
    """
    width, height = max(1, int(width)), max(1, int(height))
    _check_cancelled(cancelled)
    horizontal, vertical, depth_basis = _projection(view, yaw, pitch)
    left, right, top, bottom = _bounds_projection(bounds, horizontal, vertical)
    span_x, span_y = common_span or (right - left, bottom - top)
    scale = min(max(width - 20, 1) / max(span_x, 1),
                max(height - 20, 1) / max(span_y, 1), 22) * zoom
    mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
    rgb = bytearray([_BACKGROUND]) * (width * height * 3)
    depth = array("f", [-float("inf")]) * (width * height)
    if view == "3D":
        points, labels = _dedupe_repeated_positions(points, labels, bounds,
                                                     cancelled)
        _render_3d(points, labels, width, height, scale, mid_x, mid_y,
                   horizontal, vertical, depth_basis, yaw, pitch, rgb, depth,
                   cancelled)
    else:
        _render_orthographic(points, labels, width, height, scale, mid_x, mid_y,
                             horizontal, vertical, depth_basis, rgb, depth,
                             cancelled)
    _check_cancelled(cancelled)
    return f"P6\n{width} {height}\n255\n".encode("ascii") + rgb
