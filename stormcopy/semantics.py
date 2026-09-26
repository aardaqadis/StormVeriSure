"""Conservative logic and microprocessor fingerprints from Stormworks XML.

Vehicle logic links are described by type, relative endpoint displacement, and
the component types at their endpoints. Microprocessor hashes describe complete
internal component graphs or substantial Lua scripts. Names, layout positions,
and regenerated component IDs are omitted.
"""

from collections import Counter, defaultdict
from hashlib import sha256
import json
import re


_DOMAIN = b"stormcopy:semantics:v1\0"
_CONNECTION = re.compile(r"in\d+\Z")
_IGNORED_ATTRS = {"id", "unique_id", "component_id", "id_counter", "id_counter_node",
                  "name", "label", "description", "desc", "custom_name",
                  "username", "steam_id", "hide_in_inventory", "transform_index",
                  "color", "colour", "paint", "bc", "ac", "sc"}


def _hash(kind, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return sha256(_DOMAIN + kind.encode("ascii") + b"\0" + payload).hexdigest()[:24]


def _xyz(node):
    if node is None:
        return None
    try:
        return tuple(int(node.get(axis, "")) for axis in "xyz")
    except ValueError:
        return None


def _component_position(component):
    return _xyz(component.find(".//vp"))


def _canonical_node(node, depth=0):
    """Normalize meaningful object properties while preserving ordered arrays."""
    if depth > 64:
        raise ValueError("Microprocessor XML nesting exceeds 64 levels")
    attrs = []
    for key, value in node.attrib.items():
        if key in _IGNORED_ATTRS or key.startswith("sym"):
            continue
        if key == "script":
            value = _hash("lua", value.replace("\r\n", "\n").strip())
        attrs.append((key, value))
    attrs.sort()
    if node.tag in ("script", "lua"):
        content = _hash("lua", (node.text or "").replace("\r\n", "\n").strip())
    else:
        content = " ".join((node.text or "").split())

    children = defaultdict(list)
    for child in node:
        if child.tag in ("pos", "position") or child.tag.startswith("out"):
            continue
        if _CONNECTION.fullmatch(child.tag):
            continue
        children[child.tag].append(_canonical_node(child, depth + 1))
    # Different field tags are unordered; repeated tags retain their sequence.
    ordered_children = [(tag, values) for tag, values in sorted(children.items())]
    return (node.tag, attrs, content, ordered_children)


def _microprocessor_features(definition):
    components = list(definition.iter("c"))
    base = []
    objects = []
    id_to_index = {}
    scripts = []
    for component in components:
        obj = component.find("object")
        objects.append(obj)
        descriptor = (component.get("type", ""),
                      _canonical_node(obj) if obj is not None else None)
        base.append(_hash("micro-node", descriptor))
        if obj is not None:
            component_id = obj.get("id")
            if component_id is not None:
                id_to_index[component_id] = len(base) - 1
            script = obj.get("script", "").replace("\r\n", "\n").strip()
            if len(script) >= 64:
                scripts.append(_hash("micro-script", script))

    incoming = defaultdict(list)
    edge_count = 0
    for target, obj in enumerate(objects):
        if obj is None:
            continue
        for input_node in obj:
            if not _CONNECTION.fullmatch(input_node.tag):
                continue
            source = id_to_index.get(input_node.get("component_id"))
            if source is None:
                continue
            incoming[target].append((input_node.tag, input_node.get("node_index", ""),
                                     source))
            edge_count += 1

    # Two refinement passes include both direct wiring and a second hop while
    # remaining invariant to XML component order and regenerated IDs.
    labels = base
    for _ in range(2):
        labels = [_hash("micro-refine", (base[i], sorted(
            (port, slot, labels[source]) for port, slot, source in incoming[i])))
                  for i in range(len(base))]

    ports = []
    for node in definition.findall("./nodes/n"):
        port = node.find("node")
        if port is not None:
            bound = id_to_index.get(node.get("component_id"))
            ports.append((node.get("built_slot_index", ""), port.get("mode", ""),
                          port.get("type", ""), port.get("flags", ""),
                          labels[bound] if bound is not None else ""))

    fingerprints = Counter(scripts)
    if len(components) >= 3 and edge_count >= 2:
        graph = (sorted(labels), sorted(ports), edge_count)
        fingerprints[_hash("micro-graph", graph)] += 1
    return fingerprints


def semantic_fingerprints(root):
    """Return logic, microprocessor, and component-type evidence from a vehicle.

    ``root`` is a parsed ``xml.etree.ElementTree.Element``. The returned keys
    are ``logic_features``, ``logic_samples``, ``micro_features``,
    ``micro_samples``, and ``component_types``. Samples are vehicle coordinates
    where available; standalone definitions use ``None``.
    """
    logic_features = Counter()
    logic_samples = {}
    micro_features = Counter()
    micro_samples = {}
    component_types = Counter()
    position_types = defaultdict(list)
    vehicle_components = []
    for component in root.iter("c"):
        kind = component.get("d")
        if kind is None:
            continue  # A microprocessor's internal <c type="..."> node.
        xyz = _component_position(component)
        if xyz is None:
            continue
        component_types[kind] += 1
        position_types[xyz].append(kind)
        vehicle_components.append((component, xyz))

    for link in root.iter("logic_node_link"):
        start = _xyz(link.find("voxel_pos_0"))
        end = _xyz(link.find("voxel_pos_1"))
        if start is None or end is None:
            continue
        delta = tuple(b - a for a, b in zip(start, end))
        endpoints = (tuple(sorted(position_types.get(start, ()))),
                     tuple(sorted(position_types.get(end, ()))))
        value = _hash("logic-link", (link.get("type", ""), delta, endpoints))
        logic_features[value] += 1
        logic_samples.setdefault(value, start)

    seen = set()
    for component, xyz in vehicle_components:
        for definition in component.iter("microprocessor_definition"):
            seen.add(id(definition))
            for value, count in _microprocessor_features(definition).items():
                micro_features[value] += count
                micro_samples.setdefault(value, xyz)
    for definition in root.iter("microprocessor_definition"):
        if id(definition) in seen:
            continue
        for value, count in _microprocessor_features(definition).items():
            micro_features[value] += count
            micro_samples.setdefault(value, None)

    return {"logic_features": logic_features, "logic_samples": logic_samples,
            "micro_features": micro_features, "micro_samples": micro_samples,
            "component_types": component_types}
