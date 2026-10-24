"""3MF export and PNG preview renders from a checked mesh (stdlib only).

Both writers work on the facet set in canonical order (the same order ``mesh.geometry_hash`` uses), so
their output depends only on the geometry, not on the facet order OpenSCAD happened to write: the
3MF and PNG bytes are identical for identical geometry.

The PNG is an orthographic preview with flat shading. It is a picture for catalogues and review, not a
measurement: use the STL/3MF and the manifest for dimensions.
"""

import math
import struct
import zipfile
import zlib
from xml.sax.saxutils import escape

ZIP_DATE = (1980, 1, 1, 0, 0, 0)   # fixed timestamp: no build time leaks into the archive

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '</Types>\n')
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    '</Relationships>\n')
CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def canonical(tris):
    """Facets rotated so the smallest vertex comes first (winding kept), then sorted."""
    out = []
    for t in tris:
        k = t.index(min(t))
        out.append(t[k:] + t[:k])
    out.sort()
    return out


def _f(v):
    # 9 significant digits round-trip any float32 coordinate read from a binary STL exactly
    s = "%.9g" % v
    return "0" if s == "-0" else s


def model_xml(tris, title):
    tris = canonical(tris)
    index = {}
    for t in tris:
        for v in t:
            index.setdefault(v, None)
    verts = sorted(index)
    for i, v in enumerate(verts):
        index[v] = i
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="%s">' % CORE_NS,
             '<metadata name="Title">%s</metadata>' % escape(title),
             '<metadata name="Application">rackparts</metadata>',
             '<resources><object id="1" type="model"><mesh><vertices>']
    parts.extend('<vertex x="%s" y="%s" z="%s"/>' % (_f(v[0]), _f(v[1]), _f(v[2])) for v in verts)
    parts.append('</vertices><triangles>')
    parts.extend('<triangle v1="%d" v2="%d" v3="%d"/>' % (index[a], index[b], index[c]) for a, b, c in tris)
    parts.append('</triangles></mesh></object></resources><build><item objectid="1"/></build></model>\n')
    return "".join(parts)


def write_3mf(tris, path, title):
    """Write a single-object 3MF package. Opens ``path`` with mode "x": never overwrites."""
    with open(path, "xb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("[Content_Types].xml", CONTENT_TYPES), ("_rels/.rels", RELS),
                           ("3D/3dmodel.model", model_xml(tris, title))):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data.encode("utf-8"))


def read_3mf(path):
    """Triangles of every mesh object in a 3MF package (used to check our own exports)."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("3D/3dmodel.model"))
    ns = {"m": CORE_NS}
    if root.get("unit", "millimeter") != "millimeter":
        raise ValueError("3MF unit is not millimeter")
    tris = []
    for m in root.iterfind(".//m:mesh", ns):
        vs = [(float(v.get("x")), float(v.get("y")), float(v.get("z"))) for v in m.iterfind("m:vertices/m:vertex", ns)]
        for t in m.iterfind("m:triangles/m:triangle", ns):
            tris.append((vs[int(t.get("v1"))], vs[int(t.get("v2"))], vs[int(t.get("v3"))]))
    return tris


# ---------------------------------------------------------------------------------------------------------------
# PNG preview

BACKGROUND = (246, 247, 249)
BASE_COLOUR = (112, 146, 188)
# (direction toward the light as (right, up, toward-viewer) components, weight): lights move with the camera,
# so every view is lit the same way
LIGHTS = (((-0.35, 0.60, 0.72), 0.55), ((0.75, 0.10, 0.65), 0.25))
AMBIENT = 0.40


def _norm(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n else (0.0, 0.0, 0.0)


def view_basis(azimuth_deg, elevation_deg):
    """Right, up and toward-viewer unit vectors for a camera looking at the part from the front (-y) side."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    toward = (math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el))
    right = _norm((math.cos(az), math.sin(az), 0.0))
    up = _norm((toward[1] * right[2] - toward[2] * right[1], toward[2] * right[0] - toward[0] * right[2],
                toward[0] * right[1] - toward[1] * right[0]))
    return right, up, toward


def render_rgb(tris, width=640, height=480, azimuth_deg=-35.0, elevation_deg=28.0, supersample=2, margin=0.06):
    """Orthographic, flat-shaded, z-buffered render. Returns (width, height, bytes RGB)."""
    W, H = width * supersample, height * supersample
    right, up, toward = view_basis(azimuth_deg, elevation_deg)
    lights = []
    for (r, u, f), k in LIGHTS:
        lights.append((_norm(tuple(r * right[i] + u * up[i] + f * toward[i] for i in range(3))), k))
    tris = canonical(tris)

    def proj(v):
        return (v[0] * right[0] + v[1] * right[1] + v[2] * right[2],
                v[0] * up[0] + v[1] * up[1] + v[2] * up[2],
                v[0] * toward[0] + v[1] * toward[1] + v[2] * toward[2])

    pts = {}
    for t in tris:
        for v in t:
            if v not in pts:
                pts[v] = proj(v)
    us = [p[0] for p in pts.values()]
    vs = [p[1] for p in pts.values()]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    scale = min(W * (1 - 2 * margin) / max(u1 - u0, 1e-9), H * (1 - 2 * margin) / max(v1 - v0, 1e-9))
    ou = (W - (u1 - u0) * scale) / 2 - u0 * scale
    ov = (H - (v1 - v0) * scale) / 2 + v1 * scale

    zbuf = [-math.inf] * (W * H)
    cbuf = [None] * (W * H)
    for t in tris:
        a, b, c = t
        n = _norm(((b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
                   (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
                   (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])))
        if n[0] * toward[0] + n[1] * toward[1] + n[2] * toward[2] <= 0:
            continue   # back face of a closed, outward-oriented mesh
        shade = AMBIENT + sum(k * max(0.0, n[0] * d[0] + n[1] * d[1] + n[2] * d[2]) for d, k in lights)
        col = tuple(min(255, int(ch * shade + 0.5)) for ch in BASE_COLOUR)
        P = [pts[a], pts[b], pts[c]]
        S = [(p[0] * scale + ou, ov - p[1] * scale, p[2]) for p in P]
        S.sort(key=lambda s: s[1])
        (xa, ya, za), (xb, yb, zb), (xc, yc, zc) = S
        if yc - ya < 1e-12:
            continue
        y_start = max(0, int(math.ceil(ya - 0.5)))
        y_end = min(H - 1, int(math.floor(yc - 0.5)))
        for py in range(y_start, y_end + 1):
            sy = py + 0.5
            # long edge a->c, short edges a->b then b->c
            r = (sy - ya) / (yc - ya)
            x1, z1 = xa + (xc - xa) * r, za + (zc - za) * r
            if sy < yb:
                if yb - ya < 1e-12:
                    continue
                r2 = (sy - ya) / (yb - ya)
                x2, z2 = xa + (xb - xa) * r2, za + (zb - za) * r2
            else:
                if yc - yb < 1e-12:
                    x2, z2 = xb, zb
                else:
                    r2 = (sy - yb) / (yc - yb)
                    x2, z2 = xb + (xc - xb) * r2, zb + (zc - zb) * r2
            if x2 < x1:
                x1, x2, z1, z2 = x2, x1, z2, z1
            px0 = max(0, int(math.ceil(x1 - 0.5)))
            px1 = min(W - 1, int(math.floor(x2 - 0.5)))
            if px1 < px0:
                continue
            dz = (z2 - z1) / (x2 - x1) if x2 - x1 > 1e-12 else 0.0
            row = py * W
            z = z1 + (px0 + 0.5 - x1) * dz
            for px in range(px0, px1 + 1):
                i = row + px
                if z > zbuf[i]:
                    zbuf[i] = z
                    cbuf[i] = col
                z += dz

    out = bytearray()
    s2 = supersample * supersample
    for y in range(height):
        for x in range(width):
            r = g = b = 0
            for sy in range(supersample):
                base = (y * supersample + sy) * W + x * supersample
                for sx in range(supersample):
                    c = cbuf[base + sx] or BACKGROUND
                    r += c[0]
                    g += c[1]
                    b += c[2]
            out += bytes(((r + s2 // 2) // s2, (g + s2 // 2) // s2, (b + s2 // 2) // s2))
    return width, height, bytes(out)


def png_bytes(width, height, rgb):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_png(tris, path, **kw):
    """Render a preview and write it with mode "x" (never overwrites)."""
    data = png_bytes(*render_rgb(tris, **kw))
    with open(path, "xb") as f:
        f.write(data)
