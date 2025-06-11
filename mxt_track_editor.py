bl_info = {
    "name": "MXT Racetrack Road Creator",
    "author": "Twilight",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "3D View > Sidebar (N-Panel) > MXT Road Creator",
    "description": "Design a racetrack for Maxx Throttle!",
    "warning": "",
    "doc_url": "",
    "category": "Object",
}
import bpy
import time
import numpy as np
import bmesh
from bpy.props import (
    FloatProperty,
    FloatVectorProperty,
    EnumProperty,
    PointerProperty,
    StringProperty,
    BoolProperty,
    IntProperty,
)
from bpy.types import (
    PropertyGroup,
    Operator,
    Panel,
)
import mathutils
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, Quaternion, Matrix
import math
from bpy.app.handlers import persistent
from bpy.props import CollectionProperty
from contextlib import contextmanager

@contextmanager
def _no_undo():
    prefs = bpy.context.preferences.edit
    orig  = prefs.use_global_undo
    try:
        if orig:
            prefs.use_global_undo = False
        yield
    finally:
        if orig:
            prefs.use_global_undo = True

def _disallow_deletion(obj):
    if obj and hasattr(obj, "can_user_delete"):
        obj.can_user_delete = False
ROAD_SHAPE_TYPE_ITEMS = [
    ('FLAT', "Flat", "Flat Road Segment"),
    ('CYLINDER', "Cylinder", "Cylindrical Shape (exterior"),
    ('PIPE', "Pipe", "Pipe Shape (interior)"),
    ('CYLINDER_OPEN', "Open Cylinder", "Open Cylindrical Shape"),
    ('PIPE_OPEN', "Open Pipe", "Open Pipe Shape (interior)"),
]

class MXTEmbed(bpy.types.PropertyGroup):
    label:          StringProperty(name="Label", default="Embed")
    helper:         PointerProperty(type=bpy.types.Object)
    start_t:        FloatProperty(name="Start t", min=0.0, max=1.0, default=0.25)
    end_t:          FloatProperty(name="End t",   min=0.0, max=1.0, default=0.75)
    embed_type: EnumProperty(
        name="Type",
        items=[('RECHARGE',"Recharge",""), ('DIRT',"Dirt",""),
               ('ICE',"Ice",""), ('LAVA',"Lava",""), ('HOLE',"Hole","")],
        default='RECHARGE')
class MXTCheckpoint(bpy.types.PropertyGroup):
    
    start_t:    FloatProperty(name="t₀", min=0.0, max=1.0)
    end_t:      FloatProperty(name="t₁", min=0.0, max=1.0)
    
    pos_start:  FloatVectorProperty(size=3)
    pos_end:    FloatVectorProperty(size=3)
    basis_start:    FloatVectorProperty(size=9) 
    basis_end:  FloatVectorProperty(size=9)
    x_rad_start:    FloatProperty()
    x_rad_end:  FloatProperty()
    y_rad_start:    FloatProperty()
    y_rad_end:  FloatProperty()
    distance:   FloatProperty()
class MXTModulation(PropertyGroup):
    label: StringProperty(name="Name", default="Modulation")
    helper: PointerProperty(type=bpy.types.Object)
class MXT_UL_Modulations(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "label", text="", emboss=False, icon='FCURVE')
mxt_roads_pending_visual_update = set()
mxt_timer_is_active = False
_build_in_progress  = False     
_ignore_updates     = False     
class MXTRoad_ControlPointData(PropertyGroup):
    is_mxt_control_point: BoolProperty(default=False)
    time: FloatProperty(
        name="Time", default=0.0, min=0.0, max=1.0,
        description="Normalized time (0-1) for this control point. Modifying this flags for visual update.",
        update=lambda self, context: schedule_road_parent_visual_update(self.id_data, context)
    )
    handle_in_length: FloatProperty(
        name="Handle In Length", default=100.0, min=0.001,
        update=lambda self, context: schedule_road_parent_visual_update(self.id_data, context)
    )
    handle_out_length: FloatProperty(
        name="Handle Out Length", default=100.0, min=0.001,
        update=lambda self, context: schedule_road_parent_visual_update(self.id_data, context)
    )
    rotation_ease_factor_channel: FloatProperty(
        name="Rotation Ease Factor", subtype='NONE', unit='NONE', default=0.0,
        description="Animatable channel for rotation easing F-Curve (0-1 output expected)",
        options={'ANIMATABLE'}
    )
    scale_ease_factor_channel: FloatProperty(
        name="Scale Ease Factor", subtype='NONE', unit='NONE', default=0.0,
        description="Animatable channel for scale easing F-Curve (0-1 output expected)",
        options={'ANIMATABLE'}
    )
    twist_ease_factor_channel: FloatProperty(
        name="Twist Ease Factor", subtype='NONE', unit='NONE', default=0.0,
        description="Animatable channel for twist easing F-Curve (0-1 output expected)",
        options={'ANIMATABLE'}
    )

class MXTRoad_LineHandleData(PropertyGroup):
    is_mxt_line_handle: BoolProperty(default=False)
    
    rotation_ease_factor_channel: FloatProperty(
        name="Rotation Ease Factor", subtype='NONE', unit='NONE', default=0.0,
        description="Animatable channel for rotation easing F-Curve (0-1 output expected)",
        options={'ANIMATABLE'}
    )
    scale_ease_factor_channel: FloatProperty(
        name="Scale Ease Factor", subtype='NONE', unit='NONE', default=0.0,
        description="Animatable channel for scale easing F-Curve (0-1 output expected)",
        options={'ANIMATABLE'}
    )


def mxt_segment_type_update(self, context):
    if get_active_mxt_road_segment_parent(context):
        
         bpy.ops.mxt_road.convert_segment_type('EXEC_DEFAULT')
    return None


_object_to_select_deferred = None

def _deferred_select():
    global _object_to_select_deferred
    obj_to_select = _object_to_select_deferred
    _object_to_select_deferred = None  

    if obj_to_select and bpy.data.objects.get(obj_to_select.name):
        try:
            
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            
            bpy.ops.object.select_all(action='DESELECT')
            obj_to_select.select_set(True)
            bpy.context.view_layer.objects.active = obj_to_select
        except Exception as e:
            print(f"MXT Deferred Select Error: {e}")
    return None  

def schedule_deferred_select(obj):
    global _object_to_select_deferred
    _object_to_select_deferred = obj
    
    if not bpy.app.timers.is_registered(_deferred_select):
        bpy.app.timers.register(_deferred_select)

def mxt_active_mod_index_update(self, context):
    if 0 <= self.active_mod_index < len(self.modulations):
        mod = self.modulations[self.active_mod_index]
        if mod.helper:
            schedule_deferred_select(mod.helper)

def mxt_active_embed_idx_update(self, context):
    if 0 <= self.active_embed_idx < len(self.embeds):
        emb = self.embeds[self.active_embed_idx]
        if emb.helper:
            schedule_deferred_select(emb.helper)

def mxt_road_shape_type_update(self, context):
    
    parent = self.id_data 

    
    if self.road_shape_type in ('CYLINDER_OPEN', 'PIPE_OPEN'):
        
        if parent.name in _openness_helper_to_destroy:
             _openness_helper_to_destroy.remove(parent.name)
        _openness_helper_to_create.add(parent.name)
    
    else:
        
        if parent.name in _openness_helper_to_create:
            _openness_helper_to_create.remove(parent.name)
        _openness_helper_to_destroy.add(parent.name)
    
    
    schedule_mesh_build(parent)

class MXTRoad_RoadSegmentOverallProperties(PropertyGroup):
    is_mxt_road_segment_parent: BoolProperty(default=False)
    curve_matrix_helper_empty: PointerProperty(type=bpy.types.Object, poll=lambda self, object: object.type == 'EMPTY')
    visual_guide_curve: PointerProperty(type=bpy.types.Object, poll=lambda self, object: object.type == 'CURVE')
    road_shape_type: EnumProperty(name="Road Shape Type", items=ROAD_SHAPE_TYPE_ITEMS, default='FLAT',
    update=mxt_road_shape_type_update)
    horiz_subdivs: IntProperty(
        name="Horizontal Subdivisions",
        description="How many vertex columns across the road width",
        default=5,
        min=1,
        soft_max=65,
        update=lambda self, ctx: schedule_mesh_build(self.id_data)
    )
    road_uv_multiplier: FloatProperty(name="UV Y-Multiplier", default=1.0,
    update=lambda self, ctx: schedule_mesh_build(self.id_data))
    mesh_subdivision_length: FloatProperty(name="Mesh Subdiv Length", default=20.0, min=0.1,
    update=lambda self, ctx: schedule_mesh_build(self.id_data))
    mesh_subdivision_angle_deg: FloatProperty(name="Mesh Subdiv Angle", default=8.0, min=0.1, max=90.0,
    update=lambda self, ctx: schedule_mesh_build(self.id_data))
    num_checkpoints_per_segment: IntProperty(name="Checkpoints in Segment", default=8, min=0)
    modulations: CollectionProperty(type=MXTModulation)
    active_mod_index: IntProperty(
        name="Active Modulation Index",
        default=0,
        update=mxt_active_mod_index_update
    )
    checkpoints:    CollectionProperty(type=MXTCheckpoint)
    draw_checkpoints: BoolProperty(name="Draw Checkpoints", default=False)
    active_cp_idx:  IntProperty(default=-1)
    embeds:             CollectionProperty(type=MXTEmbed)
    active_embed_idx:  IntProperty(
        name="Active Embed Index",
        default=0,
        update=mxt_active_embed_idx_update
    )
    draw_embeds:        BoolProperty(name="Draw Embeds", default=False)
    
    
    segment_type: EnumProperty(
        name="Segment Type",
        items=[('BEZIER', "Bezier", "Use multiple empties to define a Bezier path"),
               ('LINE', "Line", "Linearly interpolate between two points"),
               ('SPIRAL', "Spiral", "A procedural spiral/helix segment")],
        default='BEZIER',
        description="Choose the method for generating the road segment's path",
        
        update=mxt_segment_type_update
    )

    
    line_start_point: PointerProperty(
        name="Start Point",
        type=bpy.types.Object,
        description="The object controlling the start transform of the line segment",
        poll=lambda self, object: object.type == 'EMPTY'
    )
    line_end_point: PointerProperty(
        name="End Point",
        type=bpy.types.Object,
        description="The object controlling the end transform of the line segment",
        poll=lambda self, object: object.type == 'EMPTY'
    )

    
    spiral_degrees: FloatProperty(
        name="Total Degrees", default=90.0,
        description="How many degrees to rotate around the axis over the segment length",
        update=lambda self, ctx: schedule_cm_rebake(self.id_data)
    )
    spiral_axis: FloatVectorProperty(
        name="Axis",
        description="The axis of rotation for the spiral (will be normalized)",
        default=(0.0, 1.0, 0.0),
        size=3,
        update=lambda self, ctx: schedule_cm_rebake(self.id_data)
    )
    spiral_helper: PointerProperty(
        name="Spiral Helper",
        description="Empty containing F-Curves for Radius (Loc.X), Height (Loc.Y), and Twist (Loc.Z)",
        type=bpy.types.Object,
        poll=lambda self, object: object.type == 'EMPTY'
    )
    spiral_axis_helper: PointerProperty( 
        name="Axis Helper",
        description="Optional Empty to define the spiral's origin and axis (its local Z-axis)",
        type=bpy.types.Object,
        poll=lambda self, object: object.type == 'EMPTY'
    )
    openness_helper: PointerProperty(
        name="Openness Helper",
        description="Empty containing an F-Curve on its X-Location to control the gap size (0-1)",
        type=bpy.types.Object,
        poll=lambda self, object: object.type == 'EMPTY'
    )
    preview_mesh_exists: BoolProperty(
        name="Preview Mesh Exists",
        description="Internal flag tracking if this segment has a preview mesh",
        default=False
    )


class MXT_UL_Embeds(bpy.types.UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "label", text="", emboss=False, icon='FCURVE')
        row.prop(item, "embed_type", text="", emboss=False, icon='NODE')

class MXTRoad_OT_AddEmbed(Operator):
    bl_idname = "mxt_road.add_embed"; bl_label = "Add Embed"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        seg = get_active_mxt_road_segment_parent(ctx)
        if not seg: return {'CANCELLED'}
        props = seg.mxt_road_overall_props

        bpy.ops.object.empty_add(type='SPHERE', radius=0, location=seg.location)
        helper = ctx.active_object
        _disallow_deletion(helper)
        helper.name = f"{seg.name}_Embed_{len(props.embeds):02d}"
        helper.parent = seg

        helper.animation_data_create()
        act = bpy.data.actions.new(f"{helper.name}_embedCurves")
        helper.animation_data.action = act
        
        
        for idx,val in ((1,-1.0), (2,1.0)): 
            helper.location[idx] = val * 0.5
            helper.keyframe_insert(data_path="location", index=idx, frame=0.0)
            helper.keyframe_insert(data_path="location", index=idx, frame=100.0)
            
            fcu = act.fcurves.find("location", index=idx)
            if fcu:
                for kp in fcu.keyframe_points:
                    kp.interpolation = 'BEZIER'
                    kp.handle_left_type = "LINEAR_X"
                    kp.handle_right_type = "LINEAR_X"
                _linearize_fcurve_handles_smooth(fcu)

        emb = props.embeds.add()
        emb.label = f"Embed {len(props.embeds)}"
        emb.helper = helper
        props.active_embed_idx = len(props.embeds)-1
        return {'FINISHED'}

class MXTRoad_OT_RemoveEmbed(Operator):
    bl_idname = "mxt_road.remove_embed"; bl_label = "Remove Embed"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        seg = get_active_mxt_road_segment_parent(ctx)
        props = seg.mxt_road_overall_props
        idx = props.active_embed_idx
        if idx < 0 or idx >= len(props.embeds): return {'CANCELLED'}
        emb = props.embeds[idx]
        if emb.helper:
            if emb.helper.animation_data and emb.helper.animation_data.action:
                act = emb.helper.animation_data.action
                if act.users == 1: bpy.data.actions.remove(act)
            bpy.data.objects.remove(emb.helper, do_unlink=True)
        props.embeds.remove(idx)
        props.active_embed_idx = min(max(0, idx-1), len(props.embeds)-1)
        return {'FINISHED'}

class MXTRoad_OT_AddModulation(Operator):
    bl_idname = "mxt_road.add_modulation"
    bl_label = "Add Modulation"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        seg = get_active_mxt_road_segment_parent(context)
        if not seg: return {'CANCELLED'}
        props = seg.mxt_road_overall_props
        
        bpy.ops.object.empty_add(type='SPHERE', radius=0, location=seg.location)
        helper = context.active_object
        _disallow_deletion(helper)
        helper.name = f"{seg.name}_Mod_{len(props.modulations):02d}"
        helper.parent = seg

        helper.animation_data_create()
        act = bpy.data.actions.new(f"{helper.name}_modCurves")
        helper.animation_data.action = act

        
        helper.location[1] = 0.0
        helper.keyframe_insert(data_path="location", index=1, frame=0.0)
        helper.keyframe_insert(data_path="location", index=1, frame=100.0)

        
        helper.location[2] = 0.0
        helper.keyframe_insert(data_path="location", index=2, frame=0.0)
        helper.location[2] = 1.0
        helper.keyframe_insert(data_path="location", index=2, frame=100.0)

        
        for idx in range(1, 3):
            fcu = act.fcurves.find("location", index=idx)
            if fcu:
                for kp in fcu.keyframe_points:
                    kp.interpolation = 'BEZIER'
                    kp.handle_left_type = "LINEAR_X"
                    kp.handle_right_type = "LINEAR_X"
                _linearize_fcurve_handles_smooth(fcu)

        mod = props.modulations.add()
        mod.label = f"Mod {len(props.modulations)}"
        mod.helper = helper
        props.active_mod_index = len(props.modulations) - 1
        return {'FINISHED'}

class MXTRoad_OT_RemoveModulation(Operator):
    bl_idname = "mxt_road.remove_modulation"
    bl_label = "Remove Modulation"
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        seg = get_active_mxt_road_segment_parent(context)
        props = seg.mxt_road_overall_props
        idx = props.active_mod_index
        if idx < 0 or idx >= len(props.modulations):
            return {'CANCELLED'}
        mod = props.modulations[idx]
        helper = mod.helper
        if helper:
            
            if helper.animation_data and helper.animation_data.action:
                act = helper.animation_data.action
                if act.users == 1:
                    bpy.data.actions.remove(act)
            bpy.data.objects.remove(helper, do_unlink=True)
        props.modulations.remove(idx)
        props.active_mod_index = min(max(0, idx-1), len(props.modulations)-1)
        return {'FINISHED'}

class MXTRoad_OT_SelectHelper(Operator):
    bl_idname = "mxt_road.select_helper"
    bl_label = "Select MXT Helper"
    bl_options = {'REGISTER', 'UNDO'}

    helper_name: StringProperty(
        name="Helper Name",
        description="The name of the helper object to select"
    )

    @classmethod
    def poll(cls, context):
        
        
        return context.mode == 'OBJECT'

    def execute(self, context):
        if not self.helper_name:
            self.report({'WARNING'}, "No helper name provided")
            return {'CANCELLED'}

        helper_obj = bpy.data.objects.get(self.helper_name)
        if not helper_obj:
            self.report({'WARNING'}, f"Helper object '{self.helper_name}' not found")
            return {'CANCELLED'}
        
        bpy.ops.object.select_all(action='DESELECT')
        helper_obj.select_set(True)
        context.view_layer.objects.active = helper_obj
        
        return {'FINISHED'}

class MXTRoad_OT_ConvertSegmentType(Operator):
    bl_idname = "mxt_road.convert_segment_type"
    bl_label = "Convert Segment Type"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return get_active_mxt_road_segment_parent(context) is not None

    def execute(self, context):
        parent = get_active_mxt_road_segment_parent(context)
        if not parent: return {'CANCELLED'}
        
        props = parent.mxt_road_overall_props
        target_type = props.segment_type

        
        
        start_loc, start_rot, start_scl = parent.matrix_world.decompose()
        end_loc, end_rot, end_scl = None, None, None
        found_source_transform = False

        
        
        cm_helper = props.curve_matrix_helper_empty
        if cm_helper and cm_helper.animation_data and cm_helper.animation_data.action and len(cm_helper.animation_data.action.fcurves) > 0:
            try:
                print("real curve")
                
                basis_start, pos_start, _ = _sample_curve_matrix(cm_helper, 0.0)
                local_mat_start = Matrix.Translation(pos_start) @ basis_start.to_4x4()
                world_mat_start = parent.matrix_world @ local_mat_start
                start_loc, start_rot, start_scl = world_mat_start.decompose()

                
                basis_end, pos_end, _ = _sample_curve_matrix(cm_helper, 1.0)
                local_mat_end = Matrix.Translation(pos_end) @ basis_end.to_4x4()
                world_mat_end = parent.matrix_world @ local_mat_end
                end_loc, end_rot, end_scl = world_mat_end.decompose()
                
                print(pos_start)
                print(pos_end)
                print(start_loc)
                print(end_loc)
                found_source_transform = True
            except Exception as e:
                
                print(f"MXT: Could not sample Curve Matrix during conversion, falling back to controls. Error: {e}")

        
        if not found_source_transform:
            existing_cps = get_mxt_control_point_empties(parent, sorted_by_time=True)
            print("fallback")
            
            if existing_cps:
                start_cp = existing_cps[0]
                start_loc, start_rot, start_scl = start_cp.matrix_world.decompose()
                if len(existing_cps) >= 2:
                    end_cp = existing_cps[-1]
                    end_loc, end_rot, end_scl = end_cp.matrix_world.decompose()
            
            
            elif props.line_start_point and props.line_end_point:
                start_loc, start_rot, start_scl = props.line_start_point.matrix_world.decompose()
                end_loc, end_rot, end_scl = props.line_end_point.matrix_world.decompose()

        
        if end_loc is None:
            print("poop")
            end_loc = start_loc + (start_rot @ Vector((0,0,100)))
            end_rot, end_scl = start_rot.copy(), start_scl.copy()

        
        existing_cps = get_mxt_control_point_empties(parent, sorted_by_time=True) 
        for cp in existing_cps: bpy.data.objects.remove(cp, do_unlink=True)
        if props.line_start_point: bpy.data.objects.remove(props.line_start_point, do_unlink=True)
        if props.line_end_point: bpy.data.objects.remove(props.line_end_point, do_unlink=True)
        if props.spiral_helper: bpy.data.objects.remove(props.spiral_helper, do_unlink=True)
        if props.spiral_axis_helper: bpy.data.objects.remove(props.spiral_axis_helper, do_unlink=True)
        if props.openness_helper:
            helper = props.openness_helper
            if helper.animation_data and helper.animation_data.action and helper.animation_data.action.users <= 1:
                bpy.data.actions.remove(helper.animation_data.action)
            bpy.data.objects.remove(helper, do_unlink=True)
        props.line_start_point, props.line_end_point, props.spiral_helper, props.spiral_axis_helper, props.openness_helper = None, None, None, None, None
        
        context.view_layer.objects.active = parent

        if target_type == 'BEZIER':
            cp0 = _create_cp_empty(context, parent, f"{parent.name}.CP.000", parent.matrix_world.inverted() @ start_loc, 0.0)
            cp0.rotation_quaternion = parent.matrix_world.to_quaternion().inverted() @ start_rot
            cp0.scale = start_scl
            cp1 = _create_cp_empty(context, parent, f"{parent.name}.CP.001", parent.matrix_world.inverted() @ end_loc, 1.0)
            cp1.rotation_quaternion = parent.matrix_world.to_quaternion().inverted() @ end_rot
            cp1.scale = end_scl
            schedule_road_parent_visual_update(parent, context)

        elif target_type == 'LINE':
            print("yay line")
            print(start_loc)
            start_point = bpy.data.objects.new(f"{parent.name}.LineStart", None)
            _disallow_deletion(start_point)
            start_point.empty_display_type, start_point.empty_display_size = 'CUBE', 1
            start_point.matrix_world = Matrix.Translation(start_loc) @ start_rot.to_matrix().to_4x4() @ Matrix.Diagonal((*start_scl, 1.0))
            context.collection.objects.link(start_point)
            start_point.parent, start_point.rotation_mode = parent, 'QUATERNION'
            end_point = bpy.data.objects.new(f"{parent.name}.LineEnd", None)
            _disallow_deletion(end_point)
            end_point.empty_display_type, end_point.empty_display_size = 'CUBE', 1
            end_point.matrix_world = Matrix.Translation(end_loc) @ end_rot.to_matrix().to_4x4() @ Matrix.Diagonal((*end_scl, 1.0))
            context.collection.objects.link(end_point)
            end_point.parent, end_point.rotation_mode = parent, 'QUATERNION'
            
            props.line_start_point, props.line_end_point = start_point, end_point
            start_point.mxt_line_handle_data.is_mxt_line_handle = True
            start_point.animation_data_create()
            action = bpy.data.actions.new(f"{start_point.name}_MXTEasingAction")
            start_point.animation_data.action = action
            for prop_name in ['rotation_ease_factor_channel', 'scale_ease_factor_channel']:
                start_point.mxt_line_handle_data[prop_name] = 0.0
                start_point.keyframe_insert(data_path=f'mxt_line_handle_data.{prop_name}', frame=0.0)
                start_point.mxt_line_handle_data[prop_name] = 1.0
                start_point.keyframe_insert(data_path=f'mxt_line_handle_data.{prop_name}', frame=100.0)
                fcu = action.fcurves.find(f'mxt_line_handle_data.{prop_name}')
                if fcu: _linearize_fcurve_handles_smooth(fcu)

        elif target_type == 'SPIRAL':
            
            axis_helper = bpy.data.objects.new(f"{parent.name}.SpiralAxisHelper", None)
            _disallow_deletion(axis_helper)
            axis_helper.empty_display_type = 'ARROWS'
            axis_helper.empty_display_size = start_scl.x
            
            axis_helper.matrix_world = Matrix.Translation(start_loc) @ start_rot.to_matrix().to_4x4()
            context.collection.objects.link(axis_helper)
            axis_helper.parent = parent
            props.spiral_axis_helper = axis_helper

            
            fcurve_helper = bpy.data.objects.new(f"{parent.name}.SpiralHelper", None)
            _disallow_deletion(fcurve_helper)
            fcurve_helper.empty_display_type = 'SPHERE'
            fcurve_helper.empty_display_size = 0
            context.collection.objects.link(fcurve_helper)
            fcurve_helper.parent = parent
            fcurve_helper.location = parent.location 
            props.spiral_helper = fcurve_helper
            
            fcurve_helper.animation_data_create()
            act = bpy.data.actions.new(f"{fcurve_helper.name}_spiralCurves")
            fcurve_helper.animation_data.action = act

            
            loc_defs = { 0: (50.0, 100.0), 1: (0.0, 0.0), 2: (0.0, 0.0) }
            for index, (start_val, end_val) in loc_defs.items():
                fcurve_helper.location[index] = start_val
                fcurve_helper.keyframe_insert(data_path="location", index=index, frame=0)
                fcurve_helper.location[index] = end_val
                fcurve_helper.keyframe_insert(data_path="location", index=index, frame=100)
                fcu = act.fcurves.find("location", index=index)
                if fcu: _linearize_fcurve_handles_smooth(fcu)

            
            scl_defs = { 0: end_scl.x, 1: end_scl.y }
            for index, value in scl_defs.items():
                
                fcurve_helper.scale[index] = value
                fcurve_helper.keyframe_insert(data_path="scale", index=index, frame=0)
                fcurve_helper.keyframe_insert(data_path="scale", index=index, frame=100)
                fcu = act.fcurves.find("scale", index=index)
                if fcu: _linearize_fcurve_handles_smooth(fcu)
        
        _bake_curve_matrix_direct(parent)
        return {'FINISHED'}
class MXTRoad_OT_RespaceCPTimes(Operator):
    bl_idname = "mxt_road.respace_cp_times"
    bl_label = "Respace CP Times"
    bl_description = "Re-distribute Control Point times evenly from 0.0 to 1.0 based on their current order"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        parent = get_active_mxt_road_segment_parent(context)
        return parent and parent.mxt_road_overall_props.segment_type == 'BEZIER'

    def execute(self, context):
        parent_obj = get_active_mxt_road_segment_parent(context)
        cps = get_mxt_control_point_empties(parent_obj, sorted_by_time=True)
        count = len(cps)
        if count < 2:
            self.report({'INFO'}, "Not enough control points to respace."); return {'CANCELLED'}

        step = 1.0 / (count - 1)
        for i, cp in enumerate(cps):
            if hasattr(cp, "mxt_cp_data"): cp.mxt_cp_data.time = i * step
        
        schedule_road_parent_visual_update(parent_obj, context)
        self.report({'INFO'}, f"Respaced times for {count} control points."); return {'FINISHED'}


def get_active_mxt_road_segment_parent(context):
    obj = context.active_object
    while obj:
        if getattr(obj, "mxt_road_overall_props", None) \
                and obj.mxt_road_overall_props.is_mxt_road_segment_parent:
            return obj
        obj = obj.parent            
    return None
def get_mxt_control_point_empties(parent_obj, sorted_by_time=True):
    cps = []
    if not parent_obj: return cps
    for child in parent_obj.children:
        if child.type == 'EMPTY' and hasattr(child, "mxt_cp_data") and child.mxt_cp_data.is_mxt_control_point:
            cps.append(child)
    if sorted_by_time:
        cps.sort(key=lambda cp_empty: cp_empty.mxt_cp_data.time if hasattr(cp_empty, "mxt_cp_data") else 0.0)
    return cps
def _linearize_fcurve_handles(fcu: bpy.types.FCurve):
    kps = fcu.keyframe_points
    if len(kps) < 2:
        return

    for idx, kp in enumerate(kps):
        # ensure handle type first so Blender doesn't reset lengths when changed
        kp.handle_left_type = 'LINEAR_X'
        kp.handle_right_type = 'LINEAR_X'

        left_vec = kp.handle_left - kp.co
        right_vec = kp.handle_right - kp.co

        if idx > 0:
            prev = kps[idx - 1]
            dx_prev = kp.co.x - prev.co.x
            target_dx = -dx_prev / 3.0
            if abs(left_vec.x) > 1e-6:
                scale = target_dx / left_vec.x
                kp.handle_left = kp.co + left_vec * scale
            else:
                kp.handle_left.x = kp.co.x + target_dx

        if idx < len(kps) - 1:
            nxt = kps[idx + 1]
            dx_next = nxt.co.x - kp.co.x
            target_dx = dx_next / 3.0
            if abs(right_vec.x) > 1e-6:
                scale = target_dx / right_vec.x
                kp.handle_right = kp.co + right_vec * scale
            else:
                kp.handle_right.x = kp.co.x + target_dx
    fcu.update()

def _linearize_fcurve_handles_smooth(fcu: bpy.types.FCurve):
    kps = fcu.keyframe_points
    if len(kps) < 2:
        return

    for idx, kp in enumerate(kps):
        
        if idx == 0:
            kp_prev = None
            kp_next = kps[idx + 1]
        elif idx == len(kps) - 1:
            kp_prev = kps[idx - 1]
            kp_next = None
        else:
            kp_prev = kps[idx - 1]
            kp_next = kps[idx + 1]
        
        if kp_prev and kp_next:
            
            slope_prev = (kp.co.y - kp_prev.co.y) / (kp.co.x - kp_prev.co.x)
            slope_next = (kp_next.co.y - kp.co.y) / (kp_next.co.x - kp.co.x)
            slope = 0.5 * (slope_prev + slope_next)

            dx_prev = kp.co.x - kp_prev.co.x
            dx_next = kp_next.co.x - kp.co.x

            kp.handle_left_type  = 'LINEAR_X'
            kp.handle_left.x     = kp.co.x - dx_prev / 3.0
            kp.handle_left.y     = kp.co.y - slope * dx_prev / 3.0
            kp.handle_right_type = 'LINEAR_X'
            kp.handle_right.x    = kp.co.x + dx_next / 3.0
            kp.handle_right.y    = kp.co.y + slope * dx_next / 3.0

        elif kp_prev:           
            slope = (kp.co.y - kp_prev.co.y) / (kp.co.x - kp_prev.co.x)
            dx_prev = kp.co.x - kp_prev.co.x

            kp.handle_left_type  = 'LINEAR_X'
            kp.handle_left.x     = kp.co.x - dx_prev / 3.0
            kp.handle_left.y     = kp.co.y - slope * dx_prev / 3.0

            
            kp.handle_right_type = 'LINEAR_X'
            kp.handle_right[:]   = kp.co

        else:                   
            slope = (kp_next.co.y - kp.co.y) / (kp_next.co.x - kp.co.x)
            dx_next = kp_next.co.x - kp.co.x

            kp.handle_right_type = 'LINEAR_X'
            kp.handle_right.x    = kp.co.x + dx_next / 3.0
            kp.handle_right.y    = kp.co.y + slope * dx_next / 3.0

            
            kp.handle_left_type  = 'LINEAR_X'
            kp.handle_left[:]    = kp.co
    fcu.update()
def _update_road_segment_visual_guide_logic(road_parent_empty, report_fn=None):
    if not road_parent_empty or not hasattr(road_parent_empty, "mxt_road_overall_props"):
        if report_fn: report_fn({'WARNING'}, "Invalid road parent empty for visual update.")
        return
    props = road_parent_empty.mxt_road_overall_props
    if not props.visual_guide_curve:
        if report_fn: report_fn({'WARNING'}, "No visual guide curve linked to road parent.")
        return
    guide_curve_obj = props.visual_guide_curve; curve_data = guide_curve_obj.data
    cp_empties = get_mxt_control_point_empties(road_parent_empty, sorted_by_time=True)
    guide_curve_obj.location = (0,0,0); guide_curve_obj.rotation_euler = (0,0,0); guide_curve_obj.scale = (1,1,1)
    while curve_data.splines: curve_data.splines.remove(curve_data.splines[0])
    if len(cp_empties) < 2: return
    spline = curve_data.splines.new('BEZIER'); spline.bezier_points.add(len(cp_empties) - 1)
    for i, cp_empty in enumerate(cp_empties):
        bp = spline.bezier_points[i]; cp_local_pos = cp_empty.location
        cp_local_rot_mat = cp_empty.rotation_euler.to_matrix(); cp_data = cp_empty.mxt_cp_data
        bp.co = cp_local_pos
        local_z_axis_of_cp = cp_local_rot_mat.col[2].normalized()
        handle_out_offset_local = local_z_axis_of_cp * cp_data.handle_out_length
        handle_in_offset_local  = -local_z_axis_of_cp * cp_data.handle_in_length
        bp.handle_right = bp.co + handle_out_offset_local; bp.handle_left  = bp.co + handle_in_offset_local
        bp.handle_left_type = 'ALIGNED'; bp.handle_right_type = 'ALIGNED'
    curve_data.update_gpu_tag()
def _find_road_parent(obj):
    while obj:
        if getattr(obj, "mxt_road_overall_props", None) \
                and obj.mxt_road_overall_props.is_mxt_road_segment_parent:
            return obj
        obj = obj.parent
    return None
def schedule_road_parent_visual_update(cp_empty_obj, context):
    global mxt_roads_pending_visual_update, mxt_timer_is_active
    if not cp_empty_obj: return
    road_parent_to_update = None
    if hasattr(cp_empty_obj, "mxt_cp_data") and cp_empty_obj.mxt_cp_data.is_mxt_control_point:
        if cp_empty_obj.parent and hasattr(cp_empty_obj.parent, "mxt_road_overall_props") and \
           cp_empty_obj.parent.mxt_road_overall_props.is_mxt_road_segment_parent:
            road_parent_to_update = cp_empty_obj.parent
    elif hasattr(cp_empty_obj, "mxt_road_overall_props") and \
         cp_empty_obj.mxt_road_overall_props.is_mxt_road_segment_parent:
        road_parent_to_update = cp_empty_obj
    if road_parent_to_update:
        if road_parent_to_update.name not in mxt_roads_pending_visual_update:
            mxt_roads_pending_visual_update.add(road_parent_to_update.name)
        if not mxt_timer_is_active:
            if bpy.context.screen:
                 try:
                    bpy.app.timers.register(_process_pending_visual_updates, first_interval=0.01666)
                    mxt_timer_is_active = True
                 except Exception as e:
                    print(f"MXT: Error registering timer: {e}")
def _process_pending_visual_updates():
    global mxt_roads_pending_visual_update, mxt_timer_is_active
    if not bpy.context.scene: mxt_timer_is_active=False; mxt_roads_pending_visual_update.clear(); return None
    if not mxt_roads_pending_visual_update: mxt_timer_is_active=False; return None
    roads_to_process_now = list(mxt_roads_pending_visual_update); mxt_roads_pending_visual_update.clear()
    for road_parent_name in roads_to_process_now:
        road_parent_obj = bpy.data.objects.get(road_parent_name)
        if road_parent_obj:
            try: _update_road_segment_visual_guide_logic(road_parent_obj)
            except Exception as e: print(f"MXT Error in timer visual update for {road_parent_name}: {e}")
    if mxt_roads_pending_visual_update: return 0.01
    mxt_timer_is_active = False; return None
_cm_pending   = set()
_mesh_pending = set()
_openness_helper_to_create = set() 
_openness_helper_to_destroy = set() 
_timer_live   = False

def _ensure_timer():
    global _timer_live
    if not _timer_live:
        bpy.app.timers.register(_process_live_updates, first_interval=0.01666)
        _timer_live = True
def schedule_cm_rebake(obj):
    parent = _find_road_parent(obj)
    if parent:
        _cm_pending.add(parent.name)
        _mesh_pending.add(parent.name)
        _ensure_timer()
def schedule_mesh_build(obj):
    parent = _find_road_parent(obj)
    if parent:
        _mesh_pending.add(parent.name)
        _ensure_timer()
def _process_live_updates():
    global _timer_live, _build_in_progress, _ignore_updates

    if _build_in_progress:
        return 0.05

    _build_in_progress = True
    _ignore_updates      = True

    try:
        while _openness_helper_to_destroy:
            name = _openness_helper_to_destroy.pop()
            parent = bpy.data.objects.get(name)
            if parent and parent.mxt_road_overall_props.openness_helper:
                helper = parent.mxt_road_overall_props.openness_helper
                if helper.animation_data and helper.animation_data.action and helper.animation_data.action.users <= 1:
                    bpy.data.actions.remove(helper.animation_data.action)
                bpy.data.objects.remove(helper, do_unlink=True)
                parent.mxt_road_overall_props.openness_helper = None

        while _openness_helper_to_create:
            name = _openness_helper_to_create.pop()
            parent = bpy.data.objects.get(name)
            if parent and not parent.mxt_road_overall_props.openness_helper:
                helper_data = bpy.data.objects.new(f"{parent.name}_OpennessHelper", None)
                _disallow_deletion(helper_data)
                helper_data.empty_display_type, helper_data.empty_display_size = 'SPHERE', 0
                parent.users_collection[0].objects.link(helper_data)
                helper_data.parent, helper_data.location = parent, parent.location
                
                helper_data.animation_data_create()
                action = bpy.data.actions.new(f"{helper_data.name}_OpennessCurve")
                helper_data.animation_data.action = action
                helper_data.location[0] = 1.0 
                helper_data.keyframe_insert(data_path="location", index=0, frame=0.0)
                helper_data.keyframe_insert(data_path="location", index=0, frame=100.0)
                fcu = action.fcurves.find("location", index=0)
                if fcu:
                    fcu.keyframe_points[0].interpolation = fcu.keyframe_points[1].interpolation = 'CONSTANT'
                    _linearize_fcurve_handles_smooth(fcu)
                parent.mxt_road_overall_props.openness_helper = helper_data
        
        while _cm_pending:
            name = _cm_pending.pop()
            parent = bpy.data.objects.get(name)
            if parent:
                try: _bake_curve_matrix_direct(parent)
                except Exception as e: print(f"CurvBake {name}: {e}")

        while _mesh_pending:
            name = _mesh_pending.pop()
            parent = bpy.data.objects.get(name)
            if parent:
                try: _build_mesh_direct(parent)
                except Exception as e: print(f"MeshBuild {name}: {e}")

    finally:
        _ignore_updates, _build_in_progress = False, False

    _timer_live = bool(_cm_pending or _mesh_pending or _openness_helper_to_create or _openness_helper_to_destroy)
    return 0.05 if _timer_live else None
@persistent
def mxt_on_depsgraph_update(scene, depsgraph):
    global _ignore_updates
    if _ignore_updates:
        return

    
    parents_to_rebake_cm = set()
    parents_to_rebuild_mesh = set()

    
    def check_and_schedule(obj):
        if not obj: return
        
        
        if obj.name.endswith(("_PreviewMesh", "_CurveMatrixHelper")): return
        if getattr(obj, "mxt_road_overall_props", None) and obj.mxt_road_overall_props.is_mxt_road_segment_parent: return

        parent = _find_road_parent(obj)
        if not parent: return
        if parent in parents_to_rebake_cm: return 

        props = parent.mxt_road_overall_props
        
        
        is_primary_control = False
        if props.segment_type == 'BEZIER' and hasattr(obj, "mxt_cp_data") and obj.mxt_cp_data.is_mxt_control_point:
            is_primary_control = True
        elif props.segment_type == 'LINE' and (obj == props.line_start_point or obj == props.line_end_point):
            is_primary_control = True
        elif props.segment_type == 'SPIRAL' and obj in (props.spiral_helper, props.spiral_axis_helper):
             is_primary_control = True

        if is_primary_control:
            parents_to_rebake_cm.add(parent)
            parents_to_rebuild_mesh.discard(parent) 
            return

        
        is_secondary_control = False
        if any(mod.helper == obj for mod in props.modulations):
            is_secondary_control = True
        elif any(emb.helper == obj for emb in props.embeds):
            is_secondary_control = True
        elif props.openness_helper == obj: 
            is_secondary_control = True
        
        if is_secondary_control:
            parents_to_rebuild_mesh.add(parent)
            return

    
    for upd in depsgraph.updates:
        
        if upd.is_updated_transform and isinstance(upd.id, bpy.types.Object):
            check_and_schedule(upd.id)

        
        elif isinstance(upd.id, bpy.types.Action):
            action = upd.id
            
            
            for o in bpy.data.objects:
                if o.animation_data and o.animation_data.action == action:
                    check_and_schedule(o)
                    break 

    
    for parent in parents_to_rebake_cm:
        schedule_cm_rebake(parent)
    
    for parent in parents_to_rebuild_mesh:
        schedule_mesh_build(parent)

    # If a preview mesh was removed manually, delete the entire segment
    parents_to_check = [obj for obj in bpy.data.objects
                        if getattr(obj, "mxt_road_overall_props", None)
                        and obj.mxt_road_overall_props.is_mxt_road_segment_parent]
    for parent in parents_to_check:
        props = parent.mxt_road_overall_props
        mesh_name = f"{parent.name}_PreviewMesh"
        mesh_exists = bpy.data.objects.get(mesh_name) is not None
        if props.preview_mesh_exists:
            if not mesh_exists:
                _delete_road_segment(parent)
        else:
            if mesh_exists:
                props.preview_mesh_exists = True
class MXTRoad_OT_LinearizeSelectedFCurves(Operator):
    bl_idname = "mxt_road.linearize_selected_fcurves"
    bl_label  = "Enforce ⅓ Handles"
    bl_description = "Force selected Bézier keys to use -1/3 · +1/3 handles"

    @classmethod
    def poll(cls, context):
        area = context.area
        return area and area.type == 'GRAPH_EDITOR' \
            and context.selected_editable_fcurves

    def execute(self, context):
        for fcu in context.selected_editable_fcurves:
            _linearize_fcurve_handles_smooth(fcu)
        self.report({'INFO'}, "Handles linearised")
        return {'FINISHED'}
def _respace_cp_times(parent_obj):
    cps = get_mxt_control_point_empties(parent_obj, sorted_by_time=False)
    if len(cps) < 2:
        return
    cps.sort(key=lambda c: c.mxt_cp_data.time)
    step = 1.0 / (len(cps) - 1)
    for i, cp in enumerate(cps):
        cp.mxt_cp_data.time = i * step
def _create_cp_empty(context, parent_obj, name, location_in_parent_space, time_val):
    bpy.ops.object.empty_add(type='PLAIN_AXES', radius=1.0, location=location_in_parent_space)
    cp_empty = context.active_object
    cp_empty.name = name
    cp_empty.parent = parent_obj
    cp_empty.mxt_cp_data.is_mxt_control_point = True
    cp_empty.mxt_cp_data.time = time_val
    _respace_cp_times(parent_obj)
    if not cp_empty.animation_data:
        cp_empty.animation_data_create()
    action_name = f"{cp_empty.name}_MXTEasingAction"
    action = bpy.data.actions.get(action_name)
    if not action:
        action = bpy.data.actions.new(name=action_name)
    cp_empty.animation_data.action = action
    prop_details = [
        ('rotation_ease_factor_channel', 0.0, 1.0),
        ('scale_ease_factor_channel', 0.0, 1.0),
        ('twist_ease_factor_channel', 0.0, 1.0),
    ]
    easing_group_name = "MXT Easing Factors"
    fcurve_group = action.groups.get(easing_group_name)
    if not fcurve_group:
        fcurve_group = action.groups.new(name=easing_group_name)
    for prop_name, start_val, end_val in prop_details:
        data_path = f'mxt_cp_data.{prop_name}'
        fcu = action.fcurves.find(data_path)
        if not fcu:
            setattr(cp_empty.mxt_cp_data, prop_name, start_val)
            cp_empty.keyframe_insert(data_path=data_path, frame=0.0)
            setattr(cp_empty.mxt_cp_data, prop_name, end_val)
            cp_empty.keyframe_insert(data_path=data_path, frame=100.0)
            fcu = action.fcurves.find(data_path)
            if fcu:
                _linearize_fcurve_handles_smooth(fcu)
                fcu.group = fcurve_group
                for kp in fcu.keyframe_points:
                    kp.interpolation = 'BEZIER'
                    kp.handle_left_type = "ALIGNED"
                    kp.handle_right_type = "ALIGNED"
                fcu.update()
            else:
                print(f"MXT Error: Failed to create or find F-Curve for {data_path} on {cp_empty.name}")
        else:
            _linearize_fcurve_handles_smooth(fcu)
            if not fcu.group or fcu.group.name != easing_group_name:
                fcu.group = fcurve_group
    context.view_layer.objects.active = parent_obj
    return cp_empty

class MXT_GGT_CPHandleGizmos(bpy.types.GizmoGroup):
    bl_idname = "MXT_GGT_cp_handle_gizmos"
    bl_label = "MXT CP Handle Gizmos"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj and obj.mode == 'OBJECT' and
                getattr(obj, "mxt_cp_data", None) and
                obj.mxt_cp_data.is_mxt_control_point)

    def setup(self, context):
        gz = self.gizmos.new("GIZMO_GT_arrow_3d")
        gz.use_draw_scale = True
        gz.color = 1.0, 0.6, 0.2
        gz.alpha = 0.8
        self.handle_out = gz

        gz = self.gizmos.new("GIZMO_GT_arrow_3d")
        gz.use_draw_scale = True
        gz.color = 0.2, 0.6, 1.0
        gz.alpha = 0.8
        self.handle_in = gz

    def draw_prepare(self, context):
        obj = context.active_object
        cp = obj.mxt_cp_data
        mat = obj.matrix_world

        rot_to_z = Matrix.Rotation(math.radians(-90.0), 4, 'X')

        self.handle_out.matrix_basis = mat @ rot_to_z
        self.handle_out.target_set_prop("offset", cp, "handle_out_length")
        self.handle_out.length = cp.handle_out_length

        self.handle_in.matrix_basis = mat @ rot_to_z @ Matrix.Rotation(math.pi, 4, 'Z')
        self.handle_in.target_set_prop("offset", cp, "handle_in_length")
        self.handle_in.length = cp.handle_in_length

def _delete_road_segment(parent_obj):
    if not parent_obj:
        return

    # Remove all child objects and their actions
    for child in list(parent_obj.children):
        if child.animation_data and child.animation_data.action and child.animation_data.action.users <= 1:
            bpy.data.actions.remove(child.animation_data.action)
        bpy.data.objects.remove(child, do_unlink=True)

    if parent_obj.animation_data and parent_obj.animation_data.action and parent_obj.animation_data.action.users <= 1:
        bpy.data.actions.remove(parent_obj.animation_data.action)

    _cm_pending.discard(parent_obj.name)
    _mesh_pending.discard(parent_obj.name)
    _openness_helper_to_create.discard(parent_obj.name)
    _openness_helper_to_destroy.discard(parent_obj.name)
    mxt_roads_pending_visual_update.discard(parent_obj.name)

    bpy.data.objects.remove(parent_obj, do_unlink=True)
class MXTRoad_OT_CreateRoadSegment(Operator):
    bl_idname = "mxt_road.create_segment_empties"
    bl_label  = "New Road Segment (Empties)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prev_seg = get_active_mxt_road_segment_parent(context)

        
        bpy.ops.object.empty_add(type='PLAIN_AXES', radius=1.0,
                                 location=context.scene.cursor.location)
        seg_par = context.active_object
        _disallow_deletion(seg_par)
        seg_par.name = "MXTRoadSegment.%03d" % len(
            [o for o in bpy.data.objects if o.name.startswith("MXTRoadSegment")])
        props = seg_par.mxt_road_overall_props
        props.is_mxt_road_segment_parent = True

        
        bpy.ops.object.empty_add(type='PLAIN_AXES', radius=0.0, location=(0,0,0))
        helper = context.active_object
        _disallow_deletion(helper)
        helper.name = f"{seg_par.name}_CurveMatrixHelper"
        helper.parent = seg_par
        helper.hide_set(True)
        props.curve_matrix_helper_empty = helper
        helper.matrix_parent_inverse = seg_par.matrix_world.inverted()

        
        cp0 = _create_cp_empty(context, seg_par, "MXTCP.000",
                               Vector((0,0,0)), 0.0)
        cp1 = _create_cp_empty(context, seg_par, "MXTCP.001",
                               Vector((0,0,500)), 1.0)

        cp0.scale = Vector((45, 45, 1))
        cp1.scale = Vector((45, 45, 1))

        
        if prev_seg:
            prev_props = prev_seg.mxt_road_overall_props
            prev_helper = prev_props.curve_matrix_helper_empty
            if prev_helper and prev_helper.animation_data:
                basis, pos, scale = _sample_curve_matrix(prev_helper, 1.0)

                
                eul = basis.to_euler()
                cp0.location = pos
                cp0.rotation_euler = eul
                cp0.scale = scale
                cp1.location = pos + basis.col[2].normalized() * 250
                cp1.rotation_euler = eul
                cp1.scale = scale

            
            for attr in ("road_shape_type", "horiz_subdivs",
                         "road_uv_multiplier", "mesh_subdivision_length",
                         "mesh_subdivision_angle_deg",
                         "num_checkpoints_per_segment"):
                setattr(props, attr, getattr(prev_props, attr))
                
            
            for mod_prev in prev_props.modulations:
                helper_prev = mod_prev.helper
                if not (helper_prev and helper_prev.animation_data and helper_prev.animation_data.action):
                    continue
                act_prev = helper_prev.animation_data.action

                f_h_prev = act_prev.fcurves.find("location", index=1)
                f_e_prev = act_prev.fcurves.find("location", index=2)
                if not f_e_prev:
                    continue

                bpy.ops.object.empty_add(type='SPHERE', radius=0, location=seg_par.location)
                helper_new = bpy.context.active_object
                _disallow_deletion(helper_new)
                helper_new.name = f"{seg_par.name}_Mod_{len(props.modulations):02d}"
                helper_new.parent = seg_par
                
                helper_new.animation_data_create()
                act_new = bpy.data.actions.new(f"{helper_new.name}_modCurves")
                helper_new.animation_data.action = act_new

                
                
                if f_h_prev and f_h_prev.keyframe_points:
                    
                    for kp_src in f_h_prev.keyframe_points:
                        helper_new.location[1] = kp_src.co.y
                        helper_new.keyframe_insert(data_path="location", index=1, frame=kp_src.co.x)
                    
                    
                    f_h_new = act_new.fcurves.find(data_path="location", index=1)
                    if f_h_new and len(f_h_new.keyframe_points) == len(f_h_prev.keyframe_points):
                        for i, kp_src in enumerate(f_h_prev.keyframe_points):
                            kp_new = f_h_new.keyframe_points[i]
                            
                            kp_new.handle_left = kp_src.handle_left
                            kp_new.handle_right = kp_src.handle_right
                            kp_new.handle_left_type = kp_src.handle_left_type
                            kp_new.handle_right_type = kp_src.handle_right_type
                            kp_new.interpolation = kp_src.interpolation
                            kp_new.easing = kp_src.easing
                        f_h_new.update()

                
                effect_end = f_e_prev.evaluate(100.0)
                helper_new.location[2] = effect_end
                helper_new.keyframe_insert(data_path="location", index=2, frame=0.0)
                helper_new.keyframe_insert(data_path="location", index=2, frame=100.0)

                
                f_e_new = act_new.fcurves.find("location", index=2)
                if f_e_new:
                    for kp in f_e_new.keyframe_points:
                        kp.interpolation = 'BEZIER'
                        kp.handle_left_type = "LINEAR_X"
                        kp.handle_right_type = "LINEAR_X"
                    _linearize_fcurve_handles_smooth(f_e_new)
                

                mod_new = props.modulations.add()
                mod_new.label = mod_prev.label
                mod_new.helper = helper_new
                props.active_mod_index = len(props.modulations) - 1
            for emb_prev in prev_props.embeds:
                helper_prev = emb_prev.helper
                if not helper_prev or not helper_prev.animation_data:
                    continue
                act_prev = helper_prev.animation_data.action
                if not act_prev:
                    continue

                f_left = act_prev.fcurves.find("location", index=1)
                f_right = act_prev.fcurves.find("location", index=2)
                if not (f_left and f_right):
                    continue

                
                tx_left_end = f_left.evaluate(100.0)
                tx_right_end = f_right.evaluate(100.0)

                
                bpy.ops.object.empty_add(type='SPHERE', radius=0, location=seg_par.location)
                helper_new = bpy.context.active_object
                _disallow_deletion(helper_new)
                helper_new.name = f"{seg_par.name}_Embed_{len(props.embeds):02d}"
                helper_new.parent = seg_par

                helper_new.animation_data_create()
                act_new = bpy.data.actions.new(f"{helper_new.name}_embedCurves")
                helper_new.animation_data.action = act_new

                
                
                helper_new.location[1] = tx_left_end
                helper_new.keyframe_insert(data_path="location", index=1, frame=0.0)
                helper_new.keyframe_insert(data_path="location", index=1, frame=100.0)
                
                
                helper_new.location[2] = tx_right_end
                helper_new.keyframe_insert(data_path="location", index=2, frame=0.0)
                helper_new.keyframe_insert(data_path="location", index=2, frame=100.0)

                
                for idx in [1, 2]:
                    fcu = act_new.fcurves.find("location", index=idx)
                    if fcu:
                        for kp in fcu.keyframe_points:
                            kp.interpolation = 'BEZIER'
                            kp.handle_left_type = "LINEAR_X"
                            kp.handle_right_type = "LINEAR_X"
                        _linearize_fcurve_handles_smooth(fcu)
                

                
                emb_new = props.embeds.add()
                emb_new.label = emb_prev.label
                emb_new.helper = helper_new
                emb_new.embed_type = emb_prev.embed_type
                emb_new.start_t = 0.0
                emb_new.end_t = 1.0
                props.active_embed_idx = len(props.embeds) - 1


        
        context.view_layer.objects.active = seg_par
        self.report({'INFO'},
                    f"Created MXT Road Segment docked to previous end" if prev_seg
                    else "Created standalone MXT Road Segment")
        return {'FINISHED'}

class MXTRoad_OT_AddControlPoint(Operator):
    bl_idname = "mxt_road.add_control_point_empty"; bl_label = "Add CP Empty"; bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context): return get_active_mxt_road_segment_parent(context) is not None
    def execute(self, context):
        road_parent_empty = get_active_mxt_road_segment_parent(context)
        cp_empties = get_mxt_control_point_empties(road_parent_empty, sorted_by_time=True)
        new_loc_local = mathutils.Vector((0,0,0)); new_time = 1.0
        if len(cp_empties) > 0:
            last_cp = cp_empties[-1]
            offset_in_last_cp_space = mathutils.Vector((0, 0, 250.0))
            new_loc_world = last_cp.matrix_world @ offset_in_last_cp_space
            new_loc_local = road_parent_empty.matrix_world.inverted() @ new_loc_world
            new_time = last_cp.mxt_cp_data.time + 0.1
            new_orientation = last_cp.rotation_euler
            new_scale = last_cp.scale
            
        new_cp_name = f"MXTCP.{len(cp_empties):03d}"
        new_cp_empty = _create_cp_empty(context, road_parent_empty, new_cp_name, new_loc_local, new_time)
        new_cp_empty.rotation_euler = new_orientation
        new_cp_empty.scale = new_scale
        _update_road_segment_visual_guide_logic(road_parent_empty, self.report)
        self.report({'INFO'}, f"Added Control Point to {road_parent_empty.name}"); return {'FINISHED'}
class MXTRoad_OT_UpdatePathVisuals(Operator):
    bl_idname = "mxt_road.update_path_visuals"; bl_label = "Update Path Visuals"; bl_options = {'REGISTER', 'UNDO'}
    @classmethod
    def poll(cls, context): return get_active_mxt_road_segment_parent(context) is not None
    def execute(self, context):
        _update_road_segment_visual_guide_logic(get_active_mxt_road_segment_parent(context), self.report)
        return {'FINISHED'}
_mxt_draw_handle = None
def _mxt_helper_positions(helper, samples=256):
    if not (helper and helper.animation_data and helper.animation_data.action):
        return []
    act = helper.animation_data.action
    fx = act.fcurves.find("location", index=0)
    fy = act.fcurves.find("location", index=1)
    fz = act.fcurves.find("location", index=2)
    if not (fx and fy and fz):
        return []
    out = []
    for i in range(samples + 1):
        t = i / samples
        out.append(Vector((fx.evaluate(t * 100), fy.evaluate(t * 100), fz.evaluate(t * 100))))
    return out

def _collect_modulations(seg_parent):
    if not (seg_parent and seg_parent.animation_data
            and seg_parent.animation_data.action):
        return []
    act = seg_parent.animation_data.action
    mods = []
    idx  = 0
    while True:
        f_h = act.fcurves.find(f'["mod_height_{idx}"]')
        f_e = act.fcurves.find(f'["mod_effect_{idx}"]')
        if not (f_h and f_e):
            break
        mods.append((f_h, f_e))
        idx += 1
    return mods

def _vertical_offset(seg_parent, mod_t: float, ty: float) -> float:
    off = 0.0
    if hasattr(seg_parent.mxt_road_overall_props, "modulations"):
        for mod in seg_parent.mxt_road_overall_props.modulations:
            helper = mod.helper
            if not (helper and helper.animation_data and helper.animation_data.action):
                continue
            act = helper.animation_data.action
            f_h = act.fcurves.find("location", index=1)
            f_e = act.fcurves.find("location", index=2)
            if not (f_h and f_e):
                continue
            aff = f_e.evaluate(ty * 100)
            if abs(aff) < 1e-6:
                continue
            off += f_h.evaluate(mod_t * 100) * aff
    return off

def _isolate_modulation_graph_editor():
    area = next((a for a in bpy.context.screen.areas if a.type == 'GRAPH_EDITOR'), None)
    if not area:
        return
    obj = bpy.context.active_object
    if not obj or not obj.parent:
        return
    props = getattr(obj.parent, "mxt_road_overall_props", None)
    if not props or not hasattr(props, "modulations"):
        return
    for mod in props.modulations:
        if mod.helper == obj:
            if obj.animation_data and obj.animation_data.action:
                for fcu in obj.animation_data.action.fcurves:
                    fcu.select = True
            area.spaces.active.show_only_selected = True
            return
def _ensure_fcurve(act, data_path, array_index):
    for fcu in act.fcurves:
        if fcu.data_path == data_path and fcu.array_index == array_index:
            return fcu
    return act.fcurves.new(data_path, index=array_index)
def _bake_curve_matrix_direct(parent_obj):
    with _no_undo():
        MXTRoad_OT_GenerateCurveMatrix.bake_for_parent(parent_obj)

def _build_mesh_direct(parent_obj):
    with _no_undo():
        MXTRoad_OT_GenerateMesh.build_for_parent(parent_obj, bpy.context)

def _ease(x, c):
    return x ** c if c != 0 else 0.0
def _remap(x, a, b, c, d):
    return (x - a) / (b - a + 1e-9) * (d - c) + c
def _root(helper, ty):
    basis, pos, _ = _sample_curve_matrix(helper, ty)
    return basis, pos
class RoadShape:
    def get_pos(self, helper, t: Vector): raise NotImplementedError
class RoadShapeFlat(RoadShape):
    def get_pos(self, helper, t):
        basis, pos = _root(helper, t.y)
        seg_parent = helper.parent

        mod_t = 0.5 * (1.0 - t.x)                
        y_off = _vertical_offset(seg_parent, mod_t, t.y)

        local = Vector((t.x, y_off, 0.0))
        return pos + basis @ local


class RoadShapeCylinder(RoadShape):
    def get_pos(self, helper, t):
        basis, pos = _root(helper, t.y)
        seg_parent = helper.parent

        theta   = t.x * math.pi                  
        radial  = Vector((math.sin(theta), math.cos(theta), 0.0)).normalized()

        mod_t   = 0.5 * (1.0 - t.x)
        r_off   = _vertical_offset(seg_parent, mod_t, t.y)

        local   = radial + radial * r_off        
        return pos + basis @ local


class RoadShapePipe(RoadShape):
    def __init__(self): self.inner = 0.8
    def get_pos(self, helper, t):
        basis, pos = _root(helper, t.y)
        seg_parent = helper.parent

        tx_angle = (t.x - 0.5) * math.pi
        radial   = Vector((math.cos(tx_angle), math.sin(tx_angle), 0.0)).normalized()

        mod_t    = 0.5 * (1.0 - t.x)
        r_off    = _vertical_offset(seg_parent, mod_t, t.y)

        local    = radial + radial * r_off
        return pos + basis @ local


class RoadShapeCylinderOpen(RoadShapeCylinder):
    def __init__(self): self.open_val = 0.5
    def get_pos(self, helper, t):
        t_open = t.copy();  t_open.x *= self.open_val
        return super().get_pos(helper, t_open)


class RoadShapePipeOpen(RoadShapePipe):
    def __init__(self): self.open_val = 0.5
    def get_pos(self, helper, t):
        t_open = t.copy();  t_open.x *= self.open_val
        return super().get_pos(helper, t_open)

def _sample_curve_matrix(helper_obj, t: float):
    act = helper_obj.animation_data.action
    fc_loc = [act.fcurves.find("location", index=i) for i in range(3)]
    fc_rot = [act.fcurves.find("rotation_quaternion", index=i) for i in range(4)]
    fc_scl = [act.fcurves.find("scale", index=i) for i in range(3)]
    pos = Vector((fc_loc[0].evaluate(t * 100),
                  fc_loc[1].evaluate(t * 100),
                  fc_loc[2].evaluate(t * 100)))
    quat = Quaternion((fc_rot[0].evaluate(t * 100),
                       fc_rot[1].evaluate(t * 100),
                       fc_rot[2].evaluate(t * 100),
                       fc_rot[3].evaluate(t * 100))).normalized()
    basis = quat.to_matrix().to_3x3()
    scale = Vector((fc_scl[0].evaluate(t * 100),
                    fc_scl[1].evaluate(t * 100),
                    fc_scl[2].evaluate(t * 100)))
    basis.col[0] *= scale.x
    basis.col[1] *= scale.y
    basis.col[2] *= scale.z
    return basis, pos, scale
def _sample_curve_matrix_numpy(helper_obj, t_values_1d):
    act = helper_obj.animation_data.action

    
    fc_loc = [act.fcurves.find("location", index=i) for i in range(3)]
    fc_rot = [act.fcurves.find("rotation_quaternion", index=i) for i in range(4)]
    fc_scl = [act.fcurves.find("scale", index=i) for i in range(3)]

    
    
    frames = t_values_1d * 100.0

    
    loc_x = np.array([fc_loc[0].evaluate(f) for f in frames])
    loc_y = np.array([fc_loc[1].evaluate(f) for f in frames])
    loc_z = np.array([fc_loc[2].evaluate(f) for f in frames])

    rot_w = np.array([fc_rot[0].evaluate(f) for f in frames])
    rot_x = np.array([fc_rot[1].evaluate(f) for f in frames])
    rot_y = np.array([fc_rot[2].evaluate(f) for f in frames])
    rot_z = np.array([fc_rot[3].evaluate(f) for f in frames])

    scl_x = np.array([fc_scl[0].evaluate(f) for f in frames])
    scl_y = np.array([fc_scl[1].evaluate(f) for f in frames])
    scl_z = np.array([fc_scl[2].evaluate(f) for f in frames])

    
    positions = np.stack((loc_x, loc_y, loc_z), axis=-1)
    quaternions = np.stack((rot_w, rot_x, rot_y, rot_z), axis=-1)
    scales = np.stack((scl_x, scl_y, scl_z), axis=-1)

    
    norms = np.linalg.norm(quaternions, axis=1, keepdims=True)
    
    norms[norms == 0] = 1
    quaternions /= norms

    return positions, quaternions, scales
def mxt_draw_callback():
    parent = get_active_mxt_road_segment_parent(bpy.context)
    if not parent:
        return 

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(2.0)

    
    
    props = parent.mxt_road_overall_props
    if not props.is_mxt_road_segment_parent:
        return

    helper = props.curve_matrix_helper_empty
    pts = _mxt_helper_positions(helper)
    if len(pts) < 2:
        pass 
    else:
        pts_world = [parent.matrix_world @ p for p in pts]
        batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": pts_world})
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 0.0, 1.0))
        batch.draw(shader)

    if props.draw_checkpoints and getattr(props, "checkpoints", None):
        shader.uniform_float("color", (1.0, 0.2, 0.2, 1.0))  
        for cp in props.checkpoints:
            for pos, basis_flat, xr, yr in (
                (cp.pos_start, cp.basis_start, cp.x_rad_start, cp.y_rad_start),
                (cp.pos_end, cp.basis_end, cp.x_rad_end, cp.y_rad_end)):

                B = mathutils.Matrix((
                    Vector(basis_flat[0:3]),
                    Vector(basis_flat[3:6]),
                    Vector(basis_flat[6:9]))).transposed()

                
                c = Vector(pos)

                p_x0 = c - B.col[0].normalized() * xr
                p_x1 = c + B.col[0].normalized() * xr
                p_y0 = c - B.col[1].normalized() * yr
                p_y1 = c + B.col[1].normalized() * yr

                world = [parent.matrix_world @ p for p in (p_x0, p_x1, p_y0, p_y1)]

                batch = batch_for_shader(shader, 'LINES', {"pos": world[0:2]})
                batch.draw(shader)
                batch = batch_for_shader(shader, 'LINES', {"pos": world[2:4]})
                batch.draw(shader)

    
    if props.draw_embeds and getattr(props, "embeds", None):
        shape_map = {
            'FLAT': RoadShapeFlat(),
            'CYLINDER': RoadShapeCylinder(),
            'PIPE': RoadShapePipe(),
            'CYLINDER_OPEN': RoadShapeCylinderOpen(),
            'PIPE_OPEN': RoadShapePipeOpen(),
        }
        shape = shape_map[props.road_shape_type]
        for emb in props.embeds:
            helper = emb.helper
            if not (helper and helper.animation_data and helper.animation_data.action):
                continue
            act = helper.animation_data.action
            f_left = act.fcurves.find("location", index=1)
            f_right = act.fcurves.find("location", index=2)
            if not (f_left and f_right): continue

            steps = 32
            verts_left = []
            verts_right = []
            for i in range(steps + 1):
                ty = emb.start_t + (emb.end_t - emb.start_t) * (i / steps)
                tx_l = f_left.evaluate(ty * 100)
                tx_r = f_right.evaluate(ty * 100)
                for tx, coll in ((tx_l, verts_left), (tx_r, verts_right)):
                    pos = shape.get_pos(props.curve_matrix_helper_empty, Vector((tx, ty)))
                    if pos is not None:
                        coll.append(parent.matrix_world @ pos)

            shader.uniform_float("color", (0.0, 0.8, 0.2, 1.0))  
            if len(verts_left) > 1:
                batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": verts_left})
                batch.draw(shader)
            shader.uniform_float("color", (0.8, 0.4, 0.0, 1.0))  
            if len(verts_right) > 1:
                batch = batch_for_shader(shader, 'LINE_STRIP', {"pos": verts_right})
                batch.draw(shader)

    if hasattr(props, "modulations"):
        for mod in props.modulations:
            helper_mod = mod.helper
            if not (helper_mod and helper_mod.select_get()):
                continue  

            act = helper_mod.animation_data.action if helper_mod.animation_data else None
            if not act:
                continue
            f_e = act.fcurves.find("location", index=2)  
            if not f_e:
                continue

            shape = {
                'FLAT': RoadShapeFlat(),
                'CYLINDER': RoadShapeCylinder(),
                'PIPE': RoadShapePipe(),
                'CYLINDER_OPEN': RoadShapeCylinderOpen(),
                'PIPE_OPEN': RoadShapePipeOpen(),
            }[props.road_shape_type]

            
            steps = 128  
            for kp in f_e.keyframe_points:
                ty = kp.co.x * 0.01

                verts = []
                for i in range(steps):
                    tx = -1.0 + 2.0 * i / (steps - 1)  
                    p = shape.get_pos(props.curve_matrix_helper_empty,
                                      Vector((tx, ty)))
                    if p is not None:
                        verts.append(parent.matrix_world @ p)

                if len(verts) > 1:
                    shader.bind()
                    shader.uniform_float("color", (0.0, 1.0, 1.0, 1.0))  
                    batch = batch_for_shader(shader, 'LINE_STRIP',
                                             {"pos": verts})
                    batch.draw(shader)
        
    
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')
class MXTRoad_PT_MainPanel(Panel):
    bl_label = "MXT Road Creator"; bl_idname = "MXTROAD_PT_main_panel"; bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'; bl_category = "MXT Road"
    def draw_cp_empty_props(self, layout, cp_empty):
        cp_data = cp_empty.mxt_cp_data; layout.prop(cp_data, "time")
        layout.label(text="Transform (on Empty):")
        col = layout.column(align=True); col.prop(cp_empty, "location", text=""); col.prop(cp_empty, "rotation_euler", text=""); col.prop(cp_empty, "scale", text="")
        layout.separator(); layout.label(text="Handle Lengths:")
        row = layout.row(align=True); row.prop(cp_data, "handle_in_length", text="In"); row.prop(cp_data, "handle_out_length", text="Out")
        easing_box = layout.box(); easing_box.label(text="Property Easing F-Curves (to Next CP):")
        easing_box.label(text="Select this CP Empty, then open the"); easing_box.label(text="Graph Editor to visually edit easing.")
        col = easing_box.column(align=True)
        col.label(text="- Rotation Ease: 'Rotation Ease Factor'", icon='FCURVE')
        col.label(text="- Scale Ease: 'Scale Ease Factor'", icon='FCURVE')
        col.label(text="- Twist Ease: 'Twist Ease Factor'", icon='FCURVE')
        if cp_empty.animation_data and cp_empty.animation_data.action:
            action = cp_empty.animation_data.action
            for prop_name_ui, data_path_str_part in { "Rotation": 'rotation_ease_factor_channel', "Scale": 'scale_ease_factor_channel', "Twist": 'twist_ease_factor_channel'}.items():
                fcu = action.fcurves.find(f'mxt_cp_data.{data_path_str_part}')
                if fcu and len(fcu.keyframe_points) > 0 : layout.label(text=f"{prop_name_ui} Factor @ t=0.5: {fcu.evaluate(0.5 * 100):.2f}")

    def draw(self, context):
        layout = self.layout; obj = context.active_object
        layout.operator(MXTRoad_OT_CreateRoadSegment.bl_idname); layout.separator()

        active_road_parent = get_active_mxt_road_segment_parent(context)
        if not active_road_parent:
            layout.label(text="Select an MXT Road Segment Parent or CP, or create new.")
            return

        road_props = active_road_parent.mxt_road_overall_props
        
        
        parent_box = layout.box()
        header_row = parent_box.row(align=True)
        header_row.label(text=f"Segment: {active_road_parent.name}")
        
        
        if road_props.segment_type == 'BEZIER':
            header_row.operator(MXTRoad_OT_AddControlPoint.bl_idname, text="", icon='ADD')
            header_row.operator('mxt_road.respace_cp_times', text="", icon='TIME')
            header_row.operator(MXTRoad_OT_UpdatePathVisuals.bl_idname, text="", icon='FILE_REFRESH')
        
        parent_box.separator()
        
        
        parent_box.prop(road_props, "segment_type")
        parent_box.separator()

        
        if road_props.segment_type == 'BEZIER':
            selected_cp = None
            if obj and obj.parent == active_road_parent and hasattr(obj, "mxt_cp_data") and obj.mxt_cp_data.is_mxt_control_point:
                selected_cp = obj
            
            if selected_cp:
                cp_box = parent_box.box()
                cp_box.label(text=f"Control Point: {selected_cp.name}")
                self.draw_cp_empty_props(cp_box, selected_cp)
            else:
                parent_box.label(text="Select a child CP Empty to edit its properties.")

        elif road_props.segment_type == 'LINE':
            line_box = parent_box.box()
            line_box.label(text="Line Segment Controls:")
            line_box.prop(road_props, "line_start_point")
            line_box.prop(road_props, "line_end_point")

            if obj and obj == road_props.line_start_point:
                easing_box = line_box.box()
                easing_box.label(text="Edit Easing in Graph Editor:")
                col = easing_box.column(align=True)
                col.label(text="- Rotation Ease: 'Rotation Ease Factor'", icon='FCURVE')
                col.label(text="- Scale Ease: 'Scale Ease Factor'", icon='FCURVE')

        elif road_props.segment_type == 'SPIRAL':
            spiral_box = parent_box.box()
            spiral_box.label(text="Spiral Segment Controls:")
            spiral_box.prop(road_props, "spiral_axis_helper")
            spiral_box.prop(road_props, "spiral_degrees")
            spiral_box.prop(road_props, "spiral_axis")
            spiral_box.prop(road_props, "spiral_helper")
            
            info_box = spiral_box.box()
            info_box.label(text="Edit F-Curves on Spiral Helper:")
            col = info_box.column(align=True)
            col.label(text="- Radius: Location X", icon='FCURVE')
            col.label(text="- Height: Location Y", icon='FCURVE')
            col.label(text="- Twist: Location Z (degrees)", icon='FCURVE')
            col.separator()
            col.label(text="- Road Width: Scale X", icon='FCURVE')
            col.label(text="- Road Thickness: Scale Y", icon='FCURVE')

            
            select_box = info_box.row()
            op = select_box.operator("mxt_road.select_helper", text="Edit Spiral Curves", icon='GRAPH')
            op.helper_name = road_props.spiral_helper.name if road_props.spiral_helper else ""
            select_box.enabled = bool(road_props.spiral_helper)


        parent_box.separator()
        
        
        common_box = layout.box()
        common_box.label(text="Shape and Mesh")
        common_box.prop(road_props, "road_shape_type")
        
        if road_props.road_shape_type in ('CYLINDER_OPEN', 'PIPE_OPEN'):
            open_box = common_box.box()
            open_box.prop(road_props, "openness_helper")
            
            
            select_row = open_box.row()
            op = select_row.operator("mxt_road.select_helper", text="Edit Openness Curve", icon='GRAPH')
            op.helper_name = road_props.openness_helper.name if road_props.openness_helper else ""
            select_row.enabled = bool(road_props.openness_helper)
            
        common_box.prop(road_props, "horiz_subdivs")
        common_box.prop(road_props, "road_uv_multiplier")
        mesh_gen_box = common_box.box(); mesh_gen_box.label(text="Adaptive Mesh Settings:")
        mesh_gen_box.prop(road_props, "mesh_subdivision_length"); mesh_gen_box.prop(road_props, "mesh_subdivision_angle_deg")

        
        mods_box = layout.box(); mods_box.label(text="Vertical Modulations & Embeds")
        mods_box.prop(road_props, "draw_embeds")
        row = mods_box.row()
        row.template_list("MXT_UL_Modulations", "", road_props, "modulations", road_props, "active_mod_index", rows=3)
        col = row.column(align=True); col.operator("mxt_road.add_modulation", icon='ADD', text=""); col.operator("mxt_road.remove_modulation", icon='REMOVE', text="")
        
        row = mods_box.row()
        row.template_list("MXT_UL_Embeds", "", road_props, "embeds", road_props, "active_embed_idx", rows=3)
        col = row.column(align=True); col.operator("mxt_road.add_embed", icon='ADD', text=""); col.operator("mxt_road.remove_embed", icon='REMOVE', text="")
        
        if road_props.embeds and 0 <= road_props.active_embed_idx < len(road_props.embeds):
            emb = road_props.embeds[road_props.active_embed_idx]
            emb_box = mods_box.box()
            emb_box.prop(emb, "label"); emb_box.prop(emb, "embed_type")
            emb_box.prop(emb, "start_t"); emb_box.prop(emb, "end_t")
            emb_box.prop(emb, "helper", text="Helper Empty")
        
        
        data_box = layout.box(); data_box.label(text="Data and Generation")
        data_box.prop(road_props, "num_checkpoints_per_segment")
        data_box.prop(road_props, "draw_checkpoints")
        data_box.separator()
        data_box.operator("mxt_road.generate_curve_matrix", text="Generate CurveMatrix", icon='FCURVE')
        data_box.operator("mxt_road.generate_mesh", text="Generate/Update Mesh", icon='MESH_PLANE')
        data_box.operator("mxt_road.generate_checkpoints", text="Generate Checkpoints", icon='OUTLINER_OB_EMPTY')
        data_box.operator("mxt_road.export_track_stub", text="Export Track", icon='EXPORT')

def _add_key(fcu, frame, value):
    kp = fcu.keyframe_points.insert(frame, value, options={'FAST'})
    kp.interpolation = 'BEZIER'
    kp.handle_left_type = "LINEAR_X"
    kp.handle_right_type = "LINEAR_X"
    return kp

class MXTRoad_OT_GenerateCurveMatrix(Operator):
    bl_idname = "mxt_road.generate_curve_matrix"
    bl_label  = "Generate CurveMatrix"
    bl_options = {'REGISTER', 'UNDO'}
    @staticmethod
    def _control_points(parent_obj):
        cps = [c for c in parent_obj.children
            if c.type == 'EMPTY'
            and getattr(c, "mxt_cp_data", None)
            and c.mxt_cp_data.is_mxt_control_point]
        cps.sort(key=lambda cp: cp.mxt_cp_data.time)
        return cps
    @staticmethod
    def _signed_angle(v_from: Vector, v_to: Vector, axis: Vector) -> float:
        cross_to = v_from.cross(v_to)
        unsigned = math.atan2(cross_to.length, max(v_from.dot(v_to), -1.0))
        sign     = cross_to.dot(axis)
        return -unsigned if sign < 0.0 else unsigned
    @staticmethod
    def _eval_channel(cp_empty, channel_name, t_norm):
        ad = cp_empty.animation_data
        if not (ad and ad.action):
            return t_norm
        fcu = ad.action.fcurves.find(f"mxt_cp_data.{channel_name}")
        return fcu.evaluate(t_norm * 100) if fcu else t_norm
    @staticmethod
    def _bezier_pos(p0, p1, p2, p3, t):
        omt = 1.0 - t
        return (p0 * (omt**3) +
            p1 * (3 * omt**2 * t) +
            p2 * (3 * omt * t**2) +
            p3 * t**3)
    @staticmethod
    def _quat_from_to(v_from, v_to):
        try:
            return v_from.rotation_difference(v_to)
        except ZeroDivisionError:
            alt_axis = Vector((1,0,0)) if abs(v_from.x) < .9 else Vector((0,1,0))
            return Quaternion(alt_axis, math.pi)
    @staticmethod
    def bake_for_parent_bezier(road_parent, *, report_fn=None):
        if not road_parent:
            if report_fn: report_fn({'ERROR'}, "No road‑segment parent")
            return False
        cps = MXTRoad_OT_GenerateCurveMatrix._control_points(road_parent)
        if len(cps) < 2:
            if report_fn: report_fn({'ERROR'}, "Need at least two control points")
            return False
        helper = road_parent.mxt_road_overall_props.curve_matrix_helper_empty
        if not helper:
            if report_fn: report_fn({'ERROR'}, "CurveMatrix helper empty not set")
            return False
        subdiv = 16
        t_samples = []
        t_samples.append(0.0)
        t_samples.append(0.0002)
        for i in range(len(cps) - 1):
            t0, t1 = cps[i].mxt_cp_data.time, cps[i+1].mxt_cp_data.time
            step = (t1 - t0) / subdiv
            for n in range(subdiv):
                t_samples.append(t0 + n * step)
        t_samples.append(0.9998)
        t_samples.append(1.0)
        if not helper.animation_data:
            helper.animation_data_create()
        act = helper.animation_data.action or \
            bpy.data.actions.new(f"{helper.name}_CurveMatrix")
        helper.animation_data.action = act
        curves = {
            ("location",i): _ensure_fcurve(act, "location", i)        for i in range(3)}
        curves |= {("rotation_quaternion",i): _ensure_fcurve(act,"rotation_quaternion",i) for i in range(4)}
        curves |= {("scale",i): _ensure_fcurve(act, "scale", i)      for i in range(3)}

        
        for fcu in curves.values():
            fcu.keyframe_points.clear()
        helper.rotation_mode = 'QUATERNION'
        for t in t_samples:
            span = next(i for i in range(len(cps)-1) if t <= cps[i+1].mxt_cp_data.time)
            a, b = cps[span], cps[span+1]
            span_len = b.mxt_cp_data.time - a.mxt_cp_data.time
            if span_len <= 1e-12:
                continue    
            bt = (t - a.mxt_cp_data.time) / span_len
            az = (a.rotation_euler.to_matrix().col[2]).normalized()
            bz = (b.rotation_euler.to_matrix().col[2]).normalized()
            p0 = a.location
            p1 = p0 + az * a.mxt_cp_data.handle_out_length
            p3 = b.location
            p2 = p3 - bz * b.mxt_cp_data.handle_in_length
            pos = MXTRoad_OT_GenerateCurveMatrix._bezier_pos(p0, p1, p2, p3, bt)
            dp = (
                3.0 * (1 - bt)**2 * (p1 - p0) +
                6.0 * (1 - bt) * bt * (p2 - p1) +
                3.0 * bt**2 * (p3 - p2)
            )
            forward_dir = dp.normalized()
            rot_fac   = MXTRoad_OT_GenerateCurveMatrix._eval_channel(a, "rotation_ease_factor_channel", bt)
            scale_fac = MXTRoad_OT_GenerateCurveMatrix._eval_channel(a, "scale_ease_factor_channel", bt)
            twist_fac = MXTRoad_OT_GenerateCurveMatrix._eval_channel(a, "twist_ease_factor_channel", bt)
            ra = a.rotation_euler.to_quaternion()
            rb = b.rotation_euler.to_quaternion()
            if rot_fac > 0 and rot_fac < 1:
                base_rot = ra.slerp(rb, rot_fac)
            z_start       = ra @ Vector((0, 0, 1))
            q_to_fwd      = MXTRoad_OT_GenerateCurveMatrix._quat_from_to(
                               z_start, forward_dir)
            rot_fwd_start = q_to_fwd @ ra
            z_end         = rb @ Vector((0, 0, 1))
            q_align_end   = MXTRoad_OT_GenerateCurveMatrix._quat_from_to(
                               z_start, z_end)
            rot_fwd_end   = q_align_end @ ra
            z_fwd_end_fix = rot_fwd_end @ Vector((0, 0, 1))
            q_fix_end     = MXTRoad_OT_GenerateCurveMatrix._quat_from_to(
                               z_fwd_end_fix, z_end)
            rot_fwd_end   = q_fix_end @ rot_fwd_end
            y_fixed = rot_fwd_end @ Vector((0, 1, 0))
            y_real  = rb          @ Vector((0, 1, 0))
            axis_z  = z_end
            twist_end = MXTRoad_OT_GenerateCurveMatrix._signed_angle(
                           y_fixed, y_real, axis_z)
            twist_cur = twist_end * twist_fac
            q_twist   = Quaternion(forward_dir, twist_cur)
            final_rot = q_twist @ rot_fwd_start
            scale = a.scale.lerp(b.scale, scale_fac)
            helper.location = pos
            helper.rotation_quaternion = final_rot
            helper.scale = scale
            _add_key(curves[("location",0)], t * 100, pos.x)
            _add_key(curves[("location",1)], t * 100, pos.y)
            _add_key(curves[("location",2)], t * 100, pos.z)

            q = final_rot.normalized()
            for idx,val in enumerate((q.w, q.x, q.y, q.z)):
                _add_key(curves[("rotation_quaternion",idx)], t * 100, val)

            for idx,val in enumerate(scale):
                _add_key(curves[("scale",idx)], t * 100, val)
        for fc in act.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = 'BEZIER'
            _linearize_fcurve_handles_smooth(fc)
            fc.update()
        if isinstance(MXTRoad_OT_GenerateCurveMatrix, Operator):
            if report_fn: report_fn({'INFO'}, f"Baked {len(t_samples)} keys with full orientation logic.")
        return True
    @staticmethod
    def _auto_calc_line_easing(start_point, start_quat, end_quat, start_scl, end_scl):
        act = start_point.animation_data.action
        fcu_rot = act.fcurves.find('mxt_line_handle_data.rotation_ease_factor_channel')
        fcu_scl = act.fcurves.find('mxt_line_handle_data.scale_ease_factor_channel')
        if not (fcu_rot and fcu_scl):
            return
        fcu_rot.keyframe_points.clear()
        fcu_scl.keyframe_points.clear()
        from mathutils import Vector
        axis = Vector((1, 0, 0))
        vec0 = start_quat @ axis * start_scl.x
        vec1 = end_quat   @ axis * end_scl.x
        total_angle = vec0.angle(vec1)
        subdiv = 32
        for i in range(subdiv + 1):
            t = i / subdiv
            frame = t * 100.0
            vec_t = vec0.lerp(vec1, t)
            scl_ease = (vec_t.length - start_scl.x) / (end_scl.x - start_scl.x) if end_scl.x != start_scl.x else t
            angle_t = vec0.angle(vec_t)
            rot_ease = angle_t / total_angle if total_angle != 0.0 else 0.0
            fcu_rot.keyframe_points.insert(frame, rot_ease)
            fcu_scl.keyframe_points.insert(frame, scl_ease)
        _linearize_fcurve_handles_smooth(fcu_rot)
        _linearize_fcurve_handles_smooth(fcu_scl)
    @staticmethod
    def bake_for_parent_line(road_parent, *, report_fn=None):
        props = road_parent.mxt_road_overall_props
        start_point, end_point = props.line_start_point, props.line_end_point

        if not (start_point and end_point and hasattr(start_point, "mxt_line_handle_data")):
            if report_fn: report_fn({'ERROR'}, "Line segment is not set up correctly for easing.")
            return False

        helper = props.curve_matrix_helper_empty
        if not helper:
            if report_fn: report_fn({'ERROR'}, "CurveMatrix helper empty not set.")
            return False

        if not helper.animation_data: helper.animation_data_create()
        act = helper.animation_data.action or bpy.data.actions.new(f"{helper.name}_CurveMatrix")
        helper.animation_data.action = act
        act.fcurves.clear()
        helper.rotation_mode = 'QUATERNION'
        
        
        curves = {("location",i): _ensure_fcurve(act, "location", i) for i in range(3)}
        curves |= {("rotation_quaternion",i): _ensure_fcurve(act,"rotation_quaternion",i) for i in range(4)}
        curves |= {("scale",i): _ensure_fcurve(act, "scale", i) for i in range(3)}

        
        easing_action = start_point.animation_data.action
        fcu_rot_ease = easing_action.fcurves.find('mxt_line_handle_data.rotation_ease_factor_channel')
        fcu_scl_ease = easing_action.fcurves.find('mxt_line_handle_data.scale_ease_factor_channel')

        if not (fcu_rot_ease and fcu_scl_ease):
            if report_fn: report_fn({'ERROR'}, "Easing F-Curves not found on Line Start Point.")
            return False

        start_loc, end_loc = start_point.location.copy(), end_point.location.copy()
        start_quat, end_quat = start_point.rotation_quaternion.copy(), end_point.rotation_quaternion.copy()
        start_scl, end_scl = start_point.scale.copy(), end_point.scale.copy()
        
        subdiv = 32 
        t_samples = [i/subdiv for i in range(subdiv + 1)]
        MXTRoad_OT_GenerateCurveMatrix._auto_calc_line_easing(start_point, start_quat, end_quat, start_scl, end_scl)
        for t in t_samples:
            frame = t * 100.0
            
            
            pos = start_loc.lerp(end_loc, t)
            
            
            rot_t = fcu_rot_ease.evaluate(frame)
            scl_t = fcu_scl_ease.evaluate(frame)

            
            rot = start_quat.slerp(end_quat, rot_t)
            scl = start_scl.lerp(end_scl, scl_t)

            _add_key(curves[("location",0)], frame, pos.x)
            _add_key(curves[("location",1)], frame, pos.y)
            _add_key(curves[("location",2)], frame, pos.z)
            _add_key(curves[("rotation_quaternion",0)], frame, rot.w)
            _add_key(curves[("rotation_quaternion",1)], frame, rot.x)
            _add_key(curves[("rotation_quaternion",2)], frame, rot.y)
            _add_key(curves[("rotation_quaternion",3)], frame, rot.z)
            _add_key(curves[("scale",0)], frame, scl.x)
            _add_key(curves[("scale",1)], frame, scl.y)
            _add_key(curves[("scale",2)], frame, scl.z)

        for fc in curves.values():
            _linearize_fcurve_handles_smooth(fc)
            fc.update()

        if report_fn: report_fn({'INFO'}, f"Baked Eased Line segment with {len(t_samples)} keys.")
        return True

    @staticmethod
    def bake_for_parent_spiral(road_parent, *, report_fn=None):
        import math

        props = road_parent.mxt_road_overall_props
        spiral_helper = props.spiral_helper
        axis_helper = props.spiral_axis_helper
        if not (spiral_helper and spiral_helper.animation_data
                and spiral_helper.animation_data.action):
            if report_fn: report_fn({'ERROR'}, "Spiral helper / curves missing.")
            return False
        if not axis_helper:
            if report_fn: report_fn({'ERROR'}, "Assign a Spiral Axis Helper.")
            return False

        act_s = spiral_helper.animation_data.action
        fcu_rad = act_s.fcurves.find("location", index=0)
        fcu_h = act_s.fcurves.find("location", index=1)
        fcu_tw = act_s.fcurves.find("location", index=2)
        fcu_sx = act_s.fcurves.find("scale", index=0)
        fcu_sy = act_s.fcurves.find("scale", index=1)
        if not (fcu_rad and fcu_h and fcu_tw and fcu_sx and fcu_sy):
            if report_fn: report_fn({'ERROR'}, "Spiral helper missing curves.")
            return False

        cm = props.curve_matrix_helper_empty
        if not cm:
            if report_fn: report_fn({'ERROR'}, "CurveMatrix helper not set.")
            return False
        if not cm.animation_data: cm.animation_data_create()

        act_cm = cm.animation_data.action or bpy.data.actions.new(
            f"{cm.name}_CurveMatrix")
        cm.animation_data.action = act_cm
        act_cm.fcurves.clear()
        cm.rotation_mode = 'QUATERNION'

        curves = {("location", i): _ensure_fcurve(act_cm, "location", i)
                  for i in range(3)}
        curves |= {("rotation_quaternion", i): _ensure_fcurve(
            act_cm, "rotation_quaternion", i) for i in range(4)}
        curves |= {("scale", i): _ensure_fcurve(act_cm, "scale", i)
                   for i in range(3)}

        
        axis_vec = Vector(props.spiral_axis).normalized()

        def canon_matrix(t):
            frame = t * 100.0
            
            r = fcu_rad.evaluate(frame)
            h = fcu_h.evaluate(frame)
            
            ang = math.radians(props.spiral_degrees) * t
            
            about = Vector((axis_vec.y, -axis_vec.x, 0.0)) * r
            p = -about
            
            qr = Quaternion(axis_vec, ang)
            p = qr @ p
            
            basis = qr.to_matrix()
            
            q_tw = Quaternion(basis.col[2], math.radians(fcu_tw.evaluate(frame)))
            basis = (q_tw.to_matrix() @ basis)

            
            
            eps = 0.001
            frame_eps = (t + eps) * 100.0
            r2 = fcu_rad.evaluate(frame_eps)
            h2 = fcu_h.evaluate(frame_eps)
            ang2 = math.radians(props.spiral_degrees) * (t + eps)
            about2 = Vector((axis_vec.y, -axis_vec.x, 0.0)) * r2
            p2 = -(Quaternion(axis_vec, ang2) @ about2) + axis_vec * h2
            tangent = (p2 - (p + axis_vec * h)).normalized()

            current_z = (basis.col[2]).normalized()
            axis_x = basis.col[0].normalized()

            
            z_proj = current_z - axis_x * current_z.dot(axis_x)
            tan_proj = tangent - axis_x * tangent.dot(axis_x)
            z_proj.normalize()
            tan_proj.normalize()

            angle = z_proj.angle(tan_proj)
            if z_proj.cross(tan_proj).dot(axis_x) < 0:
                angle = -angle

            
            q_adj = Quaternion(axis_x, angle)
            basis = (q_adj.to_matrix() @ basis)
            

            
            m = basis.to_4x4()
            m.translation = p + axis_vec * h
            
            m = Matrix.Translation(m.translation) @ basis.to_4x4()
            return m

        subdiv = 16
        ts = [0.0, 0.0002] + [i / subdiv for i in range(1, subdiv)] + [0.9998, 1.0]

        raw_transforms = []
        raw_s = []
        last_locked = axis_vec

        for t in ts:
            frame = t * 100.0
            trans = canon_matrix(t)
            raw_transforms.append(trans)
            raw_s.append(Vector((fcu_sx.evaluate(frame), fcu_sy.evaluate(frame), 1.0)))
        
        Mcorr = axis_helper.matrix_local @ raw_transforms[0].inverted()
        qcorr = Mcorr.to_quaternion()

        
        last_q = None
        for i, t in enumerate(ts):
            fr = t * 100.0
            pos = Mcorr @ raw_transforms[i].translation
            rot = (qcorr @ raw_transforms[i].to_3x3().to_quaternion()).normalized()
            if last_q and last_q.dot(rot) < 0: rot.negate()
            last_q = rot.copy()
            scl = raw_s[i]

            _add_key(curves[("location", 0)], fr, pos.x)
            _add_key(curves[("location", 1)], fr, pos.y)
            _add_key(curves[("location", 2)], fr, pos.z)
            _add_key(curves[("rotation_quaternion", 0)], fr, rot.w)
            _add_key(curves[("rotation_quaternion", 1)], fr, rot.x)
            _add_key(curves[("rotation_quaternion", 2)], fr, rot.y)
            _add_key(curves[("rotation_quaternion", 3)], fr, rot.z)
            _add_key(curves[("scale", 0)], fr, scl.x)
            _add_key(curves[("scale", 1)], fr, scl.y)
            _add_key(curves[("scale", 2)], fr, scl.z)

        for fcu in curves.values():
            _linearize_fcurve_handles_smooth(fcu)
            fcu.update()

        if report_fn:
            report_fn({'INFO'}, f"Baked spiral with axis‑locked orientation ({len(ts)} keys).")
        return True


    @staticmethod
    def bake_for_parent(road_parent, *, report_fn=None):
        props = road_parent.mxt_road_overall_props
        if props.segment_type == 'BEZIER':
            return MXTRoad_OT_GenerateCurveMatrix.bake_for_parent_bezier(road_parent, report_fn=report_fn)
        elif props.segment_type == 'LINE':
            return MXTRoad_OT_GenerateCurveMatrix.bake_for_parent_line(road_parent, report_fn=report_fn)
        elif props.segment_type == 'SPIRAL':
            return MXTRoad_OT_GenerateCurveMatrix.bake_for_parent_spiral(road_parent, report_fn=report_fn)
        
        if report_fn: report_fn({'ERROR'}, f"Unknown segment type: {props.segment_type}")
        return False

    def execute(self, context):
        road_parent = get_active_mxt_road_segment_parent(context)
        if not road_parent:
            self.report({'ERROR'}, "Select an MXT road-segment parent")
            return {'CANCELLED'}
        
        ok = MXTRoad_OT_GenerateCurveMatrix.bake_for_parent(road_parent, report_fn=self.report)
        
        
        if ok:
            schedule_mesh_build(road_parent)
            
        return {'FINISHED'} if ok else {'CANCELLED'}
def _surface(helper, tx, ty, seg_len, shape):
    eps  = 0.0005
    base = Vector((tx, ty))
    p0   = shape.get_pos(helper, base)
    if p0 is None:
        return None
    pr = shape.get_pos(helper, base - Vector((eps, 0)))
    pl = shape.get_pos(helper, base - Vector((-eps, 0)))

    
    
    seg_parent = helper.parent
    cps = get_mxt_control_point_empties(seg_parent, sorted_by_time=True)
    if len(cps) < 2:
        step_y = eps    
    else:
        
        if ty <= 0: a, b = cps[0], cps[1]
        elif ty >= 1: a, b = cps[-2], cps[-1]
        else:
            a_idx = max(i for i in range(len(cps)-1) if ty >= cps[i].mxt_cp_data.time)
            a, b = cps[a_idx], cps[a_idx+1]
        span_len = (b.mxt_cp_data.time - a.mxt_cp_data.time) or 1e-6
        bt = (ty - a.mxt_cp_data.time) / span_len
        p0_c = a.location
        p3_c = b.location
        z_a = a.rotation_euler.to_matrix().col[2].normalized()
        z_b = b.rotation_euler.to_matrix().col[2].normalized()
        p1_c = p0_c + z_a * a.mxt_cp_data.handle_out_length
        p2_c = p3_c - z_b * b.mxt_cp_data.handle_in_length

        
        dp = (
            3.0 * (1-bt)**2 * (p1_c - p0_c) +
            6.0 * (1-bt) * bt * (p2_c - p1_c) +
            3.0 * bt**2 * (p3_c - p2_c)
        )
        
        step_y = 1.0 / (dp.length + 1e-8)
    pr = shape.get_pos(helper, base - Vector((eps, 0)))
    pf = shape.get_pos(helper, base - Vector((0, step_y)))

    pl = shape.get_pos(helper, base + Vector((eps, 0)))
    pb = shape.get_pos(helper, base + Vector((0, step_y)))
    normal1 = -(pr - p0).cross(pf - p0).normalized()
    normal2 = -(pl - p0).cross(pb - p0).normalized()
    return p0, ((normal1 + normal2) * 0.5)

def _cubic(p0, p1, p2, p3, t: float):
    omt = 1.0 - t
    return (p0 * (omt**3) +
            p1 * (3*omt*omt*t) +
            p2 * (3*omt*t*t) +
            p3 * (t**3))
def _centerline_pos(seg_parent, ty: float):
    cps = get_mxt_control_point_empties(seg_parent, sorted_by_time=True)
    if len(cps) < 2:
        return seg_parent.location

    
    
    a_i = 0
    if ty >= 1.0:
        
        a_i = len(cps) - 2
    elif ty > 0.0:
        
        
        a_i = max(i for i in range(len(cps) - 1) if ty >= cps[i].mxt_cp_data.time)
    
    

    a = cps[a_i]
    b = cps[a_i+1]
    
    span_len = (b.mxt_cp_data.time - a.mxt_cp_data.time) or 1e-6
    
    bt = (ty - a.mxt_cp_data.time) / span_len
    
    p0 = a.location
    p3 = b.location
    z_a = a.rotation_euler.to_matrix().col[2].normalized()
    z_b = b.rotation_euler.to_matrix().col[2].normalized()
    p1 = p0 + z_a * a.mxt_cp_data.handle_out_length
    p2 = p3 - z_b * b.mxt_cp_data.handle_in_length
    
    return _cubic(p0, p1, p2, p3, bt)

def quaternions_to_rotation_matrices_numpy(q):
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    N = q.shape[0]
    R = np.empty((N, 3, 3), dtype=np.float64)

    R[:, 0, 0] = 1 - 2*y**2 - 2*z**2
    R[:, 0, 1] = 2*x*y - 2*z*w
    R[:, 0, 2] = 2*x*z + 2*y*w
    R[:, 1, 0] = 2*x*y + 2*z*w
    R[:, 1, 1] = 1 - 2*x**2 - 2*z**2
    R[:, 1, 2] = 2*y*z - 2*x*w
    R[:, 2, 0] = 2*x*z - 2*y*w
    R[:, 2, 1] = 2*y*z + 2*x*w
    R[:, 2, 2] = 1 - 2*x**2 - 2*y**2
    return R

def _evaluate_modulation_numpy(props, ty_1d):
    num_y = len(ty_1d)
    total_offset = np.zeros(num_y, dtype=np.float64)
    frames = ty_1d * 100.0

    for mod in props.modulations:
        helper = mod.helper
        if not (helper and helper.animation_data and helper.animation_data.action):
            continue

        act = helper.animation_data.action
        f_h = act.fcurves.find("location", index=1)
        f_e = act.fcurves.find("location", index=2)
        if not (f_h and f_e):
            continue

        
        height_vals = np.array([f_h.evaluate(f) for f in frames], dtype=np.float64)
        effect_vals = np.array([f_e.evaluate(f) for f in frames], dtype=np.float64)
        total_offset += height_vals * effect_vals

    return total_offset

def _calculate_vertex_positions_numpy(props, centerline_pos, centerline_quat, centerline_scl, tx_grid, ty_grid):
    num_y, num_x = tx_grid.shape
    ty_1d = ty_grid[:, 0]

    
    total_mod_offset_grid = np.zeros((num_y, num_x), dtype=np.float64)
    
    
    mod_t_grid = 0.5 * (1.0 - tx_grid) 

    for mod in props.modulations:
        helper = mod.helper
        if not (helper and helper.animation_data and helper.animation_data.action):
            continue

        act = helper.animation_data.action
        f_h = act.fcurves.find("location", index=1) 
        f_e = act.fcurves.find("location", index=2) 
        if not (f_h and f_e):
            continue

        
        
        effect_frames = ty_1d * 100.0
        effect_vals_1d = np.array([f_e.evaluate(f) for f in effect_frames], dtype=np.float64)

        
        
        height_frames_grid = mod_t_grid * 100.0
        
        flat_height_frames = height_frames_grid.ravel()
        flat_height_vals = np.array([f_h.evaluate(f) for f in flat_height_frames], dtype=np.float64)
        height_vals_grid = flat_height_vals.reshape(num_y, num_x)

        
        mod_grid = effect_vals_1d[:, np.newaxis] * height_vals_grid
        total_mod_offset_grid += mod_grid

    
    centerline_rot_mats = quaternions_to_rotation_matrices_numpy(centerline_quat)
    local_space_offsets = np.zeros((num_y, num_x, 3), dtype=np.float64)

    
    shape_type = props.road_shape_type
    angle_tx_grid = tx_grid

    if shape_type in ('CYLINDER_OPEN', 'PIPE_OPEN'):
        open_vals_1d = np.ones(num_y, dtype=np.float64)
        helper = props.openness_helper
        if helper and helper.animation_data and helper.animation_data.action:
            fcu = helper.animation_data.action.fcurves.find("location", index=0)
            if fcu:
                frames = ty_1d * 100.0
                open_vals_1d = np.array([fcu.evaluate(f) for f in frames], dtype=np.float64)
        angle_tx_grid = tx_grid * open_vals_1d.reshape(num_y, 1)

    
    if shape_type == 'FLAT':
        local_space_offsets[..., 0] = tx_grid 
        local_space_offsets[..., 1] = total_mod_offset_grid
    
    else: 
        if shape_type in ('CYLINDER', 'CYLINDER_OPEN'):
            angle = angle_tx_grid * np.pi
            radial_x, radial_y = np.sin(angle), np.cos(angle)
        else: 
            angle = (angle_tx_grid - 0.5) * np.pi
            radial_x, radial_y = np.cos(angle), np.sin(angle)

        radius = 1.0 + total_mod_offset_grid
        local_space_offsets[..., 0] = radial_x * radius
        local_space_offsets[..., 1] = radial_y * radius

    
    cl_pos = centerline_pos[:, np.newaxis, :]
    cl_scl = centerline_scl[:, np.newaxis, :]
    cl_rot = centerline_rot_mats[:, np.newaxis, :, :]
    scaled_offsets = local_space_offsets * cl_scl
    rotated_offsets = np.einsum('yxij,yxj->yxi', cl_rot, scaled_offsets)
    final_positions = cl_pos + rotated_offsets
    
    return final_positions

class MXTRoad_OT_GenerateMesh(Operator):
    bl_idname = "mxt_road.generate_mesh"
    bl_label  = "Generate/Update Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    def _adaptive_ty_samples_from_fcurves(cm_helper, max_len, max_ang_rad):
        samps = [0.0]; dists = [0.0]
        if not (cm_helper and cm_helper.animation_data and cm_helper.animation_data.action):
            return samps, dists
        
        act = cm_helper.animation_data.action
        fc_x = act.fcurves.find("location", index=0)
        fc_y = act.fcurves.find("location", index=1)
        fc_z = act.fcurves.find("location", index=2)
        if not (fc_x and fc_y and fc_z):
            return samps, dists

        def get_pos_from_fcurve(t_norm):
            frame = t_norm * 100.0
            return Vector((fc_x.evaluate(frame), fc_y.evaluate(frame), fc_z.evaluate(frame)))

        t = 0.001 
        
        
        h = 1e-3 
        p_prev = get_pos_from_fcurve(0.0)
        total_dist = 0.0
        
        while t < 1.0 - 1e-6:
            p_m = get_pos_from_fcurve(max(0.0, t - h))
            p_0 = get_pos_from_fcurve(t)
            p_p = get_pos_from_fcurve(min(1.0, t + h))
            
            r1 = (p_p - p_m) / (2 * h) 
            r2 = (p_p - 2 * p_0 + p_m) / (h * h) 
            
            speed = r1.length
            if speed < 1e-6:
                dt = 0.01
            else:
                curv_numerator = (r1.cross(r2)).length
                curv = curv_numerator / (speed**3 + 1e-12)
                
                dt_ang = max_ang_rad / (curv * speed + 1e-9)
                dt_len = max_len / speed
                dt = max(1e-5, min(dt_ang, dt_len))
            next_t = min(t + dt, 1.0)
            p_next = get_pos_from_fcurve(next_t)
            total_dist += (p_next - p_prev).length
            
            samps.append(next_t)
            dists.append(total_dist)
            
            t = next_t
            p_prev = p_next
            
        return samps, dists

    def _adaptive_ty_samples(helper, seg_parent, max_len, max_ang_rad):
        samps = [0.0]; dists = [0.0]
        t = 0.0
        h = 1e-2
        p_prev = _centerline_pos(seg_parent, 0.0)
        total = 0.0
        while t < 1.0 - 1e-6:
            p_m = _centerline_pos(seg_parent, t-h)
            p_0 = _centerline_pos(seg_parent, t)
            p_p = _centerline_pos(seg_parent, t+h)
            r1 = (p_p - p_m) / (2*h)
            r2 = (p_p - 2*p_0 + p_m) / (h*h)
            speed = r1.length
            curv  = (r1.cross(r2)).length / (speed**3 + 1e-12)
            dt_ang = max_ang_rad / max(curv*speed, 1e-9)
            dt_len = max_len       / max(speed,       1e-9)
            dt = max(1e-5, min(dt_ang, dt_len, 1.0 - t))
            next_t = min(t + dt, 1.0)
            p_next = _centerline_pos(seg_parent, next_t)
            total += (p_next - p_prev).length
            samps.append(next_t); dists.append(total)
            t      = next_t
            p_prev = p_next
        return samps, dists

    @staticmethod
    def _get_smooth_strip_normals(v_all, faces):
        face_normals = []
        for face in faces:
            v0, v1, v2 = v_all[face[0]], v_all[face[1]], v_all[face[2]]
            face_normals.append(np.cross(v1 - v0, v2 - v0))

        face_normals = np.array(face_normals)
        
        norms = np.linalg.norm(face_normals, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        face_normals /= norms

        vert_normals_map = {}
        for i, face in enumerate(faces):
            for v_idx in face:
                if v_idx not in vert_normals_map:
                    vert_normals_map[v_idx] = []
                vert_normals_map[v_idx].append(face_normals[i])
        
        
        for v_idx, normals in vert_normals_map.items():
            avg_normal = np.mean(normals, axis=0)
            norm = np.linalg.norm(avg_normal)
            if norm > 0:
                avg_normal /= norm
            vert_normals_map[v_idx] = avg_normal
            
        
        loop_normals = []
        for face in faces:
            for v_idx in face:
                loop_normals.append(vert_normals_map[v_idx])
        
        return loop_normals

    @staticmethod
    def build_for_parent(road_parent, context, *, report_fn=None):
        if report_fn:
            if not road_parent:
                report_fn({'ERROR'},"Select a road-segment parent"); return False
        props  = road_parent.mxt_road_overall_props
        helper = props.curve_matrix_helper_empty
        if not (helper and helper.animation_data and helper.animation_data.action):
            if report_fn: report_fn({'ERROR'},"Bake CurveMatrix first: no Action found on helper.");
            return False
        if props.horiz_subdivs < 2:
            if report_fn: report_fn({'ERROR'}, "Horizontal subdivisions must be >= 2");
            return False
        
        
        mesh_name = f"{road_parent.name}_PreviewMesh"
        mesh_obj = next((c for c in road_parent.children if c.name == mesh_name), None)
        if not mesh_obj:
            mesh_data = bpy.data.meshes.new(mesh_name)
            mesh_obj = bpy.data.objects.new(mesh_name, mesh_data)
            mesh_obj.parent = road_parent
            context.collection.objects.link(mesh_obj)
        props.preview_mesh_exists = True
        
        mesh_obj.data.materials.clear()
        material_map = {}
        required_materials = [
            'track_surface', 'embed_border', 'embed_ice', 'embed_recharge',
            'embed_dirt', 'embed_lava', 'embed_hole'
        ]
        for mat_name in required_materials:
            mat = bpy.data.materials.get(mat_name)
            if mat:
                mesh_obj.data.materials.append(mat)
                material_map[mat_name] = len(mesh_obj.data.materials) - 1
            else:
                if report_fn: report_fn({'WARNING'}, f"Material '{mat_name}' not found. Skipping.")
        
        def get_mat_idx(name):
            return material_map.get(name, 0)

        
        num_x = props.horiz_subdivs
        tx_1d = np.linspace(-1.0, 1.0, num_x, dtype=np.float64)
        if props.segment_type == 'BEZIER':
            ys, dist_1d = MXTRoad_OT_GenerateMesh._adaptive_ty_samples(helper, road_parent, props.mesh_subdivision_length, math.radians(props.mesh_subdivision_angle_deg))
        else:
            ys, dist_1d = MXTRoad_OT_GenerateMesh._adaptive_ty_samples_from_fcurves(helper, props.mesh_subdivision_length, math.radians(props.mesh_subdivision_angle_deg))
        ty_1d = np.array(ys, dtype=np.float64)
        num_y = len(ty_1d)
        if num_y < 2:
            if report_fn: report_fn({'ERROR'}, "Not enough vertical samples to build mesh.");
            return False

        
        tx_grid, ty_grid = np.meshgrid(tx_1d, ty_1d)
        centerline_pos, centerline_quat, centerline_scl = _sample_curve_matrix_numpy(helper, ty_1d)
        P0 = _calculate_vertex_positions_numpy(props, centerline_pos, centerline_quat, centerline_scl, tx_grid, ty_grid)
        
        verts_co = P0.reshape(-1, 3)
        uv_x = np.linspace(0.0, 1.0, num_x, dtype=np.float64)
        uv_tile_world_length = 50.0 / props.road_uv_multiplier
        uv_y_initial = np.array(dist_1d, dtype=np.float64) / uv_tile_world_length
        
        total_v_length = uv_y_initial[-1]
        if total_v_length > 1e-6:
            snapped_v_length = max(1.0, round(total_v_length))
            correction_factor = snapped_v_length / total_v_length
            uv_y = uv_y_initial * correction_factor
        else:
            uv_y = uv_y_initial
        uv_grid_x, uv_grid_y = np.meshgrid(uv_x, uv_y)
        uvs_per_vert = np.stack((uv_grid_x, uv_grid_y), axis=2).reshape(-1, 2)
        
        i = np.arange(verts_co.shape[0], dtype=np.int32).reshape(num_y, num_x)
        q0, q1, q2, q3 = i[:-1, :-1], i[:-1, 1:], i[1:, 1:], i[1:, :-1]
        main_road_faces = np.stack((q0, q3, q2, q1), axis=2).reshape(-1, 4)

        
        all_verts = list(verts_co)
        all_faces = main_road_faces.tolist()
        all_uvs_per_vert = list(uvs_per_vert)
        all_loop_normals = []
        all_material_indices = [get_mat_idx('track_surface')] * len(main_road_faces)

        
        epsilon = 0.0001
        cl_pos_f, cl_quat_f, cl_scl_f = _sample_curve_matrix_numpy(helper, np.minimum(ty_1d + epsilon, 1.0))
        PF = _calculate_vertex_positions_numpy(props, cl_pos_f, cl_quat_f, cl_scl_f, tx_grid, ty_grid + epsilon)
        PR = _calculate_vertex_positions_numpy(props, centerline_pos, centerline_quat, centerline_scl, tx_grid + epsilon, ty_grid)
        N_main = np.cross(PF - P0, PR - P0); norms = np.linalg.norm(N_main, axis=2, keepdims=True); norms[norms==0]=1.0; N_main /= norms
        main_road_vertex_normals = N_main.reshape(-1, 3)
        for face in main_road_faces:
            for v_idx in face: all_loop_normals.append(main_road_vertex_normals[v_idx])

        
        if hasattr(props, "embeds"):
            EMBED_INSET_UNITS = 1.0
            EMBED_PUSH_DISTANCE = 0.5
            EMBED_X_DIVS = 8
            
            hole_cutter_objects = []
            embeds_for_bmesh = [] 

            for embed in props.embeds:
                if not (embed.helper and embed.helper.animation_data and embed.helper.animation_data.action): continue
                act = embed.helper.animation_data.action
                f_left, f_right = act.fcurves.find("location", index=1), act.fcurves.find("location", index=2)
                if not (f_left and f_right): continue

                
                keyframe_times_t = []
                for fcurve in [f_left, f_right]:
                    for kfp in fcurve.keyframe_points:
                        t = kfp.co.x / 100.0
                        if embed.start_t < t < embed.end_t:
                            keyframe_times_t.append(t - 0.0001)
                            keyframe_times_t.append(t + 0.0001)
                ty_subset = ty_1d[(ty_1d > embed.start_t) & (ty_1d < embed.end_t)]
                all_t_samples = np.concatenate(([embed.start_t], ty_subset, [embed.end_t], keyframe_times_t))
                ty_embed_1d = np.unique(all_t_samples)

                if len(ty_embed_1d) < 2: continue

                cl_pos_e, cl_quat_e, cl_scl_e = _sample_curve_matrix_numpy(helper, ty_embed_1d)
                cl_rot_mats_e = quaternions_to_rotation_matrices_numpy(cl_quat_e)
                scl_ones = np.ones_like(cl_scl_e)
                frames_e = ty_embed_1d * 100.0
                tx_left, tx_right = np.array([f_left.evaluate(f) for f in frames_e]), np.array([f_right.evaluate(f) for f in frames_e])
                tx_embed_linspace = np.linspace(0, 1, EMBED_X_DIVS)[np.newaxis, :]
                tx_embed_grid = tx_left[:, np.newaxis] + (tx_right - tx_left)[:, np.newaxis] * tx_embed_linspace
                ty_embed_grid = np.repeat(ty_embed_1d[:, np.newaxis], EMBED_X_DIVS, axis=1)
                P_footprint_unscaled = _calculate_vertex_positions_numpy(props, cl_pos_e, cl_quat_e, scl_ones, tx_embed_grid, ty_embed_grid)

                def apply_scale(points_unscaled, centers, rotations, scales):
                    points_centered = points_unscaled - centers[:, np.newaxis, :]
                    S = np.apply_along_axis(np.diag, -1, scales)
                    T = rotations @ S @ np.transpose(rotations, (0, 2, 1))
                    points_scaled_centered = np.einsum('yij,ykj->yki', T, points_centered)
                    return points_scaled_centered + centers[:, np.newaxis, :]

                P_footprint = apply_scale(P_footprint_unscaled, cl_pos_e, cl_rot_mats_e, cl_scl_e)
                
                
                if embed.embed_type == 'HOLE':
                    
                    EXTRUDE_DEPTH, TOP_OFFSET = -4.0, -2.0
                    P_grid = P_footprint.copy()
                    d_ty, d_tx = np.gradient(P_grid, axis=0), np.gradient(P_grid, axis=1)
                    N_grid = np.cross(d_ty, d_tx)
                    n_len = np.linalg.norm(N_grid, axis=2, keepdims=True); n_len[n_len == 0.0] = 1.0; N_grid /= n_len
                    base_verts, n_flat = P_grid.reshape(-1, 3), N_grid.reshape(-1, 3)
                    cutter_mesh = bpy.data.meshes.new(f"{embed.label}_cutter"); cutter_obj = bpy.data.objects.new(f"{embed.label}_cutter", cutter_mesh)
                    _disallow_deletion(cutter_obj)
                    context.collection.objects.link(cutter_obj); hole_cutter_objects.append(cutter_obj)
                    bm = bmesh.new()
                    v_top = [bm.verts.new(tuple(v + n * TOP_OFFSET)) for v, n in zip(base_verts, n_flat)]
                    v_bot = [bm.verts.new(tuple(v - n * EXTRUDE_DEPTH)) for v, n in zip(base_verts, n_flat)]
                    n_y, n_x = P_grid.shape[0:2]
                    for row in range(n_y-1): 
                        for col in range(n_x-1):
                            i0=row*n_x+col; i1=i0+1; i2=i1+n_x; i3=i0+n_x
                            bm.faces.new((v_top[i0],v_top[i1],v_top[i2],v_top[i3])); bm.faces.new((v_bot[i3],v_bot[i2],v_bot[i1],v_bot[i0]))
                    for row in range(n_y-1): 
                        i0=row*n_x; i1=i0+n_x; i2=i1+n_x-1; i3=i0+n_x-1
                        bm.faces.new((v_top[i0],v_top[i1],v_bot[i1],v_bot[i0])); bm.faces.new((v_top[i3],v_bot[i3],v_bot[i2],v_top[i2]))
                    for col in range(n_x-1): 
                        i0=col; i1=i0+1; i2=(n_y-1)*n_x+i1; i3=(n_y-1)*n_x+i0
                        bm.faces.new((v_top[i0],v_bot[i0],v_bot[i1],v_top[i1])); bm.faces.new((v_top[i3],v_top[i2],v_bot[i2],v_bot[i3]))
                    bm.normal_update(); bm.to_mesh(cutter_mesh); bm.free()
                    cutter_obj.hide_set(True)
                    continue

                
                current_face_idx = len(all_faces)
                base_vert_idx = len(all_verts)
                all_verts.extend(P_footprint.reshape(-1, 3).tolist())
                footprint_indices = np.arange(base_vert_idx, len(all_verts)).reshape(P_footprint.shape[:2])
                q0,q1,q2,q3 = footprint_indices[:-1,:-1],footprint_indices[:-1,1:],footprint_indices[1:,1:],footprint_indices[1:,:-1]
                footprint_faces = np.stack((q0,q3,q2,q1),axis=2).reshape(-1,4)
                all_faces.extend(footprint_faces.tolist())

                
                embed_type_name = embed.embed_type.lower()
                surface_mat_idx = get_mat_idx(f'embed_{embed_type_name}')
                all_material_indices.extend([surface_mat_idx] * len(footprint_faces))

                all_loop_normals.extend(MXTRoad_OT_GenerateMesh._get_smooth_strip_normals(np.array(all_verts), footprint_faces))
                uv_y_embed = np.interp(ty_embed_1d, ty_1d, uv_y)
                uv_x_foot = np.linspace(0,1,EMBED_X_DIVS); uv_grid_x_foot,uv_grid_y_foot=np.meshgrid(uv_x_foot,uv_y_embed)
                all_uvs_per_vert.extend(np.stack((uv_grid_x_foot,uv_grid_y_foot),axis=2).reshape(-1,2).tolist())
                num_new_faces = len(footprint_faces)
                border_mat_idx = get_mat_idx('embed_border')
                embeds_for_bmesh.append( (current_face_idx, num_new_faces, border_mat_idx) )
                

        
        mesh = mesh_obj.data
        mesh.clear_geometry()
        num_total_verts, num_total_faces, num_loops = len(all_verts), len(all_faces), len(all_faces) * 4
        final_verts_co = np.array(all_verts, dtype=np.float32).ravel()
        final_faces_as_indices = np.array(all_faces, dtype=np.int32)
        final_uvs_per_vert = np.array(all_uvs_per_vert, dtype=np.float32)

        mesh.vertices.add(num_total_verts)
        mesh.polygons.add(num_total_faces)
        mesh.loops.add(num_loops)

        mesh.vertices.foreach_set("co", final_verts_co)
        mesh.polygons.foreach_set("loop_start", np.arange(0, num_loops, 4, dtype=np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(num_total_faces, 4, dtype=np.int32))
        mesh.loops.foreach_set("vertex_index", final_faces_as_indices.ravel())
        mesh.polygons.foreach_set("material_index", np.array(all_material_indices, dtype=np.int32))

        mesh.update(); mesh.validate()

        if not mesh.uv_layers: mesh.uv_layers.new(name="UVMap")
        loop_uvs = final_uvs_per_vert[final_faces_as_indices.ravel()]
        mesh.uv_layers.active.data.foreach_set('uv', loop_uvs.ravel())

        mesh.normals_split_custom_set(all_loop_normals)
        mesh.update()

        
        if embeds_for_bmesh:
            bm = bmesh.new()
            bm.from_mesh(mesh)
            bm.faces.ensure_lookup_table()
            for face_start_idx, num_faces, border_mat_idx in embeds_for_bmesh:
                faces_to_process = bm.faces[face_start_idx : face_start_idx + num_faces]
                
                inset_result = bmesh.ops.inset_region(
                    bm,
                    faces=faces_to_process,
                    thickness=EMBED_INSET_UNITS,
                    use_even_offset=True,
                    use_boundary=True,
                    depth=EMBED_PUSH_DISTANCE
                )
                
                
                if 'faces' in inset_result:
                    for face in inset_result['faces']:
                        face.material_index = border_mat_idx
            
            
            bm.to_mesh(mesh)
            bm.free()
            mesh.update()

        
        if hole_cutter_objects:
            
            context.view_layer.update() 
            
            orig_active = context.view_layer.objects.active
            orig_selected = [obj for obj in context.selected_objects]
            bpy.ops.object.select_all(action='DESELECT')
            mesh_obj.select_set(True)
            context.view_layer.objects.active = mesh_obj

            for cutter_obj in hole_cutter_objects:
                mod = mesh_obj.modifiers.new(name="HoleCutter", type='BOOLEAN')
                mod.object = cutter_obj
                mod.operation = 'DIFFERENCE'
                mod.solver = 'FAST'
                try:
                    bpy.ops.object.modifier_apply(modifier=mod.name)
                except RuntimeError as e:
                    if report_fn: report_fn({'ERROR'}, f"Boolean for {cutter_obj.name} failed: {e}")
                finally:
                    bpy.data.objects.remove(cutter_obj, do_unlink=True)
            
            if mesh.uv_layers and mesh.uv_layers.active:
                uv_layer = mesh.uv_layers.active.data
                bad_polys = [p.index for p in mesh.polygons if all(uv_layer[li].uv.length < 1e-6 for li in p.loop_indices)]
                if bad_polys:
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='DESELECT')
                    bpy.ops.object.mode_set(mode='OBJECT')
                    for idx in bad_polys: mesh.polygons[idx].select = True
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.delete(type='FACE')
                    bpy.ops.object.mode_set(mode='OBJECT')
            
            
            for obj in bpy.data.objects:
                obj.select_set(obj in orig_selected)
            context.view_layer.objects.active = orig_active

        if report_fn:
            report_fn({'INFO'}, f"NumPy+Bmesh build complete. Verts: {len(mesh.vertices)}, Faces: {len(mesh.polygons)}")
        return True

    def execute(self, context):
        parent = get_active_mxt_road_segment_parent(context)
        if not parent:
            self.report({'ERROR'}, "Select a road-segment parent")
            return {'CANCELLED'}
        ok = MXTRoad_OT_GenerateMesh.build_for_parent(parent, context, report_fn=self.report)
        return {'FINISHED'} if ok else {'CANCELLED'}

class MXTRoad_OT_GenerateCheckpoints(Operator):
    bl_idname  = "mxt_road.generate_checkpoints"
    bl_label   = "Generate Checkpoints"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return get_active_mxt_road_segment_parent(ctx) is not None

    def execute(self, ctx):
        seg    = get_active_mxt_road_segment_parent(ctx)
        props  = seg.mxt_road_overall_props
        helper = props.curve_matrix_helper_empty
        if not (helper and helper.animation_data):
            self.report({'ERROR'}, "Bake CurveMatrix first")
            return {'CANCELLED'}

        props.checkpoints.clear()

        num   = max(0, props.num_checkpoints_per_segment)
        step  = 1.0 / (num)                 
        for i in range(num):                
            t0 = i      * step
            t1 = (i+1)  * step

            b0, p0, _ = _sample_curve_matrix(helper, t0)
            b1, p1, _ = _sample_curve_matrix(helper, min(t1, 1.0))

            
            B0 = b0.copy();  B0.col[0].normalize(); B0.col[1].normalize(); B0.col[2].normalize()
            B1 = b1.copy();  B1.col[0].normalize(); B1.col[1].normalize(); B1.col[2].normalize()

            cp = props.checkpoints.add()
            cp.start_t  = t0
            cp.end_t    = t1

            cp.pos_start = p0
            cp.pos_end   = p1

            cp.basis_start = sum([list(B0.col[c]) for c in range(3)], [])
            cp.basis_end   = sum([list(B1.col[c]) for c in range(3)], [])

            cp.x_rad_start = b0.col[0].length
            cp.x_rad_end   = b1.col[0].length
            cp.y_rad_start = b0.col[1].length
            cp.y_rad_end   = b1.col[1].length
            cp.distance    = (p1 - p0).length

        self.report({'INFO'}, f"{len(props.checkpoints)} checkpoints generated")
        return {'FINISHED'}

class MXTRoad_OT_ExportTrackStub(Operator):
    bl_idname = "mxt_road.export_track_stub"
    bl_label = "Export Track (Stub)"
    def execute(self,c):
        self.report({'INFO'},"NYI"); return {'CANCELLED'}
classes_to_register = (
    MXTModulation,
    MXTEmbed,
    MXT_UL_Embeds,
    MXTRoad_OT_AddEmbed,
    MXTRoad_OT_RemoveEmbed,
    MXTCheckpoint,
    MXTRoad_LineHandleData,
    MXTRoad_ControlPointData,
    MXTRoad_RoadSegmentOverallProperties,
    MXT_UL_Modulations,
    MXTRoad_OT_AddModulation,
    MXTRoad_OT_RemoveModulation,
    MXTRoad_OT_SelectHelper,
    MXTRoad_OT_ConvertSegmentType,
    MXTRoad_OT_RespaceCPTimes,
    MXTRoad_OT_CreateRoadSegment,
    MXTRoad_OT_AddControlPoint,
    MXTRoad_OT_UpdatePathVisuals,
    MXT_GGT_CPHandleGizmos,
    MXTRoad_PT_MainPanel,
    MXTRoad_OT_GenerateCurveMatrix,
    MXTRoad_OT_GenerateMesh,
    MXTRoad_OT_GenerateCheckpoints,
    MXTRoad_OT_ExportTrackStub,
)
def register():
    global mxt_roads_pending_visual_update, mxt_timer_is_active, _timer_live
    mxt_roads_pending_visual_update = set()
    mxt_timer_is_active = False
    _timer_live = False
    for cls in classes_to_register: bpy.utils.register_class(cls)
    bpy.types.Object.mxt_road_overall_props = PointerProperty(type=MXTRoad_RoadSegmentOverallProperties)
    bpy.types.Object.mxt_cp_data = PointerProperty(type=MXTRoad_ControlPointData)
    bpy.types.Object.mxt_line_handle_data = PointerProperty(type=MXTRoad_LineHandleData)
    handlers = bpy.app.handlers.depsgraph_update_post
    if mxt_on_depsgraph_update not in handlers: handlers.append(mxt_on_depsgraph_update)
    global _mxt_draw_handle
    _mxt_draw_handle = bpy.types.SpaceView3D.draw_handler_add(
        mxt_draw_callback, (), 'WINDOW', 'POST_VIEW')
    print("MXT Road Creator (v0.1.0) Registered")
def unregister():
    global mxt_roads_pending_visual_update, mxt_timer_is_active
    global _mxt_draw_handle
    if _mxt_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_mxt_draw_handle, 'WINDOW')
        _mxt_draw_handle = None
    if mxt_timer_is_active:
        try: bpy.app.timers.unregister(_process_pending_visual_updates)
        except ValueError: pass
        mxt_timer_is_active = False
    mxt_roads_pending_visual_update.clear()
    handlers = bpy.app.handlers.depsgraph_update_post
    global _timer_live
    if _timer_live:
        try: bpy.app.timers.unregister(_process_live_updates)
        except ValueError: pass
    _timer_live = False
    _cm_pending.clear(); _mesh_pending.clear()
    if mxt_on_depsgraph_update in handlers: handlers.remove(mxt_on_depsgraph_update)
    if hasattr(bpy.types.Object, "mxt_cp_data"): del bpy.types.Object.mxt_cp_data
    if hasattr(bpy.types.Object, "mxt_line_handle_data"): del bpy.types.Object.mxt_line_handle_data
    if hasattr(bpy.types.Object, "mxt_road_overall_props"): del bpy.types.Object.mxt_road_overall_props
    for cls in reversed(classes_to_register): bpy.utils.unregister_class(cls)
    print("MXT Road Creator (v0.1.0) Unregistered")
if __name__ == "__main__":
    try: unregister()
    except Exception: pass
    register()