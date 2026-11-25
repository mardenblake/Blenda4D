# SPDX-License-Identifier: GPL-3.0-or-later
"""Symmetry in the viewport, the way C4D shows it: while a mesh is being edited with Symmetry on, a see-through plane
marks each mirror (in its axis colour) and the mirror image of the selection is drawn in blue on the other side."""
import itertools

import bmesh
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

# Blender's mirror switch, the local axis it flips, the colour of the axis as Blenda shows it (Blender Y is C4D Z)
AXES = (("use_mirror_x", 0, (0.95, 0.27, 0.27)), ("use_mirror_y", 1, (0.27, 0.5, 0.98)), ("use_mirror_z", 2, (0.4, 0.85, 0.35)))
TWIN = (0.3, 0.62, 1.0)
TOO_MANY = 250000            # faces; past this the mirrored selection is skipped to keep the viewport quick

_handle = []


def _flips(axes):
    """Every combination of the switched-on mirrors, as the per-axis signs a point is multiplied by."""
    out = []
    for n in range(1, len(axes) + 1):
        for combo in itertools.combinations(axes, n):
            s = [1.0, 1.0, 1.0]
            for i in combo:
                s[i] = -1.0
            out.append(Vector(s))
    return out


def geometry(ob, select_mode):
    """(planes, twin) in world space. planes: [(colour, quad corners)]; twin: (triangles, lines, points)."""
    me = ob.data
    on = [(i, col) for attr, i, col in AXES if getattr(me, attr)]
    if not on:
        return [], ([], [], [])
    mw = ob.matrix_world
    box = [Vector(c) for c in ob.bound_box]
    lo = Vector([min(c[k] for c in box) for k in range(3)])
    hi = Vector([max(c[k] for c in box) for k in range(3)])
    size = max(max(hi - lo), 1e-4)
    pad = size * 0.15
    planes = []
    for i, col in on:
        j, k = [a for a in range(3) if a != i]
        quad = []
        for a, b in ((lo[j] - pad, lo[k] - pad), (hi[j] + pad, lo[k] - pad), (hi[j] + pad, hi[k] + pad), (lo[j] - pad, hi[k] + pad)):
            v = Vector((0.0, 0.0, 0.0))
            v[j], v[k] = a, b
            quad.append(mw @ v)
        planes.append((col, quad))

    tris, lines, points = [], [], []
    bm = bmesh.from_edit_mesh(me)
    if len(bm.faces) > TOO_MANY:
        return planes, (tris, lines, points)
    flips = _flips([i for i, _ in on])
    lift = size * 0.002                                  # just off the surface, so the twin isn't lost in it
    if select_mode[2]:
        for f in bm.faces:
            if not f.select:
                continue
            for s in flips:
                n = Vector(f.normal) * s
                vs = [mw @ ((Vector(v.co) * s) + n * lift) for v in f.verts]
                tris += [p for t in range(1, len(vs) - 1) for p in (vs[0], vs[t], vs[t + 1])]
                lines += [p for a, b in zip(vs, vs[1:] + vs[:1]) for p in (a, b)]
    elif select_mode[1]:
        for e in bm.edges:
            if e.select:
                for s in flips:
                    lines += [mw @ (Vector(v.co) * s) for v in e.verts]
    else:
        for v in bm.verts:
            if v.select:
                for s in flips:
                    points.append(mw @ (Vector(v.co) * s))
    return planes, (tris, lines, points)


def _draw():
    ctx = bpy.context
    ob = ctx.edit_object
    if ob is None or ob.type != "MESH" or ctx.mode != "EDIT_MESH":
        return
    if not any(getattr(ob.data, attr) for attr, _, _ in AXES):
        return
    planes, (tris, lines, points) = geometry(ob, ctx.tool_settings.mesh_select_mode)
    if not getattr(ctx.window_manager, "b4d_sym_planes", True):
        planes = []
    region = ctx.region
    flat = gpu.shader.from_builtin("UNIFORM_COLOR")
    wide = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set("LESS_EQUAL")
    for col, quad in planes:
        flat.uniform_float("color", (*col, 0.16))
        batch_for_shader(flat, "TRIS", {"pos": quad}, indices=((0, 1, 2), (0, 2, 3))).draw(flat)
        wide.uniform_float("viewportSize", (region.width, region.height))
        wide.uniform_float("lineWidth", 1.5)
        wide.uniform_float("color", (*col, 0.8))
        batch_for_shader(wide, "LINES", {"pos": [p for a, b in zip(quad, quad[1:] + quad[:1]) for p in (a, b)]}).draw(wide)
    if tris:
        flat.uniform_float("color", (*TWIN, 0.45))
        batch_for_shader(flat, "TRIS", {"pos": tris}).draw(flat)
    if lines:
        wide.uniform_float("viewportSize", (region.width, region.height))
        wide.uniform_float("lineWidth", 2.0)
        wide.uniform_float("color", (*TWIN, 1.0))
        batch_for_shader(wide, "LINES", {"pos": lines}).draw(wide)
    if points:
        dots = gpu.shader.from_builtin("POINT_UNIFORM_COLOR")
        gpu.state.point_size_set(7.0)
        dots.uniform_float("color", (*TWIN, 1.0))
        batch_for_shader(dots, "POINTS", {"pos": points}).draw(dots)
        gpu.state.point_size_set(1.0)
    gpu.state.depth_test_set("NONE")
    gpu.state.depth_mask_set(True)
    gpu.state.blend_set("NONE")


def register():
    if not _handle:
        _handle.append(bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW"))


def unregister():
    while _handle:
        bpy.types.SpaceView3D.draw_handler_remove(_handle.pop(), "WINDOW")
