import bpy
from bpy.props import BoolProperty, StringProperty, PointerProperty

# Mapping from socket.type to valid NodeTreeInterface socket_type identifiers
SOCKET_TYPE_MAP = {
    'VALUE': 'NodeSocketFloat',
    'INT': 'NodeSocketInt',
    'BOOLEAN': 'NodeSocketBool',
    'VECTOR': 'NodeSocketVector',
    'RGBA': 'NodeSocketColor',
    'ROTATION': 'NodeSocketRotation',
    'MATRIX': 'NodeSocketMatrix',
    'STRING': 'NodeSocketString',
    'MENU': 'NodeSocketMenu',
    'GEOMETRY': 'NodeSocketGeometry',
    'OBJECT': 'NodeSocketObject',
    'COLLECTION': 'NodeSocketCollection',
    'IMAGE': 'NodeSocketImage',
    'MATERIAL': 'NodeSocketMaterial',
    'FONT': 'NodeSocketFont',
    'SOUND': 'NodeSocketSound',
    'BUNDLE': 'NodeSocketBundle',
    'CLOSURE': 'NodeSocketClosure',
}

IGNORABLE_NODE_TYPES = {
    'NodeGroupInput',
    'NodeGroupOutput',
    'NodeFrame',
    'NodeReroute',
    'GROUP_INPUT',
    'GROUP_OUTPUT',
    'FRAME',
    'REROUTE',
}

# Node properties that describe placement/identity rather than behaviour; never copied to moved nodes
COPY_SKIP_PROPS = {'name', 'location', 'location_absolute', 'parent', 'select', 'active_item'}

# Constant input nodes whose value lives in a node property (the Value node keeps it on its output socket)
CONSTANT_NODE_PROPS = {
    'FunctionNodeInputInt': 'integer',
    'FunctionNodeInputBool': 'boolean',
    'FunctionNodeInputVector': 'vector',
    'FunctionNodeInputString': 'string',
}


class GripExposeSettings(bpy.types.PropertyGroup):
    """Settings stored on the scene for the Expose Child Inputs panel"""
    use_parent_panel: BoolProperty(
        name="Wrap in Master Panel",
        description="Wrap all child group sub-panels inside a single master parent panel",
        default=True,
    )

    parent_panel_name: StringProperty(
        name="Master Panel Name",
        description="Name of the master parent panel. Leave empty to use the active node group's name",
        default="",
    )

    use_sub_panels: BoolProperty(
        name="Sub-Panels per Group",
        description="Create an individual sub-panel for each selected node group",
        default=True,
    )

    index_duplicate_panels: BoolProperty(
        name="Index Same-Name Panels",
        description=(
            "When a panel's name is already taken inside the same parent panel (e.g. several instances of one "
            "group), give it an index (.001, .002, ...) instead of merging into the existing one. Re-running "
            "on a node still reuses the panels made for it"
        ),
        default=True,
    )

    create_new_group_input: BoolProperty(
        name="Create New Group Input Node",
        description="Always create a new dedicated Group Input node next to the selected nodes, instead of reusing an existing one",
        default=False,
    )

    skip_linked: BoolProperty(
        name="Skip Linked Sockets",
        description="Only expose inputs that are not already wired",
        default=True,
    )

    extract_linked: BoolProperty(
        name="Move Linked Nodes Outside",
        description=(
            "Also expose inputs fed by other nodes: the feeding nodes (and everything upstream of them) are moved "
            "out of the group, next to every instance of it, and wired into the new sockets"
        ),
        default=False,
    )

    only_exposed: BoolProperty(
        name="Only Visible Sockets",
        description="Skip sockets that are hidden on the node",
        default=True,
    )


def get_active_node_tree(context):
    """Returns the node tree currently visible in the active Node Editor."""
    space = context.space_data
    if space and space.type == 'NODE_EDITOR':
        if hasattr(space, 'edit_tree') and space.edit_tree:
            return space.edit_tree
        if hasattr(space, 'node_tree') and space.node_tree:
            return space.node_tree
    return None


def get_node_visual_pos(node):
    """Computes absolute visual (X, Y) coordinates in the editor, taking frames into account."""
    x = node.location.x
    y = node.location.y
    curr = getattr(node, 'parent', None)
    while curr:
        x += curr.location.x
        y += curr.location.y
        curr = getattr(curr, 'parent', None)
    return x, y


def get_selected_node_groups(tree):
    """Returns selected node groups sorted top-to-bottom in Y axis."""
    if not tree or not hasattr(tree, 'nodes'):
        return []
    nodes = [
        n for n in tree.nodes
        if n.select and n.type not in IGNORABLE_NODE_TYPES and n.bl_idname not in IGNORABLE_NODE_TYPES
    ]
    nodes.sort(key=lambda n: (-get_node_visual_pos(n)[1], get_node_visual_pos(n)[0]))
    return nodes


def is_root_panel(panel):
    """True for None and for the hidden root panel Blender reports as the parent of top-level items."""
    return panel is None or panel.index < 0


def is_child_of(item, parent_panel):
    """True if item sits directly inside parent_panel (None = top level)."""
    if parent_panel is None:
        return is_root_panel(item.parent)
    return item.parent == parent_panel


def get_panel_path(item):
    """Returns the panels containing an interface item, outermost first."""
    path = []
    panel = item.parent
    while not is_root_panel(panel):
        path.append(panel)
        panel = panel.parent
    return path[::-1]


def find_interface_input(tree, identifier):
    """Returns the input socket item with this identifier from a group's interface, or None."""
    for item in tree.interface.items_tree:
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.identifier == identifier:
            return item
    return None


def get_panel_socket_identifiers(panel):
    """Identifiers of all sockets inside panel, including nested panels."""
    identifiers = set()
    for item in panel.interface_items:
        if item.item_type == 'SOCKET':
            identifiers.add(item.identifier)
        else:
            identifiers |= get_panel_socket_identifiers(item)
    return identifiers


def panel_feeds_node(tree, panel, node):
    """True if a socket in panel is already wired from a Group Input into node, i.e. the panel was made for it."""
    identifiers = get_panel_socket_identifiers(panel)
    return any(
        link.from_node.bl_idname == 'NodeGroupInput'
        and link.from_socket.identifier in identifiers
        and link.to_node.name == node.name
        for link in tree.links
    )


def get_or_create_root_panel(tree, panel_name):
    """Finds an existing top-level panel by name or creates a new one."""
    if not panel_name:
        return None
    for item in tree.interface.items_tree:
        if getattr(item, 'item_type', None) == 'PANEL' and item.name == panel_name and is_child_of(item, None):
            return item
    try:
        return tree.interface.new_panel(panel_name, default_closed=False)
    except Exception as e:
        print(f"Warning: Could not create root panel '{panel_name}': {e}")
        return None


def get_sibling_panels(tree, parent_panel):
    """Panels sitting directly inside parent_panel (None = top level)."""
    return [
        item for item in tree.interface.items_tree
        if getattr(item, 'item_type', None) == 'PANEL' and is_child_of(item, parent_panel)
    ]


def find_sub_panel(tree, sub_panel_name, parent_panel=None, node=None, index_duplicates=False):
    """Returns the existing panel in parent_panel to reuse for sub_panel_name, or None.
    With index_duplicates, only a panel made for node counts (same name or an indexed variant of it)."""
    prefix = sub_panel_name + "."
    for item in get_sibling_panels(tree, parent_panel):
        if not index_duplicates:
            if item.name == sub_panel_name:
                return item
        elif (item.name == sub_panel_name or (item.name.startswith(prefix) and item.name[len(prefix):].isdigit())) \
                and node is not None and panel_feeds_node(tree, item, node):
            return item
    return None


def get_or_create_sub_panel(tree, sub_panel_name, parent_panel=None, node=None, index_duplicates=False, template=None):
    """Finds or creates a sub-panel, nesting it inside parent_panel if provided."""
    if not sub_panel_name:
        return parent_panel
    panel = find_sub_panel(tree, sub_panel_name, parent_panel, node, index_duplicates)
    if panel is None:
        panel = create_sub_panel(tree, sub_panel_name, parent_panel, index_duplicates, template)
    return panel


def create_sub_panel(tree, sub_panel_name, parent_panel=None, index_duplicates=False, template=None):
    """Creates a sub-panel at the end of parent_panel.
    With index_duplicates, a name already taken in parent_panel gets the first free index (name.001, name.002, ...).
    The panel takes default_closed and description from template (a child group's panel) if given."""
    if index_duplicates:
        taken = {item.name for item in get_sibling_panels(tree, parent_panel)}
        base_name, index = sub_panel_name, 1
        while sub_panel_name in taken:
            sub_panel_name = f"{base_name}.{index:03d}"
            index += 1

    try:
        panel = tree.interface.new_panel(sub_panel_name, default_closed=False)
        if template is not None:
            panel.default_closed = template.default_closed
            panel.description = template.description
        if parent_panel and hasattr(tree.interface, 'move_to_parent'):
            pos = len(parent_panel.interface_items) if hasattr(parent_panel, 'interface_items') else 0
            tree.interface.move_to_parent(panel, parent_panel, pos)
        return panel
    except Exception as e:
        print(f"Warning: Could not create sub-panel '{sub_panel_name}': {e}")
        return parent_panel


def get_exposed_inputs(tree, node):
    """Maps node's input identifiers to the interface sockets already wired into them from a Group Input."""
    interface_sockets = {
        item.identifier: item for item in tree.interface.items_tree
        if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
    }
    return {
        link.to_socket.identifier: interface_sockets[link.from_socket.identifier]
        for link in tree.links
        if link.to_node.name == node.name and link.from_node.bl_idname == 'NodeGroupInput'
        and link.from_socket.identifier in interface_sockets
    }


def find_exposed_counterpart(tree, child_item, parent, node, exposed, index_duplicates):
    """Returns the item inside parent that was exposed from child_item (a socket or panel of node's group), or None."""
    if child_item.item_type == 'PANEL':
        return find_sub_panel(tree, child_item.name, parent, node, index_duplicates)
    other = exposed.get(child_item.identifier)
    return other if other is not None and other.parent == parent else None


def place_like_source(tree, item, child_item, node, exposed, index_duplicates):
    """Moves a newly exposed item right after the exposed copy of the nearest earlier item in the child group
    (or before the nearest later one), so inputs added to a child later keep its order instead of being appended.
    Blender always keeps sockets above sub-panels, so only items of the same kind serve as anchors."""
    siblings = [
        i for i in child_item.parent.interface_items
        if i.item_type == child_item.item_type and not (i.item_type == 'SOCKET' and i.in_out == 'OUTPUT')
    ]
    index = next(k for k, i in enumerate(siblings) if i == child_item)
    for sibling in reversed(siblings[:index]):
        other = find_exposed_counterpart(tree, sibling, item.parent, node, exposed, index_duplicates)
        if other is not None:
            tree.interface.move(item, other.position + 1)
            return
    for sibling in siblings[index + 1:]:
        other = find_exposed_counterpart(tree, sibling, item.parent, node, exposed, index_duplicates)
        if other is not None:
            tree.interface.move(item, other.position)
            return


def get_or_create_group_input(tree, target_nodes, force_new=False):
    """Finds an existing Group Input node or creates a new one positioned to the left of targets."""
    if not force_new:
        group_inputs = [n for n in tree.nodes if n.type == 'GROUP_INPUT' or n.bl_idname == 'NodeGroupInput']

        for gin in group_inputs:
            if gin.select:
                return gin

        if group_inputs:
            return group_inputs[0]

    # Create a fresh Group Input node positioned to the left of the topmost target
    gin = tree.nodes.new('NodeGroupInput')
    if target_nodes:
        min_x = min(get_node_visual_pos(n)[0] for n in target_nodes)
        max_y = max(get_node_visual_pos(n)[1] for n in target_nodes)
        gin.location = (min_x - 300, max_y)
    else:
        gin.location = (-300, 0)
    return gin


def assign_value(target, val):
    """Sets target.default_value, converting arrays (vectors, colors) to lists. Silently ignores type mismatches."""
    if not hasattr(target, 'default_value'):
        return
    try:
        if hasattr(val, '__iter__') and not isinstance(val, str):
            target.default_value = list(val)
        else:
            target.default_value = val
    except Exception:
        pass


def copy_socket_attributes(source_sock, interface_sock, child_node):
    """Copies default values, limits, and subtypes from the child pin to the interface socket."""
    # 1. Inherit value from the child node input pin
    if hasattr(source_sock, 'default_value'):
        assign_value(interface_sock, source_sock.default_value)

    # 2. If the child is a node group, inspect its own interface definition for limits/subtypes
    if hasattr(child_node, 'node_tree') and child_node.node_tree and hasattr(child_node.node_tree, 'interface'):
        # By identifier: names can repeat across the child's panels
        matching_item = find_interface_input(child_node.node_tree, source_sock.identifier)

        if matching_item:
            for attr in ('subtype', 'min_value', 'max_value', 'description', 'is_panel_toggle'):
                if hasattr(matching_item, attr) and hasattr(interface_sock, attr):
                    try:
                        setattr(interface_sock, attr, getattr(matching_item, attr))
                    except Exception:
                        pass


def sync_socket_value_to_instances(tree, sock_item):
    """Propagates the newly exposed default value to all existing instances of this group across the file."""
    if not hasattr(sock_item, 'default_value'):
        return

    val = sock_item.default_value
    for ng in bpy.data.node_groups:
        for n in ng.nodes:
            if getattr(n, 'node_tree', None) == tree:
                sock = n.inputs.get(sock_item.identifier) or n.inputs.get(sock_item.name)
                if sock:
                    assign_value(sock, val)


# ---------------------------------------------------------------------------
# Moving linked feeder nodes outside the group
# ---------------------------------------------------------------------------

def find_socket(sockets, identifier, index=None):
    """Finds a socket by identifier (names can repeat, e.g. on Math nodes), falling back to its index."""
    for sock in sockets:
        if sock.identifier == identifier:
            return sock
    if index is not None and index < len(sockets):
        return sockets[index]
    return None


def resolve_source_socket(link):
    """Follows reroutes back to the output socket that actually produces the value.
    Returns (socket, names of the reroutes passed on the way)."""
    sock = link.from_socket
    reroutes = []
    while sock.node.bl_idname == 'NodeReroute' and sock.node.inputs[0].links:
        reroutes.append(sock.node.name)
        sock = sock.node.inputs[0].links[0].from_socket
    return sock, reroutes


def collect_upstream_nodes(node):
    """Returns {name: node} for node and everything feeding it, stopping at Group Input nodes."""
    found = {}
    stack = [node]
    while stack:
        n = stack.pop()
        if n.name in found or n.bl_idname == 'NodeGroupInput':
            continue
        found[n.name] = n
        for inp in n.inputs:
            for link in inp.links:
                stack.append(link.from_node)
    return found


def get_zone_node_names(tree):
    """Names of zone input/output nodes (Repeat, Simulation, For Each, Closure); they can't be moved on their own."""
    names = set()
    for n in tree.nodes:
        paired = getattr(n, 'paired_output', None)
        if paired:
            names.add(n.name)
            names.add(paired.name)
    return names


def find_group_instances(tree):
    """Returns (parent_tree, group_node) for every editable instance of tree in the file."""
    return [
        (ng, n)
        for ng in bpy.data.node_groups if ng.library is None
        for n in ng.nodes if getattr(n, 'node_tree', None) == tree
    ]


def get_constant_value(source):
    """Returns the value of a constant input node (Value, Integer, ...) feeding source, or None if it's computed."""
    node = source.node
    if node.bl_idname == 'ShaderNodeValue':
        return source.default_value
    prop = CONSTANT_NODE_PROPS.get(node.bl_idname)
    return getattr(node, prop, None) if prop else None


def copy_rna_props(src, dst, skip=()):
    """Copies every writable, non-collection RNA property from src to dst."""
    for prop in src.bl_rna.properties:
        ident = prop.identifier
        if prop.is_readonly or prop.type == 'COLLECTION' or ident in skip or ident.startswith('bl_'):
            continue
        try:
            setattr(dst, ident, getattr(src, ident))
        except Exception:
            pass


def copy_dynamic_items(src, dst):
    """Rebuilds dynamic socket item collections (menu entries, index switch, capture, bundle items, ...)."""
    for prop in src.bl_rna.properties:
        if prop.type != 'COLLECTION' or not prop.identifier.endswith('_items'):
            continue
        dst_items = getattr(dst, prop.identifier)
        dst_items.clear()
        for item in getattr(src, prop.identifier):
            name = getattr(item, 'name', '')
            # .new() signatures differ per collection: (socket_type, name), (name) or ()
            attempts = [(getattr(item, a), name) for a in ('socket_type', 'data_type') if hasattr(item, a)]
            attempts += [(name,), ()]
            for args in attempts:
                try:
                    new_item = dst_items.new(*args)
                except Exception:
                    continue
                copy_rna_props(item, new_item)
                break


def copy_node(src, dst_tree, location):
    """Recreates src (settings, dynamic items, socket values) as a new node in dst_tree."""
    dst = dst_tree.nodes.new(src.bl_idname)
    copy_rna_props(src, dst, COPY_SKIP_PROPS)
    copy_dynamic_items(src, dst)
    # Second pass: some settings are only valid once data types / items from the first pass exist
    copy_rna_props(src, dst, COPY_SKIP_PROPS)
    for src_socks, dst_socks in ((src.inputs, dst.inputs), (src.outputs, dst.outputs)):
        for i, s in enumerate(src_socks):
            d = find_socket(dst_socks, s.identifier, i)
            if d is None:
                continue
            d.hide = s.hide
            if hasattr(s, 'default_value'):
                assign_value(d, s.default_value)
    dst.location = location
    return dst


def copy_nodes_to_instance(tree, nodes, extracted, parent_tree, instance):
    """Recreates nodes from inside tree to the left of instance (in parent_tree), keeping their layout,
    and wires them into the instance's newly exposed sockets."""
    positions = {name: get_node_visual_pos(n) for name, n in nodes.items()}
    right = max(positions[name][0] + n.width for name, n in nodes.items())
    top = max(y for _, y in positions.values())
    inst_x, inst_y = get_node_visual_pos(instance)
    off_x = inst_x - 80 - right
    off_y = inst_y - top

    copies = {
        name: copy_node(n, parent_tree, (positions[name][0] + off_x, positions[name][1] + off_y))
        for name, n in nodes.items()
    }

    for link in tree.links:
        to_copy = copies.get(link.to_node.name)
        if to_copy is None:
            continue
        to_sock = find_socket(to_copy.inputs, link.to_socket.identifier)
        from_copy = copies.get(link.from_node.name)
        if from_copy is not None:
            parent_tree.links.new(find_socket(from_copy.outputs, link.from_socket.identifier), to_sock)
        elif link.from_node.bl_idname == 'NodeGroupInput':
            # Fed by the group's own input: take whatever feeds that input on this instance
            inst_sock = find_socket(instance.inputs, link.from_socket.identifier)
            if inst_sock is None:
                continue
            if inst_sock.links:
                parent_tree.links.new(inst_sock.links[0].from_socket, to_sock)
            elif hasattr(inst_sock, 'default_value'):
                assign_value(to_sock, inst_sock.default_value)

    for source, sock_item in extracted.values():
        parent_tree.links.new(
            find_socket(copies[source.node.name].outputs, source.identifier),
            find_socket(instance.inputs, sock_item.identifier),
        )
    return len(copies)


def remove_orphaned_nodes(tree, names):
    """Deletes the named nodes once nothing reads their outputs anymore. Returns the names that had to stay."""
    remaining = set(names)
    changed = True
    while changed:
        changed = False
        used = {link.from_node.name for link in tree.links}
        for name in list(remaining):
            if name not in used:
                tree.nodes.remove(tree.nodes[name])
                remaining.discard(name)
                changed = True
    return remaining


class NODE_OT_expose_child_inputs(bpy.types.Operator):
    """Expose inputs of selected node groups to Group Input with nested Interface Panels, optionally moving linked feeder nodes outside the group"""
    bl_idname = "node.expose_child_inputs"
    bl_label = "Expose Child Inputs"
    bl_options = {'REGISTER', 'UNDO'}

    use_parent_panel: BoolProperty(name="Wrap in Master Panel", default=True)
    parent_panel_name: StringProperty(name="Master Panel Name", default="")
    use_sub_panels: BoolProperty(name="Sub-Panels per Group", default=True)
    index_duplicate_panels: BoolProperty(name="Index Same-Name Panels", default=True)
    create_new_group_input: BoolProperty(name="Create New Group Input Node", default=False)
    skip_linked: BoolProperty(name="Skip Linked Sockets", default=True)
    extract_linked: BoolProperty(name="Move Linked Nodes Outside", default=False)
    only_exposed: BoolProperty(name="Only Visible Sockets", default=True)

    @classmethod
    def poll(cls, context):
        tree = get_active_node_tree(context)
        return tree is not None and bool(get_selected_node_groups(tree))

    def invoke(self, context, event):
        if hasattr(context.scene, 'grip_expose_settings'):
            s = context.scene.grip_expose_settings
            self.use_parent_panel = s.use_parent_panel
            self.parent_panel_name = s.parent_panel_name
            self.use_sub_panels = s.use_sub_panels
            self.index_duplicate_panels = s.index_duplicate_panels
            self.create_new_group_input = s.create_new_group_input
            self.skip_linked = s.skip_linked
            self.extract_linked = s.extract_linked
            self.only_exposed = s.only_exposed
        return self.execute(context)

    def execute(self, context):
        tree = get_active_node_tree(context)
        if not tree:
            self.report({'ERROR'}, "No active node tree open in the Node Editor")
            return {'CANCELLED'}

        if hasattr(context.scene, 'grip_expose_settings'):
            s = context.scene.grip_expose_settings
            self.use_parent_panel = s.use_parent_panel
            self.parent_panel_name = s.parent_panel_name
            self.use_sub_panels = s.use_sub_panels
            self.index_duplicate_panels = s.index_duplicate_panels
            self.create_new_group_input = s.create_new_group_input
            self.skip_linked = s.skip_linked
            self.extract_linked = s.extract_linked
            self.only_exposed = s.only_exposed

        selected_nodes = get_selected_node_groups(tree)
        if not selected_nodes:
            self.report({'WARNING'}, "No node groups selected. Please select at least one node group.")
            return {'CANCELLED'}

        # Determine Parent Panel
        parent_panel = None
        if self.use_parent_panel:
            root_name = self.parent_panel_name.strip() if self.parent_panel_name.strip() else tree.name
            parent_panel = get_or_create_root_panel(tree, root_name)

        # Get or create Group Input node (respecting create_new_group_input)
        gin = get_or_create_group_input(tree, selected_nodes, force_new=self.create_new_group_input)
        connected_count = 0
        nodes_processed = 0

        # Moving linked nodes outside needs somewhere to put them: next to every instance of this group
        instances = find_group_instances(tree) if self.extract_linked else []
        selected_names = {n.name for n in selected_nodes}
        zone_names = get_zone_node_names(tree) if instances else set()
        extracted = {}      # source socket pointer -> (source output socket, interface socket item)
        nodes_to_move = {}  # name -> node: everything upstream of the extracted sources
        passed_reroutes = set()  # reroutes between sources and inputs; removed if left dangling

        for node in selected_nodes:
            sub_panel_name = ""
            if self.use_sub_panels:
                if node.label:
                    sub_panel_name = node.label
                elif hasattr(node, 'node_tree') and node.node_tree:
                    sub_panel_name = node.node_tree.name
                else:
                    sub_panel_name = node.name

            target_panel = get_or_create_sub_panel(
                tree, sub_panel_name, parent_panel, node, self.index_duplicate_panels
            ) if self.use_sub_panels else parent_panel
            exposed = get_exposed_inputs(tree, node)  # from earlier runs; used to slot new sockets in source order

            node_had_exposure = False
            for inp in node.inputs:
                if self.only_exposed and inp.hide:
                    continue
                if hasattr(inp, 'enabled') and not inp.enabled:
                    continue

                source = None
                if instances and inp.links and not inp.is_multi_input:
                    source, reroutes = resolve_source_socket(inp.links[0])
                    upstream = collect_upstream_nodes(source.node)
                    # Leave alone inputs already fed by the group's own inputs, wiring between selected
                    # nodes, and zones (their input/output pair can't be split)
                    if source.node.bl_idname == 'NodeGroupInput' or any(
                        name in selected_names or name in zone_names for name in upstream
                    ):
                        source = None

                if source is None and self.skip_linked and inp.is_linked:
                    continue

                socket_type = SOCKET_TYPE_MAP.get(inp.type, 'NodeSocketFloat')
                socket_name = inp.name

                if source:
                    passed_reroutes.update(reroutes)
                is_new_socket = not (source and source.as_pointer() in extracted)
                if is_new_socket:
                    # Recreate the child group's own panels (outermost first) so its structure carries over
                    socket_panel = target_panel
                    child_item = find_interface_input(node.node_tree, inp.identifier) if getattr(node, 'node_tree', None) else None
                    for child_panel in (get_panel_path(child_item) if child_item else []):
                        panel = find_sub_panel(tree, child_panel.name, socket_panel, node, self.index_duplicate_panels)
                        if panel is None:
                            panel = create_sub_panel(
                                tree, child_panel.name, socket_panel, self.index_duplicate_panels, child_panel
                            )
                            place_like_source(tree, panel, child_panel, node, exposed, self.index_duplicate_panels)
                        socket_panel = panel

                    sock_item = tree.interface.new_socket(
                        name=socket_name,
                        in_out='INPUT',
                        socket_type=socket_type,
                        parent=socket_panel,
                    )
                else:
                    # Same feeder already exposed for another input: share its socket, as the original wire did
                    sock_item = extracted[source.as_pointer()][1]

                gin_output = gin.outputs.get(sock_item.identifier)
                if gin_output:
                    tree.links.new(gin_output, inp)
                    exposed[inp.identifier] = sock_item
                    connected_count += 1
                    node_had_exposure = True

                if is_new_socket:
                    # Done after linking: a Menu socket only gets its items (and so accepts a value)
                    # once it's wired to the child's menu
                    copy_socket_attributes(inp, sock_item, node)
                    if source:
                        constant = get_constant_value(source)
                        if constant is not None:
                            assign_value(sock_item, constant)
                        extracted[source.as_pointer()] = (source, sock_item)
                        nodes_to_move.update(upstream)
                    sync_socket_value_to_instances(tree, sock_item)
                    if child_item:
                        place_like_source(tree, sock_item, child_item, node, exposed, self.index_duplicate_panels)

            if node_had_exposure:
                nodes_processed += 1

        report_type = {'INFO'}
        moved_info = ""
        if extracted:
            for parent_tree, instance in instances:
                copy_nodes_to_instance(tree, nodes_to_move, extracted, parent_tree, instance)

            # Anything else inside that read an extracted value now reads it from the group input too
            for source, sock_item in extracted.values():
                gin_output = gin.outputs.get(sock_item.identifier)
                for link in source.links:
                    to_socket = link.to_socket
                    tree.links.remove(link)
                    tree.links.new(gin_output, to_socket)

            kept = remove_orphaned_nodes(tree, set(nodes_to_move) | passed_reroutes) & set(nodes_to_move)
            moved_info = (
                f" Moved {len(nodes_to_move) - len(kept)} linked node(s) outside, "
                f"next to {len(instances)} instance(s)."
            )
            if kept:
                moved_info += f" Kept {len(kept)} inside, still used by other nodes: {', '.join(sorted(kept))}."
        elif self.extract_linked and not instances:
            report_type = {'WARNING'}
            moved_info = f" '{tree.name}' has no instances to move linked nodes next to, so they were left inside."

        parent_info = f" inside master panel '{parent_panel.name}'" if parent_panel else ""
        self.report(
            report_type,
            f"Exposed {connected_count} socket(s) from {nodes_processed} node group(s){parent_info}.{moved_info}"
        )
        return {'FINISHED'}


class NODE_PT_grip_expose_panel(bpy.types.Panel):
    """Dedicated Node Wrangler style panel in the Node Editor Sidebar (N > Grip)"""
    bl_label = "Expose Child Inputs"
    bl_idname = "NODE_PT_grip_expose_panel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Grip"

    def draw(self, context):
        layout = self.layout
        tree = get_active_node_tree(context)

        if not tree:
            layout.label(text="No active node tree open", icon='INFO')
            return

        settings = context.scene.grip_expose_settings
        selected_nodes = get_selected_node_groups(tree)

        # Header: Active tree name
        header_box = layout.box()
        row = header_box.row()
        row.label(text=f"Active: {tree.name}", icon='NODETREE')

        # Selection info
        sel_box = layout.box()
        if selected_nodes:
            sel_box.label(text=f"Selected ({len(selected_nodes)} in Y order):", icon='CHECKBOX_HLT')
            col = sel_box.column(align=True)
            for n in selected_nodes:
                name = n.label if n.label else (n.node_tree.name if hasattr(n, 'node_tree') and n.node_tree else n.name)
                col.label(text=f" • {name}", icon='DOT')
        else:
            sel_box.label(text="No node groups selected", icon='INFO')
            sel_box.label(text="Select groups in editor to expose", icon='BLANK1')

        # Settings
        opt_box = layout.box()
        opt_box.label(text="Interface Options:", icon='PREFERENCES')
        col = opt_box.column(align=True)
        col.prop(settings, "use_parent_panel")
        if settings.use_parent_panel:
            sub = col.column(align=True)
            sub.prop(settings, "parent_panel_name", text="Name", placeholder=tree.name)
        col.prop(settings, "use_sub_panels")
        if settings.use_sub_panels:
            sub = col.column(align=True)
            sub.prop(settings, "index_duplicate_panels")

        col.separator()
        col.prop(settings, "create_new_group_input", icon='DUPLICATE')
        col.prop(settings, "skip_linked")
        col.prop(settings, "extract_linked")
        col.prop(settings, "only_exposed")

        # Action Button
        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 1.4

        if selected_nodes:
            col.operator("node.expose_child_inputs", text="Expose Selected to Group Input", icon='LINKED')
        else:
            sub = col.row()
            sub.enabled = False
            sub.operator("node.expose_child_inputs", text="Select Node Groups to Expose", icon='RESTRICT_SELECT_ON')


classes = (
    GripExposeSettings,
    NODE_OT_expose_child_inputs,
    NODE_PT_grip_expose_panel,
)


def register():
    unregister()
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass
    bpy.types.Scene.grip_expose_settings = PointerProperty(type=GripExposeSettings)


def unregister():
    if hasattr(bpy.types.Scene, 'grip_expose_settings'):
        try:
            del bpy.types.Scene.grip_expose_settings
        except Exception:
            pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
