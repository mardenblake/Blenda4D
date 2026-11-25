# SPDX-License-Identifier: GPL-3.0-or-later
"""Material Manager: turn the selected materials into Octane materials, or Octane materials into Blender's own.

To Octane uses the Octane add-on's own converter (it knows Principled BSDF and Blender's other shaders) on each
material, so no object has to use it. To Blender reads the Octane material's main settings (colour, metal,
roughness, specular, coating, sheen, transmission, opacity, emission) and the image textures on them, normal and bump
maps included, into a Principled BSDF. Each material keeps its name, its place in the Material Manager and every
object and tag that uses it; Undo brings the old one back."""
import bpy

from . import matman


def _octane_node(mat):
    """The Octane material node the material's output shows, or None (a Blender material)."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    out = mat.node_tree.get_output_node("ALL")
    if out is None:
        out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None or not out.inputs[0].is_linked:
        return None
    node = out.inputs[0].links[0].from_node
    return node if node.bl_idname.startswith("Octane") else None


def is_octane(mat):
    return _octane_node(mat) is not None


def octane_ready():
    from . import octane_live
    return octane_live.installed()


def _targets(context, to_octane):
    """The selected materials there is something to do for: Blender ones to convert to Octane, or Octane ones back."""
    return [m for m in matman.selected(context) if m.library is None and is_octane(m) != to_octane]


def _swap(old, new):
    """new takes old's place: name, Material Manager place and selection, every user."""
    name = old.name
    for k in (matman.ORDER, matman.SEL):
        if k in old:
            new[k] = old[k]
    old.user_remap(new)
    old.name = name + " (old)"
    new.name = name
    wm = bpy.context.window_manager
    if wm.b4d_material in (name, old.name):
        wm.b4d_material = new.name
    bpy.data.materials.remove(old)


# --- Blender -> Octane --------------------------------------------------------------------------------------------------------------
SHADERS = ("BSDF_PRINCIPLED", "BSDF_ANISOTROPIC", "BSDF_GLOSSY", "BSDF_METALLIC", "BSDF_DIFFUSE", "EMISSION",
           "BSDF_GLASS", "BSDF_REFRACTION", "BSDF_TRANSLUCENT", "SUBSURFACE_SCATTERING", "BSDF_SHEEN")


def _shaders(tree, depth=0):
    """Every shader node in a tree, groups opened (old materials keep their shader in a node group)."""
    out = []
    if tree is None or depth > 6:
        return out
    for n in tree.nodes:
        if n.type in SHADERS:
            out.append(n)
        elif n.type == "GROUP" and n.node_tree is not None:
            out += _shaders(n.node_tree, depth + 1)
    return out


def _bl_read(node, name, default):
    s = node.inputs.get(name)
    if s is None:
        return default, None
    img = _image_up(s)
    v = _plain(getattr(s, "default_value", None))
    return (default if v is None else v), img


def _ours_to_octane(mat):
    """Octane's converter found nothing to convert (a shader inside a node group, say): an Octane Universal material
    from the main shader's colour, metal, roughness, anisotropy and image textures."""
    found = _shaders(mat.node_tree)
    if not found:
        return None
    rank = {t: i for i, t in enumerate(SHADERS)}
    main = min(found, key=lambda n: rank.get(n.type, 99))
    metal_kinds = ("BSDF_ANISOTROPIC", "BSDF_GLOSSY", "BSDF_METALLIC")
    color, color_img = _bl_read(main, "Base Color" if main.type == "BSDF_PRINCIPLED" else "Color", (0.8, 0.8, 0.8))
    metal, metal_img = _bl_read(main, "Metallic", 1.0 if main.type in metal_kinds else 0.0)
    rough, rough_img = _bl_read(main, "Roughness", 0.3)
    aniso, _ = _bl_read(main, "Anisotropy" if main.type != "BSDF_PRINCIPLED" else "Anisotropic", 0.0)
    rot, _ = _bl_read(main, "Rotation" if main.type != "BSDF_PRINCIPLED" else "Anisotropic Rotation", 0.0)
    if main.type in metal_kinds and not aniso:          # several anisotropic shaders mixed: brushed metal
        aniso = 0.8 if sum(1 for n in found if n.type in metal_kinds) > 1 else aniso   # (Blender 4+ files them as Glossy)
    new = mat.copy()
    nt = new.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (400, 0)
    u = nt.nodes.new("OctaneUniversalMaterial")
    nt.links.new(u.outputs[0], out.inputs["Surface"])

    def put(name, value, img=None, color=False):
        s = u.inputs.get(name)
        if s is None:
            return
        if img is not None:
            t = nt.nodes.new("OctaneRGBImage" if color else "OctaneGreyscaleImage")
            t.image = img; t.location = (-400, -200 * len(nt.nodes))
            nt.links.new(t.outputs[0], s)
            return
        try:
            s.default_value = _rgb(value) if color else (_lum(value) if not isinstance(value, float) else value)
        except (TypeError, ValueError, AttributeError):
            pass
    put("Albedo", color, color_img, color=True)
    put("Metallic", metal, metal_img)
    put("Roughness", rough, rough_img)
    put("Anisotropy", min(1.0, abs(_lum(aniso))) if aniso else 0.0)
    put("Rotation", _lum(rot) if rot else 0.0)
    return new


def _shows_octane(mat):
    node = _octane_node(mat)
    return node is not None


def to_octane(mat):
    from octane.utils.converters import common
    src_out = mat.node_tree.get_output_node("ALL") if mat.node_tree else None
    new = None
    if src_out is not None and common.is_material_convertable(mat):
        new = mat.copy()
        new.node_tree.nodes.clear()
        try:
            node = common.convert_to_octane_node(new.node_tree, None, None, mat.node_tree, src_out, None)
        except Exception as e:
            print("Blenda 4D: Octane converter", mat.name, e)
            node = None
        if node is None or not _shows_octane(new):    # nothing came out: never swap in an empty material
            bpy.data.materials.remove(new)
            new = None
    if new is None:
        new = _ours_to_octane(mat)
        if new is None or not _shows_octane(new):
            if new is not None:
                bpy.data.materials.remove(new)
            return False
    try:
        from octane.utils import utility
        utility.beautifier_nodetree_layout_by_owner(new)
    except Exception:
        pass
    _swap(mat, new)
    return True


# --- Octane -> Blender --------------------------------------------------------------------------------------------------------------
def _image_up(sock, depth=0):
    """The first image found upstream of a socket (through mixes, colour corrections and the like), or None."""
    if sock is None or not sock.is_linked or depth > 8:
        return None
    node = sock.links[0].from_node
    img = getattr(node, "image", None)
    if isinstance(img, bpy.types.Image):
        return img
    for s in node.inputs:
        found = _image_up(s, depth + 1)
        if found is not None:
            return found
    return None


def _plain(v):
    if isinstance(v, (int, float, bool)):
        return float(v)
    try:
        t = tuple(v)
    except TypeError:
        return None
    if t and all(isinstance(c, (int, float)) for c in t):
        return t
    return None


def _read(node, *names):
    """(value, image) of the first input of node named in names: its value (a number or colour) and an image texture
    on it, if any. A linked input with no image reads the first plain value of the node feeding it."""
    for n in names:
        s = node.inputs.get(n)
        if s is None:
            continue
        img = _image_up(s)
        v = _plain(getattr(s, "default_value", None))
        if s.is_linked and img is None:
            up = s.links[0].from_node
            v = next((_plain(getattr(i, "default_value", None)) for i in up.inputs
                      if _plain(getattr(i, "default_value", None)) is not None), v)
        return v, img
    return None, None


def _lum(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, float):
        return v
    c = tuple(v)[:3]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] if len(c) == 3 else float(c[0])


def _rgb(v, default=(0.8, 0.8, 0.8)):
    if v is None:
        return default
    if isinstance(v, float):
        return (v, v, v)
    c = tuple(v)[:3]
    return c if len(c) == 3 else (c[0], c[0], c[0])


def _main(node, depth=0):
    """Mixes and layers: the first real material inside."""
    if depth > 6:
        return node
    if node.bl_idname in ("OctaneMixMaterial", "OctaneLayeredMaterial", "OctaneCompositeMaterial", "OctaneClippingMaterial"):
        for s in node.inputs:
            if s.is_linked and s.links[0].from_node.bl_idname.endswith("Material"):
                return _main(s.links[0].from_node, depth + 1)
    return node


def _emission(node):
    """(colour, strength, image) of an emission on the material, or None."""
    s = node.inputs.get("Emission")
    if s is None or not s.is_linked:
        return None
    em = s.links[0].from_node
    power = _plain(getattr(em.inputs.get("Power"), "default_value", None)) if em.inputs.get("Power") else None
    col, img = _read(em, "Texture", "Color", "Emission")
    return _rgb(col, (1.0, 1.0, 1.0)), float(power) if isinstance(power, float) else 1.0, img


def octane_settings(mat):
    """What the Octane material looks like, as Principled BSDF settings and textures."""
    node = _main(_octane_node(mat))
    kind = node.bl_idname
    st = {}
    base, base_img = _read(node, "Albedo", "Diffuse", "Base color", "Color", "Transmission", "Reflection", "Specular")
    st["Base Color"] = (_rgb(base), base_img)
    metal, metal_img = _read(node, "Metallic", "Metalness")
    st["Metallic"] = (1.0 if kind == "OctaneMetallicMaterial" else _lum(metal), metal_img)
    rough, rough_img = _read(node, "Roughness", "Specular roughness")
    st["Roughness"] = (_lum(rough, 0.3), rough_img)
    if kind == "OctaneUniversalMaterial":
        spec, _ = _read(node, "Specular")
        st["Specular IOR Level"] = (min(1.0, _lum(spec, 1.0) * 0.5), None)
    ior, _ = _read(node, "Dielectric IOR", "Index of refraction", "IOR", "Index")
    if isinstance(ior, float) and ior > 1.0:
        st["IOR"] = (ior, None)
    coat, _ = _read(node, "Coating", "Coat")
    if _lum(coat) > 0.01:
        st["Coat Weight"] = (min(1.0, _lum(coat)), None)
        cr, _ = _read(node, "Coating roughness", "Coat roughness")
        st["Coat Roughness"] = (_lum(cr, 0.03), None)
    sheen, _ = _read(node, "Sheen")
    if _lum(sheen) > 0.01:
        st["Sheen Weight"] = (1.0, None)
        st["Sheen Tint"] = (_rgb(sheen), None)
        sr, _ = _read(node, "Sheen roughness")
        st["Sheen Roughness"] = (_lum(sr, 0.2), None)
    trans, trans_img = _read(node, "Transmission")
    if kind in ("OctaneSpecularMaterial", "OctaneStandardSurfaceMaterial") or (kind == "OctaneUniversalMaterial" and _lum(trans) > 0.01):
        st["Transmission Weight"] = (1.0 if kind == "OctaneSpecularMaterial" else min(1.0, _lum(trans)), None)
        if kind == "OctaneSpecularMaterial":
            st["Base Color"] = (_rgb(trans, (1.0, 1.0, 1.0)), trans_img)
    aniso, _ = _read(node, "Anisotropy")
    if isinstance(aniso, float) and abs(aniso) > 0.001:
        st["Anisotropic"] = (min(1.0, abs(aniso)), None)
        rot, _ = _read(node, "Rotation")
        st["Anisotropic Rotation"] = (_lum(rot) % 1.0, None)
    op, op_img = _read(node, "Opacity")
    if op_img is not None or (isinstance(op, float) and op < 0.999):
        st["Alpha"] = (_lum(op, 1.0), op_img)
    _n, normal_img = _read(node, "Normal")
    _b, bump_img = _read(node, "Bump")
    bh, _ = _read(node, "Bump height")
    em = _emission(node)
    return st, normal_img, bump_img, (bh if isinstance(bh, float) else 0.01), em


def to_blender(mat):
    if not is_octane(mat):
        return False
    st, normal_img, bump_img, bump_h, em = octane_settings(mat)
    new = bpy.data.materials.new(mat.name + " (Blender)")
    new.use_nodes = True
    nt = new.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (400, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (0, 0)
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    y = 300

    def tex(img, color=True):
        nonlocal y
        t = nt.nodes.new("ShaderNodeTexImage"); t.image = img; t.location = (-420, y); y -= 280
        if not color and img.colorspace_settings.name.lower().startswith("srgb"):
            try:
                img.colorspace_settings.name = "Non-Color"
            except TypeError:
                pass
        return t

    for name, (v, img) in st.items():
        s = bsdf.inputs.get(name)
        if s is None:
            continue
        if img is not None:
            t = tex(img, color=name == "Base Color")
            nt.links.new(t.outputs["Color"], s)
            continue
        if v is None:
            continue
        try:
            if s.type == "RGBA":
                s.default_value = (*_rgb(v), 1.0)
            else:
                s.default_value = _lum(v) if not isinstance(v, float) else v
        except (TypeError, ValueError):
            pass
    if normal_img is not None:
        t = tex(normal_img, color=False)
        nm = nt.nodes.new("ShaderNodeNormalMap"); nm.location = (-180, y + 280)
        nt.links.new(t.outputs["Color"], nm.inputs["Color"]); nt.links.new(nm.outputs[0], bsdf.inputs["Normal"])
    elif bump_img is not None:
        t = tex(bump_img, color=False)
        bp = nt.nodes.new("ShaderNodeBump"); bp.location = (-180, y + 280)
        bp.inputs["Distance"].default_value = max(bump_h, 1e-4)
        nt.links.new(t.outputs["Color"], bp.inputs["Height"]); nt.links.new(bp.outputs[0], bsdf.inputs["Normal"])
    if em is not None:
        col, strength, img = em
        if img is not None:
            nt.links.new(tex(img).outputs["Color"], bsdf.inputs["Emission Color"])
        else:
            bsdf.inputs["Emission Color"].default_value = (*col, 1.0)
        bsdf.inputs["Emission Strength"].default_value = strength
    if "Alpha" in st:
        try:
            new.surface_render_method = "BLENDED"
        except (AttributeError, TypeError):
            pass
    new.diffuse_color = (*_rgb(st["Base Color"][0]), 1.0)
    _carry_keys(mat, _main(_octane_node(mat)), new, bsdf)
    _swap(mat, new)
    return True


# Octane input -> Principled BSDF input, for keyframes (a title fading in on Opacity must still fade in on Alpha)
_KEYED = {"Opacity": "Alpha", "Albedo": "Base Color", "Diffuse": "Base Color", "Roughness": "Roughness",
          "Specular roughness": "Roughness", "Metallic": "Metallic", "Coating roughness": "Coat Roughness",
          "Dielectric IOR": "IOR", "Index of refraction": "IOR"}


def _carry_keys(mat, node, new, bsdf):
    """Copy the Octane material's keyframes onto the matching Principled BSDF inputs of the new material."""
    ad = mat.node_tree.animation_data if mat.node_tree else None
    if node is None or ad is None or ad.action is None:
        return
    try:
        from bpy_extras import anim_utils
        cb = anim_utils.action_get_channelbag_for_slot(ad.action, ad.action_slot)
        fcs = list(cb.fcurves) if cb else []
    except Exception:
        fcs = list(getattr(ad.action, "fcurves", []))
    head = f'nodes["{node.name}"].inputs['
    for fc in fcs:
        if not fc.data_path.startswith(head) or not fc.data_path.endswith("].default_value"):
            continue
        try:
            sock = node.inputs[int(fc.data_path[len(head):].split("]")[0])]
        except (ValueError, IndexError):
            continue
        target = bsdf.inputs.get(_KEYED.get(sock.name, ""))
        if target is None:
            continue
        idx = list(bsdf.inputs).index(target)
        path = f'nodes["{bsdf.name}"].inputs[{idx}].default_value'
        is_color = target.type == "RGBA"
        for k in fc.keyframe_points:
            f, v = k.co
            if is_color:
                cur = list(target.default_value); cur[fc.array_index] = v; target.default_value = cur
                new.node_tree.keyframe_insert(path, index=fc.array_index, frame=f)
            else:
                target.default_value = v
                new.node_tree.keyframe_insert(path, frame=f)
        if target.name == "Alpha":
            try:
                new.surface_render_method = "BLENDED"
            except (AttributeError, TypeError):
                pass


# --- the buttons -------------------------------------------------------------------------------------------------------------------
class _Convert:
    bl_options = {"REGISTER", "UNDO"}
    octane = False

    def execute(self, context):
        mats = _targets(context, self.octane)
        if not mats:
            self.report({"INFO"}, "Select materials that are not Octane materials yet" if self.octane
                        else "Select Octane materials")
            return {"CANCELLED"}
        done, failed = 0, []
        for m in mats:
            name = m.name
            try:
                ok = to_octane(m) if self.octane else to_blender(m)
            except Exception as e:
                print("Blenda 4D: convert", name, e)
                ok = False
            if ok:
                done += 1
            else:
                failed.append(name)
        msg = f"{done} material{'s' if done != 1 else ''} converted"
        if failed:
            msg += f"; could not convert {', '.join(failed[:4])}{'...' if len(failed) > 4 else ''}"
        self.report({"WARNING"} if failed else {"INFO"}, msg)
        for w in context.window_manager.windows:
            for a in w.screen.areas:
                a.tag_redraw()
        return {"FINISHED"} if done else {"CANCELLED"}


class BLENDA4D_OT_mm_to_octane(_Convert, bpy.types.Operator):
    """Convert the selected materials into Octane materials (Octane's own converter); objects keep them"""
    bl_idname = "blenda4d.mm_to_octane"
    bl_label = "Convert to Octane Materials"
    octane = True

    @classmethod
    def poll(cls, context):
        if not octane_ready():
            cls.poll_message_set("Octane is not installed")
            return False
        return True


class BLENDA4D_OT_mm_to_blender(_Convert, bpy.types.Operator):
    """Convert the selected Octane materials into Blender materials (Principled BSDF with their colours, settings and
    textures), for Cycles and EEVEE"""
    bl_idname = "blenda4d.mm_to_blender"
    bl_label = "Convert to Blender Materials"
    octane = False


CLASSES = (BLENDA4D_OT_mm_to_octane, BLENDA4D_OT_mm_to_blender)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
