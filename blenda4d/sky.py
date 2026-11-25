# SPDX-License-Identifier: GPL-3.0-or-later
"""The Sky object, and the Floor.

Blender keeps a scene's sky in its World, which has no place in an object list. Blenda puts one Sky object in front of
it: an object with no geometry that stands for the World.

- Selecting the Sky shows the sky's settings on its Attribute Manager page: a plain colour, an HDRI image (with
  rotation, contrast and saturation), or a sun-and-atmosphere sky ("Physical Sky").
- There is one Sky per scene. Creating another selects the one there is.
- Its Object Manager dots work: the editor dot takes the sky out of the views, the render dot out of final renders,
  the check switches it off in both.
- Deleting the Sky removes the sky: the scene gets back the World it had before the Sky was made.

The settings live on the scene (b4d_sky_*); the World's nodes are rebuilt from them whenever one changes. Everything
the Object Manager can change on its own (deleting the Sky, its dots) is picked up by a timer, never by a depsgraph
callback: making or removing data inside Blender's update callbacks is not safe."""
import math

import bpy

from . import kinds

KIND = "sky"
PREFIX = "Blenda "                       # our nodes in the World; anything else in the tree is left alone
GREY = (0.05, 0.05, 0.05)                # Blender's own default world colour: what a scene with no sky shows
MID_GREY = 0.18
PHYSICAL_SCALE = 0.3
SUN_ROWS = (("Sun Height", "sun_elevation"), ("Sun Direction", "sun_rotation"), ("Sun Size", "sun_size"),
            ("Sun Brightness", "sun_intensity"), ("Altitude", "altitude"), ("Air", "air_density"),
            ("Haze", "aerosol_density"), ("Ozone", "ozone_density"))
_busy = [False]


# --- the Sky object ------------------------------------------------------------------------------------------------

def is_sky(ob):
    return ob is not None and ob.get("b4d_type") == KIND


def find(scene):
    return next((o for o in scene.objects if is_sky(o)), None)


def shown(ob):
    """(in the views, in final renders) for a Sky object."""
    on = ob.get("_b4d_enabled", True)
    return bool(on and not ob.hide_viewport), bool(on and not ob.hide_render)


def _our_world(scene):
    w = scene.b4d_sky_world
    if w is None:
        w = bpy.data.worlds.new("Sky")
        w.use_nodes = True
        w.node_tree.nodes.clear()            # the Sky's own nodes only
        scene.b4d_sky_world = w
    w.use_nodes = True
    return w


def create(context, physical=False):
    """The scene's Sky: the one it has, or a new one. Physical Sky switches it to the sun-and-atmosphere sky."""
    scene = context.scene
    ob = find(scene)
    if ob is None:
        ob = bpy.data.objects.new("Physical Sky" if physical else "Sky", bpy.data.meshes.new("Sky"))
        ob["b4d_type"] = KIND
        ob.lock_location = ob.lock_rotation = ob.lock_scale = (True, True, True)     # a sky has no place or size
        context.collection.objects.link(ob)
    if scene.world is None or scene.world != scene.b4d_sky_world:
        _adopt(scene)
    scene["b4d_sky_physical"] = bool(physical)
    if physical and scene.b4d_sky_image is not None:
        scene.b4d_sky_image = None           # a Physical Sky asked for: the HDRI gives way
    apply(scene)
    sync(scene)
    return ob


def _adopt(scene):
    """The Sky takes over the scene's World; an HDRI it showed becomes the Sky's HDRI. Every HDRI dropped in replaces
    the one before, and nothing old is kept: the dropped World and the replaced picture go once nothing uses them
    (deleting the Sky leaves plain grey, never an earlier HDRI)."""
    old = scene.world
    before = scene.b4d_sky_image
    if old is not None and old != scene.b4d_sky_world:
        env = _environment(old.node_tree) if old.node_tree is not None else None
        if env is not None and env.image is not None:
            scene.b4d_sky_image = env.image          # every HDRI dropped in replaces the Sky's, as in C4D
    scene.b4d_sky_prev_world = None
    scene.world = _our_world(scene)
    if old is not None and old != scene.world and old.users == 0:
        bpy.data.worlds.remove(old)
    if before is not None and before != scene.b4d_sky_image and before.users == 0:
        bpy.data.images.remove(before)


def _environment(tree, seen=()):
    for n in tree.nodes:
        if n.type == "TEX_ENVIRONMENT":
            return n
        if n.type == "GROUP" and n.node_tree is not None and n.node_tree.name not in seen:
            found = _environment(n.node_tree, seen + (n.node_tree.name,))
            if found is not None:
                return found
    return None


def create_sky(context):
    return create(context, physical=False)


def create_physical(context):
    return create(context, physical=True)


def use_image(scene, image):
    """For other modules (an HDRI from the Asset Browser): put the picture on the scene's Sky, making the Sky if needed."""
    with bpy.context.temp_override(scene=scene):
        if find(scene) is None:
            create(bpy.context)
    scene.b4d_sky_image = image


def physical_node(scene):
    w = scene.b4d_sky_world
    return w.node_tree.nodes.get(PREFIX + "Physical Sky") if w is not None and w.node_tree is not None else None


# --- the World's nodes ---------------------------------------------------------------------------------------------

def _node(nt, name, kind, loc, keep):
    n = nt.nodes.get(PREFIX + name)
    if n is None or n.bl_idname != kind:
        if n is not None:
            nt.nodes.remove(n)
        n = nt.nodes.new(kind)
        n.name = PREFIX + name
        n.label = name
    n.location = loc
    keep.add(n.name)
    return n


def apply(scene, render_hidden=False):
    """Build the Sky's World from the page's settings. render_hidden: a final render with the Sky's render dot off
    sees the default grey (no light, no reflections from the sky)."""
    w = scene.b4d_sky_world
    if w is None or find(scene) is None:
        return
    w.use_nodes = True
    nt = w.node_tree
    keep = set()
    L = nt.links
    out = next((n for n in nt.nodes if n.type == "OUTPUT_WORLD" and n.is_active_output), None)
    if out is None:
        out = _node(nt, "Output", "ShaderNodeOutputWorld", (400, 0), keep)
    keep.add(out.name)
    bg = _node(nt, "Background", "ShaderNodeBackground", (0, 0), keep)
    sun_sky = scene.b4d_sky_image is None and scene.get("b4d_sky_physical")
    # the sun-and-atmosphere sky is real daylight, far brighter than a picture: at Intensity 1 it is scaled to look
    # like a normally exposed day
    bg.inputs["Strength"].default_value = scene.b4d_sky_intensity * (PHYSICAL_SCALE if sun_sky else 1.0)
    colour = bg.inputs["Color"]
    for l in list(colour.links):
        L.remove(l)
    image = scene.b4d_sky_image
    if image is not None:
        tc = _node(nt, "Coordinates", "ShaderNodeTexCoord", (-1000, 0), keep)
        mp = _node(nt, "Rotation", "ShaderNodeMapping", (-800, 0), keep)
        env = _node(nt, "HDRI", "ShaderNodeTexEnvironment", (-600, 0), keep)
        env.image = image
        mp.inputs["Rotation"].default_value[2] = scene.b4d_sky_rotation
        L.new(tc.outputs["Generated"], mp.inputs["Vector"])
        L.new(mp.outputs["Vector"], env.inputs["Vector"])
        src = env.outputs["Color"]
        contrast, sat = scene.b4d_sky_contrast / 100.0, scene.b4d_sky_saturation / 100.0
        if abs(contrast) > 1e-6 or abs(sat - 1.0) > 1e-6:
            # Saturation, then contrast as a power curve around mid grey (scale down, gamma, scale back up), so an
            # HDR image's highlights never clip
            hs = _node(nt, "Saturation", "ShaderNodeHueSaturation", (-400, 0), keep)
            dn = _node(nt, "Grade Down", "ShaderNodeVectorMath", (-400, -200), keep)
            gm = _node(nt, "Contrast", "ShaderNodeGamma", (-200, -200), keep)
            up = _node(nt, "Grade Up", "ShaderNodeVectorMath", (-200, 0), keep)
            dn.operation = up.operation = "SCALE"
            hs.inputs["Saturation"].default_value = sat
            dn.inputs["Scale"].default_value = 1.0 / MID_GREY
            gm.inputs["Gamma"].default_value = 2.0 ** contrast
            up.inputs["Scale"].default_value = MID_GREY
            L.new(src, hs.inputs["Color"])
            L.new(hs.outputs["Color"], dn.inputs[0])
            L.new(dn.outputs["Vector"], gm.inputs["Color"])
            L.new(gm.outputs["Color"], up.inputs[0])
            src = up.outputs["Vector"]
        L.new(src, colour)
    elif scene.get("b4d_sky_physical"):
        sun = nt.nodes.get(PREFIX + "Physical Sky")
        if sun is None:
            sun = _node(nt, "Physical Sky", "ShaderNodeTexSky", (-300, 0), keep)
            sun.sky_type = "MULTIPLE_SCATTERING"
            sun.sun_elevation = math.radians(30)
        L.new(sun.outputs["Color"], colour)
    else:
        colour.default_value = (*scene.b4d_sky_color, 1.0)
    keep.add(PREFIX + "Physical Sky")        # its settings are kept while another sky type is in use
    result = bg.outputs["Background"]
    if render_hidden:
        grey = _node(nt, "Render Grey", "ShaderNodeBackground", (200, -300), keep)
        grey.inputs["Color"].default_value = (*GREY, 1.0)
        result = grey.outputs["Background"]
    elif scene.b4d_sky_hide_camera and (image is not None or scene.get("b4d_sky_physical")):
        # Hide from Camera: the sky still lights and reflects, but rays from the camera see the Sky Color
        lp = _node(nt, "Camera Rays", "ShaderNodeLightPath", (0, 400), keep)
        cam = _node(nt, "Camera Color", "ShaderNodeBackground", (0, -200), keep)
        mix = _node(nt, "Camera Mix", "ShaderNodeMixShader", (200, 0), keep)
        cam.inputs["Color"].default_value = (*scene.b4d_sky_color, 1.0)
        L.new(lp.outputs["Is Camera Ray"], mix.inputs["Fac"])
        L.new(result, mix.inputs[1])
        L.new(cam.outputs["Background"], mix.inputs[2])
        result = mix.outputs["Shader"]
    L.new(result, out.inputs["Surface"])
    _octane(scene, nt, out, result, keep)
    for n in [n for n in nt.nodes if n.name.startswith(PREFIX) and n.name not in keep]:
        nt.nodes.remove(n)


def _octane(scene, nt, out, result, keep):
    """The same sky for Octane, which renders a World only through its own environment nodes: the HDRI (turned by
    Rotation), the colour, or a daylight sky, at the Sky's Intensity; with Hide from Camera the camera sees the Sky
    Color while lighting and reflections still come from the sky. Blender's own output then serves Cycles, with a
    copy for EEVEE (Octane reads an output meant for every renderer instead of its own)."""
    try:
        from . import octane_live
        if not octane_live.installed():
            return
    except (ImportError, AttributeError):
        return
    L = nt.links
    image = scene.b4d_sky_image
    try:
        env = _node(nt, "Octane Sky", "OctaneTextureEnvironment", (400, -600), keep)
        oout = _node(nt, "Octane Output", "OctaneEditorWorldOutputNode", (700, -600), keep)
        if image is not None:
            tex = _node(nt, "Octane HDRI", "OctaneRGBImage", (100, -600), keep)
            tex.image = image
            turn = _node(nt, "Octane Rotation", "Octane3DTransformation", (-200, -700), keep)
            turn.inputs["Translation"].default_value = (scene.b4d_sky_rotation / (2.0 * math.pi), 0.0, 0.0)
            L.new(turn.outputs[0], tex.inputs["UV transform"])
            L.new(tex.outputs[0], env.inputs["Texture"])
        elif scene.get("b4d_sky_physical"):
            day = _node(nt, "Octane Daylight", "OctaneDaylightEnvironment", (400, -600), keep)
            L.new(day.outputs[0], oout.inputs[0])
            env = day
        else:
            col = _node(nt, "Octane Color", "OctaneRGBColor", (100, -600), keep)
            col.a_value = tuple(scene.b4d_sky_color)
            L.new(col.outputs[0], env.inputs["Texture"])
        if "Power" in env.inputs:
            env.inputs["Power"].default_value = scene.b4d_sky_intensity
        L.new(env.outputs[0], oout.inputs["Environment"])
        if scene.b4d_sky_hide_camera and (image is not None or scene.get("b4d_sky_physical")):
            vis = _node(nt, "Octane Camera Sky", "OctaneTextureEnvironment", (400, -900), keep)
            vcol = _node(nt, "Octane Camera Color", "OctaneRGBColor", (100, -900), keep)
            vcol.a_value = tuple(scene.b4d_sky_color)
            L.new(vcol.outputs[0], vis.inputs["Texture"])
            vis.inputs["Backplate"].default_value = True
            L.new(vis.outputs[0], oout.inputs["Visible Environment"])
    except (RuntimeError, KeyError, AttributeError) as e:
        print("Blenda 4D: Octane sky", e)
        return
    if out.target == "ALL":
        out.target = "CYCLES"
    ev = _node(nt, "EEVEE Output", "ShaderNodeOutputWorld", (400, 200), keep)
    ev.target = "EEVEE"
    L.new(result, ev.inputs["Surface"])


def _changed(self, context):
    apply(self)


# --- keeping the Sky, the World and the views in step (a timer) ----------------------------------------------------

def _views(scene, state):
    """state: 1 the Sky is shown in the views, 0 it is hidden, -1 there is no Sky (Blender's own defaults)."""
    for win in bpy.context.window_manager.windows:
        if win.scene != scene:
            continue
        for a in win.screen.areas:
            if a.type == "VIEW_3D":
                sh = a.spaces.active.shading
                sh.use_scene_world = state == 1
                sh.use_scene_world_render = state != 0
                a.tag_redraw()


def _removed(scene):
    """The Sky is gone: so is the sky. What is left is a plain medium grey, as in C4D with no Sky object (Joseph,
    Nov 24: never an HDRI from before coming back)."""
    w = scene.b4d_sky_world
    img = scene.b4d_sky_image
    if scene.world is None or scene.world == w:
        grey = bpy.data.worlds.get("No Sky") or bpy.data.worlds.new("No Sky")
        grey.color = (MID_GREY,) * 3
        grey.use_nodes = True
        nt = grey.node_tree
        if not any(n.type == "BACKGROUND" for n in nt.nodes) or any(n.type == "TEX_ENVIRONMENT" for n in nt.nodes):
            nt.nodes.clear()
            out = nt.nodes.new("ShaderNodeOutputWorld"); out.location = (300, 0)
            bg = nt.nodes.new("ShaderNodeBackground"); nt.links.new(bg.outputs[0], out.inputs[0])
        bg = next(n for n in nt.nodes if n.type == "BACKGROUND")
        bg.inputs["Color"].default_value = (MID_GREY, MID_GREY, MID_GREY, 1.0)
        bg.inputs["Strength"].default_value = 1.0
        scene.world = grey
    scene.b4d_sky_world = None
    scene.b4d_sky_prev_world = None
    if "b4d_sky_physical" in scene:
        del scene["b4d_sky_physical"]
    for key in ("b4d_sky_image", "b4d_sky_color", "b4d_sky_intensity", "b4d_sky_rotation", "b4d_sky_contrast",
                "b4d_sky_saturation", "b4d_sky_hide_camera"):
        scene.property_unset(key)                # back to the defaults (no Sky: the update callbacks do nothing)
    if w is not None and w.users == 0:
        bpy.data.worlds.remove(w)
    if img is not None and img.users == 0:
        bpy.data.images.remove(img)              # no old HDRI left in the file


_seen_world = {}                           # scene pointer -> the World it had at the last tick


def _dropped_world(scene):
    """An HDRI world dropped on the view (Blender's own drop of a World asset, Poly Haven's for one) while the scene
    has no Sky: the Sky is made and takes it, and is selected so its controls are at hand, as in C4D. A World the
    scene already had when first seen (a file just opened) is left alone."""
    key = scene.as_pointer()
    w = scene.world
    now = w.as_pointer() if w is not None else 0
    before = _seen_world.get(key)
    _seen_world[key] = now
    if before is None or before == now or w is None or w == scene.b4d_sky_world:
        return False
    if w.node_tree is None or _environment(w.node_tree) is None:
        return False
    try:
        from . import switch
        if not switch.is_on():
            return False
    except Exception:
        pass
    win = next((x for x in bpy.context.window_manager.windows if x.scene == scene), None)
    if win is None:
        return False
    with bpy.context.temp_override(window=win, scene=scene):
        ob = create(bpy.context)
        for o in scene.objects:
            if o.select_get():
                o.select_set(False)
        try:
            ob.select_set(True)
        except RuntimeError:
            pass
        bpy.context.view_layer.objects.active = ob
    _seen_world[key] = scene.world.as_pointer() if scene.world is not None else 0
    for a in win.screen.areas:
        a.tag_redraw()
    return True


def sync(scene):
    ob = find(scene)
    if ob is None and _dropped_world(scene):
        ob = find(scene)
    if ob is None:
        if scene.b4d_sky_world is not None:
            _removed(scene)                      # the World it had comes back: not a new drop
            _seen_world[scene.as_pointer()] = scene.world.as_pointer() if scene.world is not None else 0
        if scene.get("b4d_sky_views", -1) != -1:
            scene["b4d_sky_views"] = -1
            _views(scene, -1)
        return
    if scene.world != scene.b4d_sky_world or scene.b4d_sky_world is None:
        _adopt(scene)                            # a World set some other way (a dropped HDRI, say): the Sky takes it
        apply(scene)
    if shown(ob)[1] and not scene.b4d_sky_hide_camera and scene.render.film_transparent:
        scene.render.film_transparent = False    # the Sky decides what the camera sees: Hide from Camera, not Film Transparent
    state = 1 if shown(ob)[0] else 0
    if scene.get("b4d_sky_views", -1) != state:
        scene["b4d_sky_views"] = state
        _views(scene, state)
    _seen_world[scene.as_pointer()] = scene.world.as_pointer() if scene.world is not None else 0


def _tick():
    if _busy[0]:
        return 0.3
    _busy[0] = True
    try:
        for scene in {w.scene for w in bpy.context.window_manager.windows}:
            sync(scene)
    except Exception as ex:                      # never let the timer die
        print("Blenda 4D sky:", ex)
    finally:
        _busy[0] = False
    return 0.3


@bpy.app.handlers.persistent
def _render_start(scene, *_):
    ob = find(scene)
    if ob is not None and not shown(ob)[1]:
        apply(scene, render_hidden=True)
        scene["b4d_sky_render_held"] = True


@bpy.app.handlers.persistent
def _render_end(scene, *_):
    for sc in bpy.data.scenes:
        if sc.get("b4d_sky_render_held"):
            del sc["b4d_sky_render_held"]
            apply(sc)


# --- the Attribute Manager page ------------------------------------------------------------------------------------

def _unit_row(col, scene, attr, label, unit, active):
    """One layout for every number row, so Intensity, Rotation, Contrast and Saturation line up (fields, units, dots)."""
    row = col.row()
    row.active = active
    sp = row.split(factor=0.93, align=True)
    sp.prop(scene, attr, text=label)
    sp.label(text=unit)


def page_sky(col, ob):
    scene = bpy.context.scene
    image = scene.b4d_sky_image
    sun = physical_node(scene) if image is None and scene.get("b4d_sky_physical") else None
    _unit_row(col, scene, "b4d_sky_color", "Sky Color", "", (image is None and sun is None) or scene.b4d_sky_hide_camera)
    _unit_row(col, scene, "b4d_sky_intensity", "Intensity", "", True)
    if sun is not None:
        col.separator()
        for label, attr in SUN_ROWS:
            if hasattr(sun, attr):
                col.prop(sun, attr, text=label)
        col.prop(sun, "sun_disc", text="Sun Disc")
    col.separator()
    sp = col.split(factor=0.4)
    left = sp.row()
    left.alignment = "RIGHT"
    left.label(text="HDRI")
    sp.template_ID(scene, "b4d_sky_image", open="image.open")
    from . import sky_resolution
    sky_resolution.draw(col, scene)
    _unit_row(col, scene, "b4d_sky_rotation", "Rotation", "", image is not None)
    for label, attr in (("Contrast", "b4d_sky_contrast"), ("Saturation", "b4d_sky_saturation")):
        _unit_row(col, scene, attr, label, "%", image is not None)   # a plain field with "%" after it
    col.separator()
    row = col.row()
    row.active = image is not None or sun is not None
    row.prop(scene, "b4d_sky_hide_camera", text="Hide from Camera")
    col.separator()
    col.operator("blenda4d.sky_nodes", text="Open in Node Editor...", icon="NODETREE")


class BLENDA4D_OT_sky_nodes(bpy.types.Operator):
    """Open the sky's nodes (the scene's World) in a Node Editor window"""
    bl_idname = "blenda4d.sky_nodes"
    bl_label = "Open in Node Editor"

    def execute(self, context):
        if context.scene.world is None:
            return {"CANCELLED"}
        wm = context.window_manager
        for win in wm.windows:                  # a Node Editor already open somewhere: show the World there
            for area in win.screen.areas:
                if area.type == "NODE_EDITOR":
                    self._show(area)
                    return {"FINISHED"}
        before = {w.as_pointer() for w in wm.windows}
        bpy.ops.wm.window_new()
        win = next((w for w in wm.windows if w.as_pointer() not in before), None)
        if win is None:
            return {"CANCELLED"}
        area = max(win.screen.areas, key=lambda a: a.width * a.height)
        area.type = "NODE_EDITOR"
        self._show(area)
        return {"FINISHED"}

    @staticmethod
    def _show(area):
        area.ui_type = "ShaderNodeTree"
        sp = area.spaces.active
        sp.pin = False
        sp.shader_type = "WORLD"
        area.tag_redraw()


# --- an HDRI dropped into the scene ----------------------------------------------------------------------------------

HDRI_EXTENSIONS = (".hdr", ".exr", ".hdri")


class BLENDA4D_OT_sky_from_file(bpy.types.Operator):
    """Light the scene with this HDRI: it goes on the scene's Sky (made if there is none), and the Sky is selected so
    its settings show in the Attribute Manager"""
    bl_idname = "blenda4d.sky_from_file"
    bl_label = "Use as Sky"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"})
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement, options={"SKIP_SAVE", "HIDDEN"})

    def _path(self):
        import os
        if self.directory and len(self.files):
            for f in self.files:                       # several dropped: the first HDRI wins (one sky per scene)
                if f.name.lower().endswith(HDRI_EXTENSIONS):
                    return os.path.join(self.directory, f.name)
        return self.filepath

    def execute(self, context):
        path = self._path()
        if not path or not path.lower().endswith(HDRI_EXTENSIONS):
            self.report({"WARNING"}, "Drop an HDRI (.hdr or .exr)")
            return {"CANCELLED"}
        try:
            image = bpy.data.images.load(path, check_existing=True)
        except RuntimeError as e:
            self.report({"ERROR"}, f"Could not read {path}: {e}")
            return {"CANCELLED"}
        ob = create(context)
        before = context.scene.b4d_sky_image
        context.scene.b4d_sky_image = image
        if before is not None and before != image and before.users == 0:
            bpy.data.images.remove(before)           # nothing old kept
        for o in context.selected_objects:
            o.select_set(False)
        try:
            ob.select_set(True)
        except RuntimeError:
            pass
        context.view_layer.objects.active = ob
        for area in context.screen.areas:
            area.tag_redraw()
        self.report({"INFO"}, f"Sky: {image.name}")
        return {"FINISHED"}

    def invoke(self, context, event):
        return self.execute(context)


class BLENDA4D_FH_sky_drop(bpy.types.FileHandler):
    """An .hdr or .exr dragged onto the 3D view or the Object Manager becomes the scene's Sky, as in C4D"""
    bl_idname = "BLENDA4D_FH_sky_drop"
    bl_label = "Use as Sky"
    bl_import_operator = "blenda4d.sky_from_file"
    bl_file_extensions = ";".join(HDRI_EXTENSIONS)

    @classmethod
    def poll_drop(cls, context):
        area = context.area
        if area is None:
            return False
        if area.type == "VIEW_3D":
            return True
        from . import roles
        return roles.role(area) == "OBJECTS"


SKY_DROP_CLASSES = (BLENDA4D_OT_sky_from_file, BLENDA4D_FH_sky_drop)


# --- the Floor -----------------------------------------------------------------------------------------------------

FLOOR_SIZE = 200.0                         # metres: reaches the horizon in most shots


def create_floor(context):
    h = FLOOR_SIZE / 2
    me = bpy.data.meshes.new("Floor")
    me.from_pydata([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)], [], [(0, 1, 2, 3)])
    me.uv_layers.new(name="UVMap")
    ob = bpy.data.objects.new("Floor", me)
    ob["b4d_type"] = "floor"
    ob.location = context.scene.cursor.location
    context.collection.objects.link(ob)
    return ob


# --- registration --------------------------------------------------------------------------------------------------

def register():
    bpy.utils.register_class(BLENDA4D_OT_sky_nodes)
    for c in SKY_DROP_CLASSES:
        bpy.utils.register_class(c)
    from . import sky_resolution
    sky_resolution.register()
    S = bpy.types.Scene
    S.b4d_sky_world = bpy.props.PointerProperty(description='The world the Sky object made', type=bpy.types.World)
    S.b4d_sky_prev_world = bpy.props.PointerProperty(description='The world in use before the Sky object, put back when the Sky is deleted', type=bpy.types.World)
    S.b4d_sky_color = bpy.props.FloatVectorProperty(
        name="Sky Color", subtype="COLOR", size=3, min=0.0, max=1.0, default=GREY, update=_changed,
        description="The sky's colour when it has no HDRI, and what the camera sees with Hide from Camera on")
    S.b4d_sky_intensity = bpy.props.FloatProperty(
        name="Intensity", default=1.0, min=0.0, soft_max=10.0, update=_changed,
        description="How bright the sky is, and how much light it gives the scene")
    S.b4d_sky_image = bpy.props.PointerProperty(
        name="HDRI", type=bpy.types.Image, update=_changed,
        description="A panoramic image that wraps around the scene and lights it")
    S.b4d_sky_rotation = bpy.props.FloatProperty(
        name="Rotation", subtype="ANGLE", default=0.0, update=_changed,
        description="Turn the HDRI around the vertical axis")
    S.b4d_sky_contrast = bpy.props.FloatProperty(
        name="Contrast", subtype="NONE", default=0.0, min=-100.0, max=100.0, precision=0, update=_changed,
        description="More or less contrast in the HDRI, around mid grey; bright spots never clip")
    S.b4d_sky_saturation = bpy.props.FloatProperty(
        name="Saturation", subtype="NONE", default=100.0, min=0.0, max=200.0, precision=0, update=_changed,
        description="0 % is grey, 100 % the HDRI as it is, above that more colourful")
    S.b4d_sky_hide_camera = bpy.props.BoolProperty(
        name="Hide from Camera", default=False, update=_changed,
        description="The sky still lights and reflects in the scene, but the camera sees the Sky Color behind it")
    tabs = [("OBJECT", "Sky")]
    kinds.add(KIND, "Sky", "WORLD", create=create_sky, tabs=tabs, pages={"OBJECT": page_sky}, own_tabs_only=True,
              titles={"OBJECT": "Sky Properties"}, describe="The scene's sky: a colour or an HDRI image around everything")
    kinds.add("physical_sky", "Physical Sky", "LIGHT_SUN", create=create_physical,
              describe="A sky with a sun and an atmosphere, worked out from the sun's height")
    kinds.add("floor", "Floor", "MESH_PLANE", create=create_floor, describe="A large flat ground plane")
    kinds.create_button("environment", "Sky", KIND)
    kinds.create_button("environment", "Physical Sky", "physical_sky")
    kinds.create_button("environment", "Floor", "floor")
    kinds.menu_item("Create", "Sky", KIND)
    kinds.menu_item("Create", "Physical Sky", "physical_sky")
    kinds.menu_item("Create", "Floor", "floor")
    _order_environment()
    for h, f in ((bpy.app.handlers.render_init, _render_start), (bpy.app.handlers.render_complete, _render_end),
                 (bpy.app.handlers.render_cancel, _render_end), (bpy.app.handlers.frame_change_post, _frame)):
        if f not in h:
            h.append(f)
    if not bpy.app.background and not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)


@bpy.app.handlers.persistent
def _frame(scene, *_args):
    """Keyframed Sky settings reach the World on every frame: an animated setting never runs its update function,
    so the World (and Octane's sky) would keep the value it last had by hand. Only values change here, no nodes are
    made or removed (safe while rendering)."""
    w = getattr(scene, "b4d_sky_world", None)
    if w is None or w.node_tree is None or find(scene) is None:
        return
    nt = w.node_tree
    sun_sky = scene.b4d_sky_image is None and scene.get("b4d_sky_physical")
    n = nt.nodes.get(PREFIX + "Background")
    if n is not None:
        n.inputs["Strength"].default_value = scene.b4d_sky_intensity * (PHYSICAL_SCALE if sun_sky else 1.0)
    n = nt.nodes.get(PREFIX + "Rotation")
    if n is not None:
        n.inputs["Rotation"].default_value[2] = scene.b4d_sky_rotation
    n = nt.nodes.get(PREFIX + "Octane Sky")
    if n is not None and "Power" in n.inputs:
        n.inputs["Power"].default_value = scene.b4d_sky_intensity
    n = nt.nodes.get(PREFIX + "Octane Rotation")
    if n is not None:
        n.inputs["Translation"].default_value = (scene.b4d_sky_rotation / (2.0 * math.pi), 0.0, 0.0)


def _order_environment():
    """Physical Sky sits next to Sky in the environment group."""
    from . import bars
    members = bars.GROUPS.get("environment")
    if not members:
        return
    labels = [m[0] for m in members]
    if "Sky" in labels and "Physical Sky" in labels:
        ph = members.pop(labels.index("Physical Sky"))
        members.insert([m[0] for m in members].index("Sky") + 1, ph)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for h, f in ((bpy.app.handlers.render_init, _render_start), (bpy.app.handlers.render_complete, _render_end),
                 (bpy.app.handlers.render_cancel, _render_end), (bpy.app.handlers.frame_change_post, _frame)):
        if f in h:
            h.remove(f)
    for k in (KIND, "physical_sky", "floor"):
        kinds.KINDS.pop(k, None)
    S = bpy.types.Scene
    for n in ("b4d_sky_world", "b4d_sky_prev_world", "b4d_sky_color", "b4d_sky_intensity", "b4d_sky_image",
              "b4d_sky_rotation", "b4d_sky_contrast", "b4d_sky_saturation", "b4d_sky_hide_camera"):
        if hasattr(S, n):
            delattr(S, n)
    if hasattr(bpy.types, "BLENDA4D_OT_sky_nodes"):
        bpy.utils.unregister_class(BLENDA4D_OT_sky_nodes)
    for c in reversed(SKY_DROP_CLASSES):
        if c.is_registered:
            bpy.utils.unregister_class(c)
    from . import sky_resolution
    if hasattr(bpy.types, "BLENDA4D_OT_sky_resolution"):
        sky_resolution.unregister()
