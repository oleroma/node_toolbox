import textwrap
from itertools import combinations

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty, PointerProperty

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

# Nodes whose width isn't a plain node width: frames are sized by their content, reroutes have none
WIDTHLESS_NODE_TYPES = {'NodeFrame', 'NodeReroute'}

# Nodes that only shape the layout: they have no sockets to reorder, and links pass through reroutes
LAYOUT_NODE_TYPES = {'NodeFrame', 'NodeReroute'}

# Nodes whose sockets are made from lists of items, which can be reordered: bl_idname -> {side: item collections, in
# socket order}. Zone input nodes show the items stored on their paired output node. The Index Switch is left out on
# purpose: the order of its items is what the index picks from
ITEM_COLLECTIONS = {
    'NodeCombineBundle': {'INPUT': ('bundle_items',)},
    'NodeSeparateBundle': {'OUTPUT': ('bundle_items',)},
    'NodeEvaluateClosure': {'INPUT': ('input_items',), 'OUTPUT': ('output_items',)},
    'NodeClosureInput': {'OUTPUT': ('input_items',)},
    'NodeClosureOutput': {'INPUT': ('output_items',)},
    'FunctionNodeFormatString': {'INPUT': ('format_items',)},
    'GeometryNodeMenuSwitch': {'INPUT': ('enum_items',), 'OUTPUT': ('enum_items',)},
    'GeometryNodeCaptureAttribute': {'INPUT': ('capture_items',), 'OUTPUT': ('capture_items',)},
    'GeometryNodeBake': {'INPUT': ('bake_items',), 'OUTPUT': ('bake_items',)},
    'GeometryNodeViewer': {'INPUT': ('viewer_items',)},
    'GeometryNodeFieldToList': {'INPUT': ('list_items',), 'OUTPUT': ('list_items',)},
    'GeometryNodeFieldToGrid': {'INPUT': ('grid_items',), 'OUTPUT': ('grid_items',)},
    'GeometryNodeClosureToList': {'OUTPUT': ('list_items',)},
    'GeometryNodeRepeatInput': {'INPUT': ('repeat_items',), 'OUTPUT': ('repeat_items',)},
    'GeometryNodeRepeatOutput': {'INPUT': ('repeat_items',), 'OUTPUT': ('repeat_items',)},
    'GeometryNodeSimulationInput': {'INPUT': ('state_items',), 'OUTPUT': ('state_items',)},
    'GeometryNodeSimulationOutput': {'INPUT': ('state_items',), 'OUTPUT': ('state_items',)},
    'GeometryNodeForeachGeometryElementInput': {'INPUT': ('input_items',), 'OUTPUT': ('input_items',)},
    'GeometryNodeForeachGeometryElementOutput': {
        'INPUT': ('main_items', 'generation_items'),
        'OUTPUT': ('main_items', 'generation_items'),
    },
    'CompositorNodeOutputFile': {'INPUT': ('file_output_items',)},
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
        description=(
            "Only expose inputs that are not wired to other nodes. Inputs already fed by the Group Input "
            "are always skipped, so re-running only adds what's missing"
        ),
        default=True,
    )

    extract_linked: BoolProperty(
        name="Move Linked Nodes Outside",
        description=(
            "When Skip Linked Sockets is off, linked inputs are exposed by moving the feeding nodes (and everything "
            "upstream of them) out of the group, next to every instance of it, and wiring them into the new sockets. "
            "Without this, the existing links are replaced by the Group Input"
        ),
        default=False,
    )

    only_exposed: BoolProperty(
        name="Only Visible Sockets",
        description="Skip sockets that are hidden on the node",
        default=True,
    )


class GripToolboxSettings(bpy.types.PropertyGroup):
    """Settings stored on the scene for the Toolbox panel"""
    node_width: FloatProperty(
        name="Node Width",
        description="Width to give the selected nodes. Each node type clamps it to its own min/max",
        default=140.0,
        min=10.0,
        soft_max=700.0,
        step=1000,
        precision=0,
        subtype='PIXEL',
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


def get_node_title(node):
    """The name to show for a node: its label, else its group's name, else its name."""
    if node.label:
        return node.label
    group = getattr(node, 'node_tree', None)
    return group.name if group else node.name


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


def get_input_consumers(tree):
    """Maps interface input identifiers to the (node, input socket) pairs their Group Input outputs feed,
    following reroutes."""
    links_from = {}
    for link in tree.links:
        links_from.setdefault(link.from_node.name, []).append(link)
    consumers = {}
    for gin in tree.nodes:
        if gin.bl_idname != 'NodeGroupInput':
            continue
        for first in links_from.get(gin.name, ()):
            stack = [first]
            while stack:
                link = stack.pop()
                if link.to_node.bl_idname == 'NodeReroute':
                    stack.extend(links_from.get(link.to_node.name, ()))
                else:
                    consumers.setdefault(first.from_socket.identifier, []).append((link.to_node, link.to_socket))
    return consumers


def panel_feeds_node(panel, node, consumers):
    """True if a socket in panel already feeds node from a Group Input, i.e. the panel was made for it."""
    return any(
        target.name == node.name
        for identifier in get_panel_socket_identifiers(panel)
        for target, _ in consumers.get(identifier, ())
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


def find_sub_panel(tree, sub_panel_name, parent_panel, node, consumers, index_duplicates=False):
    """Returns the existing panel in parent_panel to reuse for sub_panel_name, or None.
    With index_duplicates, only a panel made for node counts (same name or an indexed variant of it)."""
    prefix = sub_panel_name + "."
    for item in get_sibling_panels(tree, parent_panel):
        if not index_duplicates:
            if item.name == sub_panel_name:
                return item
        elif (item.name == sub_panel_name or (item.name.startswith(prefix) and item.name[len(prefix):].isdigit())) \
                and panel_feeds_node(item, node, consumers):
            return item
    return None


def find_node_panel(tree, parent_panel, node, consumers):
    """Returns the panel in parent_panel already holding node's exposed inputs, even if it or the node was renamed
    since, or None. At top level only sockets named like the input they feed count."""
    names = {i.identifier: i.name for i in tree.interface.items_tree if i.item_type == 'SOCKET'}
    for panel in get_sibling_panels(tree, parent_panel):
        for identifier in get_panel_socket_identifiers(panel):
            for target, sock in consumers.get(identifier, ()):
                if target.name == node.name and (parent_panel is not None or sock.name == names.get(identifier)):
                    return panel
    return None


def get_or_create_sub_panel(tree, sub_panel_name, parent_panel, node, consumers, index_duplicates=False, template=None):
    """Finds or creates a sub-panel, nesting it inside parent_panel if provided."""
    if not sub_panel_name:
        return parent_panel
    panel = find_sub_panel(tree, sub_panel_name, parent_panel, node, consumers, index_duplicates)
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


def is_inside(item, panel):
    """True if item sits anywhere inside panel (None = anywhere in the interface)."""
    if panel is None:
        return True
    parent = item.parent
    while not is_root_panel(parent):
        if parent == panel:
            return True
        parent = parent.parent
    return False


def get_source_siblings(node, inp):
    """Returns (the inputs next to inp in its source, in order; inp's index among them). For a group node that's
    the inputs of inp's panel in the group, for any other node (Combine Bundle, Menu Switch, ...) its own inputs."""
    child_item = find_interface_input(node.node_tree, inp.identifier) if getattr(node, 'node_tree', None) else None
    if child_item:
        siblings = [i for i in child_item.parent.interface_items if i.item_type == 'SOCKET' and i.in_out == 'INPUT']
    else:
        siblings = [i for i in node.inputs if i.bl_idname != 'NodeSocketVirtual']
    return siblings, next(k for k, i in enumerate(siblings) if i.identifier == inp.identifier)


def find_exposed_neighbour(tree, siblings, index, node, consumers, within):
    """Returns the exposed socket of the nearest earlier (else later) input among siblings around index,
    searched inside within (None = top level, where only sockets named like their input count), or None.
    New inputs join their neighbours there, whatever layout those were exposed with."""
    interface_sockets = {i.identifier: i for i in tree.interface.items_tree if i.item_type == 'SOCKET'}
    exposed = {}  # node input identifier -> interface socket feeding it
    for identifier, targets in consumers.items():
        for target, sock in targets:
            if target.name == node.name and identifier in interface_sockets:
                exposed.setdefault(sock.identifier, interface_sockets[identifier])
    for sibling in list(reversed(siblings[:index])) + siblings[index + 1:]:
        other = exposed.get(sibling.identifier)
        if other is not None and is_inside(other, within) and (within is not None or other.name == sibling.name):
            return other
    return None


def get_layout_order(node, socket, cache):
    """Sort key placing a socket by its node's position (top-to-bottom, then left-to-right) and then by the socket's
    position on the node (a group node's sockets are in its group's interface order)."""
    sockets_key = (node.name, socket.is_output)
    if sockets_key not in cache:
        sockets = node.outputs if socket.is_output else node.inputs
        cache[sockets_key] = {s.identifier: i for i, s in enumerate(sockets)}
    x, y = get_node_visual_pos(node)
    return (-y, x, node.name, cache[sockets_key].get(socket.identifier, 0))


def sort_interface(tree, container, socket_key, dry_run=False):
    """Sorts the sockets and panels inside container (None = top level), recursively, by socket_key(item): a sort key,
    or None for a socket that keeps its place. A panel sorts by the smallest key inside it.
    Returns the number of items out of place, which are moved unless dry_run."""

    def item_key(item):
        if item.item_type == 'PANEL':
            keys = [key for key in map(item_key, item.interface_items) if key is not None]
            return min(keys) if keys else None
        return socket_key(item)

    def sort_level(items):
        desired = list(items)
        # Blender keeps sockets above panels, so each kind is sorted within the slots it already occupies.
        # A panel's toggle socket always stays first in its panel
        for kind in ('SOCKET', 'PANEL'):
            managed = [
                (item_key(i), pos) for pos, i in enumerate(items)
                if i.item_type == kind and not getattr(i, 'is_panel_toggle', False)
            ]
            managed = [(key, pos) for key, pos in managed if key is not None]
            for slot, (_, pos) in zip([pos for _, pos in managed], sorted(managed)):
                desired[slot] = items[pos]
        moved = 0
        for target, item in enumerate(desired):
            if item.position != target:
                if not dry_run:
                    tree.interface.move(item, target)
                moved += 1
        for item in desired:
            if item.item_type == 'PANEL':
                moved += sort_level(list(item.interface_items))
        return moved

    top = [i for i in tree.interface.items_tree if is_child_of(i, None)] if container is None \
        else list(container.interface_items)
    return sort_level(top)


def sort_exposed_items(tree, container, consumers):
    """Reorders the exposed sockets and panels inside container (None = top level), recursively, into source order:
    the nodes they feed top-to-bottom, then each node's own input order. Items feeding no node keep their place.
    At top level only sockets named like the input they feed count, so the group's own inputs stay put.
    Returns the number of items moved."""
    strict = container is None
    cache = {}

    def socket_key(item):
        if item.in_out != 'INPUT':
            return None
        keys = [
            get_layout_order(node, sock, cache) for node, sock in consumers.get(item.identifier, ())
            if node.bl_idname != 'NodeGroupOutput' and (not strict or sock.name == item.name)
        ]
        return min(keys) if keys else None

    return sort_interface(tree, container, socket_key)


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
        # Skip Linked wins: linked inputs are only exposed (and their feeders moved out) when it's off
        extract_linked = self.extract_linked and not self.skip_linked
        instances = find_group_instances(tree) if extract_linked else []
        selected_names = {n.name for n in selected_nodes}
        zone_names = get_zone_node_names(tree) if instances else set()
        extracted = {}      # source socket pointer -> (source output socket, interface socket item)
        nodes_to_move = {}  # name -> node: everything upstream of the extracted sources
        passed_reroutes = set()  # reroutes between sources and inputs; removed if left dangling
        consumers = get_input_consumers(tree)  # which exposed socket feeds which node input; kept up to date below

        for node in selected_nodes:
            sub_panel_name = ""
            if self.use_sub_panels:
                if node.label:
                    sub_panel_name = node.label
                elif hasattr(node, 'node_tree') and node.node_tree:
                    sub_panel_name = node.node_tree.name
                else:
                    sub_panel_name = node.name

            target_panel = parent_panel
            if self.use_sub_panels:
                target_panel = find_node_panel(tree, parent_panel, node, consumers) or get_or_create_sub_panel(
                    tree, sub_panel_name, parent_panel, node, consumers, self.index_duplicate_panels
                )

            node_had_exposure = False
            for inp in node.inputs:
                if self.only_exposed and inp.hide:
                    continue
                if hasattr(inp, 'enabled') and not inp.enabled:
                    continue
                # The empty "add item" socket of nodes with item lists (Combine Bundle, Menu Switch, ...):
                # wiring it would make the node grow a new item
                if inp.bl_idname == 'NodeSocketVirtual':
                    continue
                # Already exposed (or fed by one of the group's own inputs): never expose it twice
                if inp.links and resolve_source_socket(inp.links[0])[0].node.bl_idname == 'NodeGroupInput':
                    continue

                if inp.links and self.skip_linked:
                    continue
                # Wiring between the selected nodes is structure, not a parameter: never replace it
                if inp.links and resolve_source_socket(inp.links[0])[0].node.name in selected_names:
                    continue

                source = None
                if instances and inp.links and not inp.is_multi_input:
                    source, reroutes = resolve_source_socket(inp.links[0])
                    upstream = collect_upstream_nodes(source.node)
                    # Fed (further up) by a selected node or a zone, whose input/output pair can't be split:
                    # moving it out would break the graph, so leave it alone
                    if any(name in selected_names or name in zone_names for name in upstream):
                        continue

                socket_type = SOCKET_TYPE_MAP.get(inp.type, 'NodeSocketFloat')
                socket_name = inp.name

                if source:
                    passed_reroutes.update(reroutes)
                is_new_socket = not (source and source.as_pointer() in extracted)
                if is_new_socket:
                    socket_panel = target_panel
                    child_item = find_interface_input(node.node_tree, inp.identifier) if getattr(node, 'node_tree', None) else None
                    siblings, index = get_source_siblings(node, inp)
                    neighbour = find_exposed_neighbour(tree, siblings, index, node, consumers, target_panel)
                    if neighbour is not None:
                        # Join the inputs next to it in the source, wherever (and however nested) they were exposed
                        socket_panel = None if is_root_panel(neighbour.parent) else neighbour.parent
                    else:
                        # Recreate the child group's own panels (outermost first) so its structure carries over
                        for child_panel in (get_panel_path(child_item) if child_item else []):
                            socket_panel = get_or_create_sub_panel(
                                tree, child_panel.name, socket_panel, node, consumers, self.index_duplicate_panels,
                                child_panel,
                            )

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
                    consumers.setdefault(sock_item.identifier, []).append((node, inp))
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
        elif extract_linked and not instances:
            report_type = {'WARNING'}
            moved_info = f" '{tree.name}' has no instances to move linked nodes next to, so they were left inside."

        # Put new and existing exposed items in source order: nodes top-to-bottom, then each node's input order
        reordered = sort_exposed_items(tree, parent_panel, get_input_consumers(tree))
        order_info = f" Reordered {reordered} item(s) to match the source." if reordered else ""

        parent_info = f" inside master panel '{parent_panel.name}'" if parent_panel else ""
        self.report(
            report_type,
            f"Exposed {connected_count} socket(s) from {nodes_processed} node group(s){parent_info}."
            f"{moved_info}{order_info}"
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
                col.label(text=f" • {get_node_title(n)}", icon='DOT')
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
        sub = col.column(align=True)
        sub.active = not settings.skip_linked  # only applies to linked inputs, which Skip Linked leaves alone
        sub.prop(settings, "extract_linked")
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


# ---------------------------------------------------------------------------
# Reordering sockets to follow their links
# ---------------------------------------------------------------------------

class InterfaceSockets:
    """The input or output sockets of a node tree's interface: the outputs of its Group Input nodes or the inputs of
    its Group Output nodes, and the sockets of every group node using it"""
    icon = 'NODETREE'

    def __init__(self, tree, in_out, label, hint=""):
        self.tree = tree
        self.in_out = in_out
        self.label = label
        self.hint = hint
        self.id = ('INTERFACE', tree.as_pointer(), in_out)
        self.editable = tree.library is None and tree.override_library is None
        self.keys = {}   # interface socket identifier -> sort key
        self.nodes = []  # names of the selected nodes showing these sockets

    def sort(self, dry_run=False):
        """Sorts the keyed sockets, each inside its own panel. Returns the number of items out of place."""
        def socket_key(item):
            return self.keys.get(item.identifier) if item.in_out == self.in_out else None
        return sort_interface(self.tree, None, socket_key, dry_run)


class ItemSockets:
    """Sockets made from one of a node's item lists (Combine Bundle, Capture Attribute, zones, ...)"""
    icon = 'NODE'

    def __init__(self, owner, prop, label):
        self.owner = owner  # the node storing the items: the paired output node for a zone input node
        self.prop = prop
        self.label = label
        self.hint = ""
        self.id = ('ITEMS', owner.as_pointer(), prop)
        self.editable = True
        self.keys = {}   # item index -> sort key
        self.nodes = []

    def sort(self, dry_run=False):
        """Sorts the keyed items within the slots they occupy. Returns the number of items moved."""
        items = getattr(self.owner, self.prop)
        desired = list(range(len(items)))
        for slot, (_, index) in zip(sorted(self.keys), sorted((key, i) for i, key in self.keys.items())):
            desired[slot] = index
        current = list(range(len(items)))
        moved = 0
        # Front to back, so every item still to place sits after its target slot
        for target, index in enumerate(desired):
            pos = current.index(index)
            if pos != target:
                if not dry_run:
                    items.move(pos, target)
                current.insert(target, current.pop(pos))
                moved += 1
        return moved


def find_item_run(sockets, names, start):
    """Returns where the sockets of an item list (named names) start among sockets, at or after start, or None.
    The run must end at the "add item" socket or at the last socket, so a fixed socket (Capture Attribute's Geometry,
    ...) is never taken for an item of the same name."""
    count = len(names)
    for k in range(start, len(sockets) - count + 1):
        end = k + count
        if (end == len(sockets) or sockets[end].bl_idname == 'NodeSocketVirtual') \
                and [s.name for s in sockets[k:end]] == names:
            return k
    return None


def get_reorderable_sockets(tree, node, in_out):
    """Returns [(socket list, {socket identifier: handle in that list})] for the in_out sockets of node that can be
    reordered: a Group Input's outputs and a Group Output's inputs (tree's interface), a group node's sockets (its
    group's interface) and sockets made from item lists. Empty when the node's sockets have a fixed order."""
    sockets = list(node.inputs if in_out == 'INPUT' else node.outputs)
    interface = None
    if node.bl_idname == 'NodeGroupInput' and in_out == 'OUTPUT':
        interface = InterfaceSockets(tree, 'INPUT', "Group Input")
    elif node.bl_idname == 'NodeGroupOutput' and in_out == 'INPUT':
        interface = InterfaceSockets(tree, 'OUTPUT', "Group Output")
    elif getattr(node, 'node_tree', None):
        group = node.node_tree
        # Other users of the group get the new order too
        users = group.users - int(group.use_fake_user)
        interface = InterfaceSockets(group, in_out, group.name, f"{users} users" if users > 1 else "")
    if interface:
        return [(interface, {s.identifier: s.identifier for s in sockets if s.bl_idname != 'NodeSocketVirtual'})]

    props = ITEM_COLLECTIONS.get(node.bl_idname, {}).get(in_out, ())
    owner = node.paired_output if hasattr(node, 'paired_output') else node
    found = []
    start = 0
    for prop in props:
        items = getattr(owner, prop, None) if owner else None
        if not items or not hasattr(items, 'move'):
            continue
        k = find_item_run(sockets, [getattr(item, 'name', None) for item in items], start)
        if k is None:
            continue
        label = get_node_title(node)
        if len(props) > 1:
            label += f" ({prop.removesuffix('_items').replace('_', ' ').title()})"
        found.append((ItemSockets(owner, prop, label), {sockets[k + i].identifier: i for i in range(len(items))}))
        start = k + len(items)
    return found


def get_connections(tree, selected):
    """Returns (from node, from socket, to node, to socket) for the links between the selected nodes ({name: node}),
    following reroutes. With a single node selected, all of its links count."""
    links_from = {}
    for link in tree.links:
        links_from.setdefault(link.from_node.name, []).append(link)
    single = len(selected) == 1
    connections = []
    for link in tree.links:
        from_node = link.from_node
        if from_node.bl_idname == 'NodeReroute' or not (single or from_node.name in selected):
            continue
        stack = [link]
        while stack:
            current = stack.pop()
            to_node = current.to_node
            if to_node.bl_idname == 'NodeReroute':
                stack.extend(links_from.get(to_node.name, ()))
            elif to_node.name in selected or (single and from_node.name in selected):
                connections.append((from_node, link.from_socket, to_node, current.to_socket))
    return connections


def find_order_conflict(node_keys):
    """Returns the names of two nodes ({name: {handle: sort key}}) whose links want two shared sockets in opposite
    orders, or None."""
    for a, b in combinations(node_keys, 2):
        keys_a, keys_b = node_keys[a], node_keys[b]
        shared = [handle for handle in keys_a if handle in keys_b]
        for h1, h2 in combinations(shared, 2):
            if (keys_a[h1] < keys_a[h2] and keys_b[h1] > keys_b[h2]) or \
                    (keys_a[h1] > keys_a[h2] and keys_b[h1] < keys_b[h2]):
                return a, b
    return None


def plan_socket_reorder(tree, in_out):
    """Works out how to reorder the in_out sockets of the selected nodes so they follow the sockets at the other end
    of their links: those sockets' nodes top-to-bottom (then left-to-right), then each node's own socket order.
    Links between selected nodes count, or all links of a lone selected node; other sockets keep their place.
    Returns (socket lists to sort with their keys set, in the order to sort them; notes [(icon, text)] on what
    can't be done as asked)."""
    selected = {n.name: n for n in tree.nodes if n.select and n.bl_idname not in LAYOUT_NODE_TYPES}
    if not selected:
        return [], [('INFO', "Select nodes to reorder their sockets")]
    side = "inputs" if in_out == 'INPUT' else "outputs"

    lists = {}        # list id -> socket list
    node_keys = {}    # list id -> {node name: {handle: sort key}}
    reorderable = {}  # node name -> get_reorderable_sockets() result
    fixed = set()     # titles of selected nodes whose linked sockets have a fixed order
    edges = set()     # (from node name, to node name) between selected nodes
    cache = {}
    connections = get_connections(tree, selected)
    for from_node, from_sock, to_node, to_sock in connections:
        if from_node.name in selected and to_node.name in selected:
            edges.add((from_node.name, to_node.name))
        if in_out == 'INPUT':
            node, sock, other, other_sock = to_node, to_sock, from_node, from_sock
        else:
            node, sock, other, other_sock = from_node, from_sock, to_node, to_sock
        if node.name not in selected:
            continue
        if node.name not in reorderable:
            reorderable[node.name] = get_reorderable_sockets(tree, node, in_out)
        for socket_list, handles in reorderable[node.name]:
            handle = handles.get(sock.identifier)
            if handle is not None:
                lists.setdefault(socket_list.id, socket_list)
                keys = node_keys.setdefault(socket_list.id, {}).setdefault(node.name, {})
                key = get_layout_order(other, other_sock, cache)
                keys[handle] = min(keys.get(handle, key), key)
                break
        else:
            fixed.add(get_node_title(node))

    # Inputs follow what feeds them, so upstream nodes are sorted first (downstream first for outputs):
    # sorting zone items moves the sockets on both sides of the zone
    rank = dict.fromkeys(selected, 0)
    for _ in range(len(selected)):
        changed = False
        for a, b in edges:
            if rank[b] <= rank[a]:
                rank[b] = rank[a] + 1
                changed = True
        if not changed:
            break

    notes = []
    result = []
    for list_id, socket_list in lists.items():
        if not socket_list.editable:
            notes.append(('ERROR', f"{socket_list.label} is linked from a library and can't be changed"))
            continue
        per_node = node_keys[list_id]
        # Several selected nodes can show the same sockets (instances of one group, Group Input copies):
        # each socket follows its topmost link
        for keys in per_node.values():
            for handle, key in keys.items():
                socket_list.keys[handle] = min(socket_list.keys.get(handle, key), key)
        conflict = find_order_conflict(per_node)
        if conflict:
            a, b = (selected[name].label or name for name in conflict)
            notes.append((
                'ERROR',
                f"{socket_list.label}: {a} and {b} need different orders, each socket follows its topmost link",
            ))
        socket_list.nodes = list(per_node)
        result.append(socket_list)
    if in_out == 'INPUT':
        result.sort(key=lambda l: min(rank[name] for name in l.nodes))
    else:
        result.sort(key=lambda l: -max(rank[name] for name in l.nodes))

    if not result and not notes:
        if not connections:
            only = get_node_title(next(iter(selected.values())))
            notes.append(('INFO', f"Nothing to reorder: {only} has no links" if len(selected) == 1
                          else "Nothing to reorder: no links between the selected nodes"))
        elif fixed:
            verb = "has" if len(fixed) == 1 else "have"
            notes.append(('INFO', f"Nothing to reorder: {', '.join(sorted(fixed))} {verb} fixed {side}"))
        else:
            notes.append(('INFO', f"Nothing to reorder: no linked {side} on the selected nodes"))
    return result, notes


REORDER_DESCRIPTIONS = {
    'INPUT': (
        "Reorder the input sockets of the selected nodes to follow the outputs linked to them: those nodes "
        "top-to-bottom, then each node's own socket order. Works on Group Output, node groups (for all users of the "
        "group) and item-based nodes (bundles, zones, Capture Attribute, ...). Links between selected nodes count, "
        "or all links of a single selected node"
    ),
    'OUTPUT': (
        "Reorder the output sockets of the selected nodes to follow the inputs they're linked to: those nodes "
        "top-to-bottom, then each node's own socket order. Works on Group Input, node groups (for all users of the "
        "group) and item-based nodes (bundles, zones, Capture Attribute, ...). Links between selected nodes count, "
        "or all links of a single selected node"
    ),
}


class NODE_OT_grip_reorder_sockets(bpy.types.Operator):
    """Reorder the sockets of the selected nodes to follow the sockets they're linked to"""
    bl_idname = "node.grip_reorder_sockets"
    bl_label = "Reorder Sockets"
    bl_options = {'REGISTER', 'UNDO'}

    in_out: EnumProperty(
        name="Sockets",
        items=(
            ('INPUT', "Inputs", "Reorder input sockets to follow the outputs linked to them"),
            ('OUTPUT', "Outputs", "Reorder output sockets to follow the inputs they're linked to"),
        ),
        default='INPUT',
    )

    @classmethod
    def description(cls, context, properties):
        return REORDER_DESCRIPTIONS[properties.in_out]

    @classmethod
    def poll(cls, context):
        tree = get_active_node_tree(context)
        return tree is not None and any(n.select for n in tree.nodes)

    def execute(self, context):
        tree = get_active_node_tree(context)
        side = "inputs" if self.in_out == 'INPUT' else "outputs"
        socket_lists, notes = plan_socket_reorder(tree, self.in_out)

        moved = 0
        changed = []
        for list_id in [l.id for l in socket_lists]:
            # Planned again before each list: sorting zone items moves sockets the next list may follow
            socket_list = next((l for l in plan_socket_reorder(tree, self.in_out)[0] if l.id == list_id), None)
            count = socket_list.sort() if socket_list else 0
            if count:
                moved += count
                changed.append(socket_list.label)

        warnings = [text for icon, text in notes if icon == 'ERROR']
        if moved:
            message = [f"Reordered the {side} of {', '.join(changed)} ({moved} moved)."] + warnings
        elif socket_lists:
            message = [f"The {side} are already in order."] + warnings
        else:
            message = [text for _, text in notes]
        self.report({'WARNING'} if warnings else {'INFO'}, " ".join(message))
        return {'FINISHED'} if moved else {'CANCELLED'}


def draw_wrapped(layout, context, text, icon='NONE'):
    """Draws text as labels wrapped to the region's width, with the icon on the first line."""
    chars = int(context.region.width / (7 * (context.preferences.system.ui_scale or 1))) - 4
    col = layout.column(align=True)
    for i, line in enumerate(textwrap.wrap(text, max(chars, 16))):
        col.label(text=line, icon=icon if i == 0 else ('NONE' if icon == 'NONE' else 'BLANK1'))


class NODE_PT_grip_reorder_panel(bpy.types.Panel):
    """Reorder sockets of the selected nodes to follow their links (N > Grip)"""
    bl_label = "Reorder Sockets"
    bl_idname = "NODE_PT_grip_reorder_panel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Grip"

    def draw(self, context):
        layout = self.layout
        tree = get_active_node_tree(context)

        if not tree:
            layout.label(text="No active node tree open", icon='INFO')
            return

        buttons = (('INPUT', "Reorder Inputs", 'IMPORT'), ('OUTPUT', "Reorder Outputs", 'EXPORT'))
        if not any(n.select and n.bl_idname not in LAYOUT_NODE_TYPES for n in tree.nodes):
            row = layout.row(align=True)
            row.scale_y = 1.4
            row.enabled = False
            for in_out, text, icon in buttons:
                row.operator(NODE_OT_grip_reorder_sockets.bl_idname, text=text, icon=icon).in_out = in_out
            layout.label(text="Select nodes to reorder their sockets", icon='INFO')
            return

        for in_out, text, icon in buttons:
            socket_lists, notes = plan_socket_reorder(tree, in_out)
            pending = [socket_list.sort(dry_run=True) for socket_list in socket_lists]

            box = layout.box()
            row = box.row()
            row.scale_y = 1.4
            row.enabled = any(pending)
            row.operator(NODE_OT_grip_reorder_sockets.bl_idname, text=text, icon=icon).in_out = in_out

            col = box.column(align=True)
            for socket_list, out_of_place in zip(socket_lists, pending):
                row = col.row()
                label = f"{socket_list.label} ({socket_list.hint})" if socket_list.hint else socket_list.label
                row.label(text=label, icon=socket_list.icon)
                sub = row.row()
                sub.alignment = 'RIGHT'
                if out_of_place:
                    count = len(socket_list.keys)
                    sub.label(text=f"{count} socket{'s' if count != 1 else ''}")
                else:
                    sub.label(text="In order", icon='CHECKMARK')
            for note_icon, note in notes:
                draw_wrapped(col, context, note, note_icon)


# ---------------------------------------------------------------------------
# Toolbox
# ---------------------------------------------------------------------------

def get_selected_sizable_nodes(tree):
    """Selected nodes that have a settable width (everything but frames and reroutes)."""
    if not tree or not hasattr(tree, 'nodes'):
        return []
    return [n for n in tree.nodes if n.select and n.bl_idname not in WIDTHLESS_NODE_TYPES]


class NODE_OT_grip_set_node_width(bpy.types.Operator):
    """Set the width of all selected nodes"""
    bl_idname = "node.grip_set_node_width"
    bl_label = "Set Node Width"
    bl_options = {'REGISTER', 'UNDO'}

    width: FloatProperty(
        name="Width",
        default=140.0,
        min=10.0,
        soft_max=700.0,
        step=1000,
        precision=0,
        subtype='PIXEL',
    )

    @classmethod
    def poll(cls, context):
        return bool(get_selected_sizable_nodes(get_active_node_tree(context)))

    def execute(self, context):
        nodes = get_selected_sizable_nodes(get_active_node_tree(context))
        if not nodes:
            self.report({'WARNING'}, "No nodes selected")
            return {'CANCELLED'}

        for node in nodes:
            node.width = self.width

        # Blender clamps the width to each node type's own min/max
        clamped = sum(1 for n in nodes if abs(n.width - self.width) > 0.5)
        clamped_info = f" ({clamped} clamped to their type's limits)" if clamped else ""
        self.report({'INFO'}, f"Set width of {len(nodes)} node(s) to {self.width:g}{clamped_info}")
        return {'FINISHED'}


class NODE_PT_grip_toolbox_panel(bpy.types.Panel):
    """Personal node tools in the Node Editor Sidebar (N > Grip)"""
    bl_label = "Toolbox"
    bl_idname = "NODE_PT_grip_toolbox_panel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Grip"

    def draw(self, context):
        layout = self.layout
        tree = get_active_node_tree(context)

        if not tree:
            layout.label(text="No active node tree open", icon='INFO')
            return

        settings = context.scene.grip_toolbox_settings

        # Node width
        width_box = layout.box()
        width_box.label(text="Node Width:", icon='ARROW_LEFTRIGHT')
        row = width_box.row(align=True)
        row.prop(settings, "node_width", text="")
        op = row.operator(NODE_OT_grip_set_node_width.bl_idname, text="Apply to Selected")
        op.width = settings.node_width


classes = (
    GripExposeSettings,
    GripToolboxSettings,
    NODE_OT_expose_child_inputs,
    NODE_OT_grip_reorder_sockets,
    NODE_OT_grip_set_node_width,
    NODE_PT_grip_expose_panel,
    NODE_PT_grip_reorder_panel,
    NODE_PT_grip_toolbox_panel,
)


def register():
    unregister()
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            pass
    bpy.types.Scene.grip_expose_settings = PointerProperty(type=GripExposeSettings)
    bpy.types.Scene.grip_toolbox_settings = PointerProperty(type=GripToolboxSettings)


def unregister():
    for prop in ('grip_expose_settings', 'grip_toolbox_settings'):
        if hasattr(bpy.types.Scene, prop):
            try:
                delattr(bpy.types.Scene, prop)
            except Exception:
                pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


if __name__ == "__main__":
    register()
