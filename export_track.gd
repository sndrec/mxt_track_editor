@tool
extends EditorScript

const track_filename := "test_track"

func _run() -> void:
	var track_root : TrackRoot
	var scene_root := FZGlobalTrackEditor.track_editor.get_tree().edited_scene_root
	var editor_cam:Camera3D = EditorInterface.get_editor_viewport_3d(0).get_camera_3d()
	if !scene_root:
		return
	for child in scene_root.get_children():
		if child is TrackRoot:
			track_root = child
			break
	if !track_root:
		return
	var checkpoints := track_root.checkpoints
	var segments := track_root.get_children()
	
	var data := StreamPeerBufferExtension.new()
	data.resize(1024 * 1024 * 16)
	
	# every checkpoint is born equal
	for i in checkpoints.size():
		var cur_cp := checkpoints[i]
		data.put_checkpoint(cur_cp)
		# replace this with proper checkpoint references
		# utilizing the prev/next segment tables
		# this is just a placeholder
		if i == 0:
			data.put_u32(1)
			data.put_u32(i + 1)
		elif i == checkpoints.size() - 1:
			data.put_u32(1)
			data.put_u32(i - 1)
		else:
			data.put_u32(2)
			data.put_u32(i + 1)
			data.put_u32(i - 1)
	
	for i in segments.size():
		var segment : RoadPath = segments[i]
		data.put_u32(i)
		var road_type := 0
		if segment.road_shape is RoadShapeCylinder:
			road_type = 1
		elif segment.road_shape is RoadShapeCylinderOpen:
			road_type = 2
		elif segment.road_shape is RoadShapePipe:
			road_type = 3
		elif segment.road_shape is RoadShapePipeOpen:
			road_type = 4
		data.put_u32(road_type)
		match road_type:
			2:
				data.put_curve(segment.road_shape.openness)
			4:
				data.put_curve(segment.road_shape.openness)
		
		data.put_u32(segment.road_shape.modulation_table.size()) # number of road modulations
		for n in segment.road_shape.modulation_table.size():
			var modulation := segment.road_shape.modulation_table[n]
			data.put_curve(modulation.modulation_effect)
			data.put_curve(modulation.modulation_width)
		
		data.put_u32(segment.road_shape.embed_table.size()) # number of embeds
		for n in segment.road_shape.embed_table.size():
			var embed := segment.road_shape.embed_table[n]
			data.put_float(embed.road_start)
			data.put_float(embed.road_end)
			data.put_u32(embed.embed_type)
			data.put_curve(embed.left_boundary)
			data.put_curve(embed.right_boundary)
			
		data.put_curve(segment.road_curve.position_x)
		data.put_curve(segment.road_curve.position_y)
		data.put_curve(segment.road_curve.position_z)
		data.put_curve(segment.road_curve.rotation_xx)
		data.put_curve(segment.road_curve.rotation_xy)
		data.put_curve(segment.road_curve.rotation_xz)
		data.put_curve(segment.road_curve.rotation_yx)
		data.put_curve(segment.road_curve.rotation_yy)
		data.put_curve(segment.road_curve.rotation_yz)
		data.put_curve(segment.road_curve.rotation_zx)
		data.put_curve(segment.road_curve.rotation_zy)
		data.put_curve(segment.road_curve.rotation_zz)
		data.put_curve(segment.road_curve.scale_x)
		data.put_curve(segment.road_curve.scale_y)
		data.put_curve(segment.road_curve.scale_z)
		data.put_curve(segment.rail_height_left)
		data.put_curve(segment.rail_height_right)
	data.resize(data.get_position())
	
	var header := StreamPeerBuffer.new()
	header.resize(1024)
	
	header.put_string("v0.1") # version number string
	header.put_u32(checkpoints.size()) # number of collision checkpoints
	header.put_u32(segments.size()) # number of track segments
	header.resize(header.get_position())
	header.seek(0)
	header.put_u32(header.get_size())
	
	var final_data := StreamPeerBuffer.new()
	final_data.put_data(header.data_array)
	final_data.put_data(data.data_array)
	
	var save = FileAccess.open("user://" + track_filename + ".mxt_track", FileAccess.WRITE)
	save.store_buffer(final_data.data_array)
	print("saved track")

class_name StreamPeerBufferExtension extends StreamPeerBuffer

func get_vector3() -> Vector3:
	return Vector3(get_float(), get_float(), get_float())

func put_vector3(inVector : Vector3) -> void:
	put_float(inVector.x)
	put_float(inVector.y)
	put_float(inVector.z)

func put_quaternion(in_quat : Quaternion) -> void:
	put_float(in_quat.x)
	put_float(in_quat.y)
	put_float(in_quat.z)
	put_float(in_quat.w)

func get_basis() -> Basis:
	return Basis(get_vector3(), get_vector3(), get_vector3())

func put_basis(inBasis : Basis) -> void:
	put_vector3(inBasis.x)
	put_vector3(inBasis.y)
	put_vector3(inBasis.z)

func get_transform() -> Transform3D:
	return Transform3D(get_basis(), get_vector3())

func put_transform(inTransform : Transform3D) -> void:
	put_basis(inTransform.basis)
	put_vector3(inTransform.origin)

func put_curve(in_curve : Curve) -> void:
	put_u32(in_curve.point_count)
	for v in in_curve.point_count:
		put_float(in_curve.get_point_position(v).x)
		put_float(in_curve.get_point_position(v).y)
		put_float(in_curve.get_point_left_tangent(v))
		put_float(in_curve.get_point_right_tangent(v))

func get_curve() -> Curve:
	var new_curve := Curve.new()
	var point_count := get_u32()
	for i in point_count:
		new_curve.add_point(Vector2(get_float(), get_float()), get_float(), get_float())
	return new_curve

func put_checkpoint(in_checkpoint : Checkpoint) -> void:
	put_vector3(in_checkpoint.position_start)
	put_vector3(in_checkpoint.position_end)
	put_basis(in_checkpoint.orientation_start)
	put_basis(in_checkpoint.orientation_end)
	put_float(in_checkpoint.x_radius_start)
	put_float(in_checkpoint.y_radius_start)
	put_float(in_checkpoint.x_radius_end)
	put_float(in_checkpoint.y_radius_end)
	put_float(in_checkpoint.y_start)
	put_float(in_checkpoint.y_end)
	put_float(in_checkpoint.distance)
	put_u32(in_checkpoint.road_segment)
	put_vector3(in_checkpoint.start_plane.normal)
	put_float(in_checkpoint.start_plane.d)
	put_vector3(in_checkpoint.end_plane.normal)
	put_float(in_checkpoint.end_plane.d)

func get_checkpoint() -> Checkpoint:
	var new_checkpoint := Checkpoint.new()
	new_checkpoint.position_start = get_vector3()
	new_checkpoint.position_end = get_vector3()
	new_checkpoint.orientation_start = get_basis()
	new_checkpoint.orientation_end = get_basis()
	new_checkpoint.x_radius_start = get_float()
	new_checkpoint.y_radius_start = get_float()
	new_checkpoint.x_radius_end = get_float()
	new_checkpoint.y_radius_end = get_float()
	new_checkpoint.y_start = get_float()
	new_checkpoint.y_end = get_float()
	new_checkpoint.distance = get_float()
	new_checkpoint.road_segment = get_u32()
	new_checkpoint.start_plane = Plane(get_vector3(), get_float())
	new_checkpoint.end_plane = Plane(get_vector3(), get_float())
	return new_checkpoint
	
