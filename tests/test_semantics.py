"""Behavior tests for Stormworks logic and microprocessor fingerprints."""

import unittest
import xml.etree.ElementTree as ET

from stormcopy.semantics import semantic_fingerprints


def vehicle(script="x = input.getNumber(1)\noutput.setNumber(1, x + 1)",
            ids=(10, 20, 30), name="Control", shift=(0, 0, 0),
            reverse=False, connection=True):
    x, y, z = shift
    links = (f'<logic_node_link type="2"><voxel_pos_0 x="{x}" y="{y}" z="{z}"/>'
             f'<voxel_pos_1 x="{x + 4}" y="{y}" z="{z}"/></logic_node_link>')
    nodes = [
        f'<c type="8"><object id="{ids[0]}" n="7"><pos x="0" y="0"/>'
        '<out1/></object></c>',
        f'<c type="56"><object id="{ids[1]}" script="{script}"><pos x="1" y="0"/>'
        + (f'<in1 component_id="{ids[0]}" node_index="0"/>' if connection else '')
        + '<out1/></object></c>',
        f'<c type="6"><object id="{ids[2]}"><pos x="2" y="0"/>'
        f'<in1 component_id="{ids[1]}" node_index="0"/><out1/></object></c>',
    ]
    if reverse:
        nodes.reverse()
    xml = (f'<vehicle><components><c d="microprocessor"><o><vp x="{x}" y="{y}" z="{z}"/>'
           f'<microprocessor_definition name="{name}" id_counter="99" width="2">'
           f'<nodes><n id="7" component_id="{ids[0]}" built_slot_index="0">'
           '<node mode="1" type="1" label="display name"/></n></nodes>'
           f'<group><components>{"".join(nodes)}</components></group>'
           '</microprocessor_definition></o></c>'
           f'<c d="engine"><o><vp x="{x + 4}" y="{y}" z="{z}"/></o></c>'
           f'</components><logic_node_links>{links}</logic_node_links></vehicle>')
    return ET.fromstring(xml)


class SemanticFingerprintTests(unittest.TestCase):
    def test_translation_and_cosmetic_edits_keep_evidence(self):
        first = semantic_fingerprints(vehicle())
        moved = semantic_fingerprints(vehicle(ids=(100, 200, 300), name="Renamed",
                                              shift=(17, -3, 9), reverse=True))
        self.assertTrue(first["logic_features"])
        self.assertTrue(first["micro_features"])
        self.assertEqual(first["logic_features"], moved["logic_features"])
        self.assertEqual(first["micro_features"], moved["micro_features"])
        self.assertEqual(first["component_types"], moved["component_types"])
        self.assertEqual(first["component_types"], {"microprocessor": 1, "engine": 1})
        old_position = next(iter(first["micro_samples"].values()))
        moved_position = next(iter(moved["micro_samples"].values()))
        self.assertEqual(tuple(a + b for a, b in zip(old_position, (17, -3, 9))),
                         moved_position)

    def test_script_and_wiring_changes_change_micro_evidence(self):
        original = semantic_fingerprints(vehicle())
        edited_script = semantic_fingerprints(vehicle(script=(
            "x = input.getNumber(1)\noutput.setNumber(1, x + 2)")))
        rewired = semantic_fingerprints(vehicle(connection=False))
        self.assertNotEqual(original["micro_features"], edited_script["micro_features"])
        self.assertNotEqual(original["micro_features"], rewired["micro_features"])

    def test_link_delta_and_type_are_meaningful(self):
        original = vehicle()
        changed = vehicle()
        link = next(changed.iter("logic_node_link"))
        link.set("type", "3")
        self.assertNotEqual(semantic_fingerprints(original)["logic_features"],
                            semantic_fingerprints(changed)["logic_features"])
        changed = vehicle()
        next(changed.iter("voxel_pos_1")).set("x", "9")
        self.assertNotEqual(semantic_fingerprints(original)["logic_features"],
                            semantic_fingerprints(changed)["logic_features"])

    def test_swapping_external_microprocessor_port_binding_changes_graph(self):
        original = vehicle()
        changed = vehicle()
        next(changed.iter("n")).set("component_id", "20")
        self.assertNotEqual(semantic_fingerprints(original)["micro_features"],
                            semantic_fingerprints(changed)["micro_features"])

    def test_tiny_generic_microprocessor_is_not_evidence(self):
        root = ET.fromstring('<vehicle><c d="microprocessor"><o><vp x="0" y="0" z="0"/>'
                           '<microprocessor_definition name="Generic"><group><components>'
                           '<c type="1"><object id="7"/></c>'
                           '</components></group></microprocessor_definition></o></c></vehicle>')
        result = semantic_fingerprints(root)
        self.assertFalse(result["micro_features"])
        self.assertEqual(result["component_types"], {"microprocessor": 1})

    def test_standalone_definition_has_no_position(self):
        root = ET.fromstring('<vehicle><microprocessor_definition><group><components>'
                           '<c type="56"><object id="1" script="'
                           + 'x = input.getNumber(1) output.setNumber(1, x + 100)' * 2
                           + '"/></c></components></group></microprocessor_definition></vehicle>')
        result = semantic_fingerprints(root)
        self.assertTrue(result["micro_features"])
        self.assertEqual(set(result["micro_samples"].values()), {None})

    def test_invalid_logic_endpoint_is_skipped(self):
        root = ET.fromstring('<vehicle><logic_node_link type="1"><voxel_pos_0 x="a" '
                           'y="0" z="0"/><voxel_pos_1 x="1" y="0" z="0"/>'
                           '</logic_node_link></vehicle>')
        self.assertFalse(semantic_fingerprints(root)["logic_features"])


if __name__ == "__main__":
    unittest.main()
