@tool
extends EditorScript

const track_filename := "test_track"

# Called when the script is executed (using File -> Run in Script Editor).
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
	
	# create data first, then create the header and put it at the top
	# this lets us more easily get offsets to specific data sections
	
	var data := StreamPeerBufferExtension.new()
	data.resize(1024 * 1024 * 16)
	
	# every checkpoint is born equal
	for i in checkpoints.size():
		var cur_cp := checkpoints[i]
		data.put_checkpoint(cur_cp)
		# replace this with proper checkpoint references
		# once i implement proper concurrent track segments
		# in the editor
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
	
	# segments are a little more complex
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
