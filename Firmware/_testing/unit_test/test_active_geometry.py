"""
    Unit tests for the active array geometry (Firmware/_active/active_geometry.py).

    Hardware-free; configs from active_config.default_config; temp dirs for files.

    Project: HeIMDALL DAQ Firmware
    License: GNU GPL V3
"""
import json
import logging
import math
import os
import sys
import tempfile
import unittest
from os.path import dirname, join, realpath

import numpy as np

current_path = dirname(realpath(__file__))
root_path = dirname(dirname(current_path))
sys.path.insert(0, join(root_path, "_active"))
sys.path.insert(0, join(root_path, "_daq_core"))

import active_config as C  # noqa: E402
import active_geometry as G  # noqa: E402
from active_common import C0, ChannelRef, GeometryError  # noqa: E402

logging.getLogger("antenna_profile").setLevel(logging.ERROR)
logging.getLogger("heimdall.active.geometry").setLevel(logging.ERROR)

F = 1e9
LAM = C0 / F


def codes(issues, level=None):
    return [i.code for i in issues if level is None or i.level == level]


def two_dev_cfg(**kw):
    return C.default_config(devices={"a": {"family": "sim", "rx_channels": "0..3", "tx_channels": "0..1"},
                                     "b": {"family": "sim", "rx_channels": "0..2"}}, **kw)


def base_doc(**kw):
    d = {"schema": G.GEOMETRY_SCHEMA, "name": "t", "units": "m", "frame": "body",
         "elements": [{"id": 0, "pos": [0, 0, 0], "rx": {"device": "a", "channel": 0}},
                      {"id": 1, "pos": [0.15, 0, 0], "rx": {"device": "a", "channel": 1}}]}
    d.update(kw)
    return d


class TestFramesAndDirections(unittest.TestCase):
    def test_orientation_matrix(self):
        R = G.Orientation(heading_deg=90).matrix()
        np.testing.assert_allclose(R @ [0, 1, 0], [1, 0, 0], atol=1e-12)    # forward -> East
        R = G.Orientation(pitch_deg=90).matrix()
        np.testing.assert_allclose(R @ [0, 1, 0], [0, 0, 1], atol=1e-12)    # nose up -> Up
        R = G.Orientation(roll_deg=90).matrix()
        np.testing.assert_allclose(R @ [1, 0, 0], [0, 0, -1], atol=1e-12)   # right side down
        np.testing.assert_allclose(G.Orientation().matrix(), np.eye(3))
        R = G.Orientation(37, -12, 5).matrix()
        np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(np.linalg.det(R), 1.0, places=12)

    def test_with_orientation_rotates_positions(self):
        g = G.preset_ula(3, 0.1)                                            # along body +x
        np.testing.assert_allclose(g.positions()[2], [0.2, 0, 0], atol=1e-12)
        g90 = g.with_orientation(G.Orientation(heading_deg=90))
        np.testing.assert_allclose(g90.positions()[2], [0, -0.2, 0], atol=1e-12)
        np.testing.assert_allclose(g90.boresight_enu(), [1, 0, 0], atol=1e-12)

    def test_direction_vector_and_inverse(self):
        np.testing.assert_allclose(G.direction_vector(90, 0), [1, 0, 0], atol=1e-12)
        np.testing.assert_allclose(G.direction_vector(0, 0), [0, 1, 0], atol=1e-12)
        np.testing.assert_allclose(G.direction_vector(0, 90), [0, 0, 1], atol=1e-12)
        rng = np.random.default_rng(5)
        az, el = rng.uniform(0, 360, 50), rng.uniform(-89, 89, 50)
        U = G.direction_vector(az, el)
        self.assertEqual(U.shape, (50, 3))
        a2, e2 = G.direction_angles(U)
        np.testing.assert_allclose(a2, az, atol=1e-9)
        np.testing.assert_allclose(e2, el, atol=1e-9)
        self.assertEqual(G.direction_angles([1, 0, 0]), (90.0, 0.0))
        self.assertEqual(G.direction_vector(np.zeros((2, 3)), 0).shape, (2, 3, 3))
        self.assertAlmostEqual(G.compass_to_math_deg(0), 90.0)
        self.assertAlmostEqual(G.compass_to_math_deg(90), 0.0)
        self.assertAlmostEqual(G.compass_to_math_deg(180), 270.0)

    def test_lla_to_enu_known_points(self):
        e = G.lla_to_enu(0.0, 0.001, 0.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(e[0], 6378137.0 * math.radians(0.001), delta=1e-3)   # 111.3195 m East
        self.assertAlmostEqual(e[1], 0.0, delta=1e-6)
        self.assertAlmostEqual(e[2], -e[0] ** 2 / (2 * 6378137.0), delta=1e-5)      # curvature drop
        n = G.lla_to_enu(0.001, 0.0, 0.0, 0.0, 0.0, 0.0)
        self.assertAlmostEqual(n[1], 110.574, delta=0.01)                           # meridian arc
        u = G.lla_to_enu(47.5, 19.0, 112.0, 47.5, 19.0, 110.0)
        np.testing.assert_allclose(u, [0, 0, 2.0], atol=1e-6)
        pts = G.lla_to_enu([47.5, 47.5001], [19.0, 19.0], [0, 0], 47.5, 19.0, 0)
        self.assertEqual(pts.shape, (2, 3))


class TestPresets(unittest.TestCase):
    def test_uca_radius(self):
        g = G.preset_uca(5, arc_spacing_m=LAM / 2)
        P = g.positions()
        R = np.linalg.norm(P - P.mean(axis=0), axis=1)
        np.testing.assert_allclose(R, LAM / (4 * math.sin(math.pi / 5)), rtol=1e-12)
        self.assertAlmostEqual(R[0] / LAM, 0.4253, places=4)
        self.assertEqual(G.detect_lattice(P)["kind"], "uca")
        self.assertAlmostEqual(g.min_spacing_m(), LAM / 2, places=12)
        g2 = G.preset_uca(4, radius_m=0.3, gamma0_deg=45)
        np.testing.assert_allclose(g2.elements[0].pos_body_m, [0.3 * math.sin(math.radians(45)),
                                                               0.3 * math.cos(math.radians(45)), 0], atol=1e-12)
        with self.assertRaises(GeometryError):
            G.preset_uca(5)

    def test_ula_ura_tri(self):
        g = G.preset_ula(4, 0.1, axis_az_deg=0)
        np.testing.assert_allclose(g.positions()[3], [0, 0.3, 0], atol=1e-12)
        self.assertEqual(g.elements[2].rx, ChannelRef("sim0", 2))
        self.assertEqual(g.elements[2].tx, ChannelRef("sim0", 2))
        self.assertIsNone(G.preset_ula(4, 0.1, map_tx=False).elements[0].tx)
        u = G.preset_ura(3, 2, 0.1, 0.2)
        np.testing.assert_allclose(u.positions()[5], [0.2, 0, 0.2], atol=1e-12)       # vertical panel
        uh = G.preset_ura(3, 2, 0.1, 0.2, plane="horizontal")
        np.testing.assert_allclose(uh.positions()[5], [0.2, 0.2, 0], atol=1e-12)
        t = G.preset_tri(3, 2, 0.1)
        np.testing.assert_allclose(t.positions()[3], [0.05, 0, 0.1 * math.sqrt(3) / 2], atol=1e-12)
        with self.assertRaises(GeometryError):
            G.preset_ula(1, 0.1)
        with self.assertRaises(GeometryError):
            G.preset_ula(4, 0.0)

    def test_rank_and_ambiguity(self):
        self.assertEqual(G.preset_ula(4, 0.1).rank(), 1)
        self.assertEqual(G.preset_ula(4, 0.1).ambiguity(), "cone")
        uca = G.preset_uca(5, radius_m=0.2)
        self.assertEqual(uca.rank(), 2)
        self.assertEqual(uca.ambiguity(), "mirror_elevation")
        self.assertEqual(uca.plane_kind(), "horizontal")
        ura = G.preset_ura(2, 2, 0.1, 0.1)                                  # vertical panel
        self.assertEqual(ura.ambiguity(), "mirror_elevation")              # frozen label (spec 4.4 / 10.6)
        self.assertEqual(ura.plane_kind(), "vertical")
        self.assertEqual(ura.with_orientation(G.Orientation(pitch_deg=40)).plane_kind(), "tilted")
        self.assertIsNone(G.preset_ula(4, 0.1).plane_kind())
        for g in (uca, ura, G.preset_tri(2, 2, 0.1), G.preset_ula(4, 0.1)):
            self.assertIn(g.ambiguity(), G.AMBIGUITY_LABELS)
        # the +el / -el warning applies to horizontal planes only
        lam = 3 * 0.1
        self.assertFalse(any("indistinguishable" in i.message and i.level == "warning"
                             for i in G.validate_geometry(ura, C0 / lam, C0 / lam, 30, look=(0, 10))))
        self.assertTrue(any("+el and -el" in i.message
                            for i in G.validate_geometry(uca, C0 / lam, C0 / lam, 30, look=(0, 10))))
        tet = G.ArrayGeometry([G.Element(0, (0, 0, 0)), G.Element(1, (0.2, 0, 0)),
                               G.Element(2, (0, 0.2, 0)), G.Element(3, (0, 0, 0.2))])
        self.assertEqual(tet.rank("all"), 3)
        self.assertEqual(tet.ambiguity("all"), "none")
        self.assertEqual(tet.rank("rx"), 0)                                 # no RX-mapped elements
        self.assertEqual(tet.ambiguity("rx"), "degenerate")
        # millimetre survey scatter keeps a nominally planar array planar
        P = [(0, 0, 0), (0.3, 0, 0.001), (0, 0.3, -0.0008), (0.3, 0.3, 0.0005)]
        g = G.ArrayGeometry([G.Element(i, p) for i, p in enumerate(P)])
        self.assertEqual(g.rank("all"), 2)

    def test_phase_reference_and_sizes(self):
        g = G.preset_ula(3, 0.1)
        np.testing.assert_allclose(g.positions()[0], 0, atol=1e-15)
        gc = G.ArrayGeometry(g.elements, phase_reference="centroid")
        np.testing.assert_allclose(gc.positions().mean(axis=0), 0, atol=1e-15)
        self.assertAlmostEqual(g.max_baseline_m(), 0.2)
        self.assertAlmostEqual(g.min_spacing_m(), 0.1)
        g2 = G.ArrayGeometry([G.Element(0, (0, 0, 0)), G.Element(1, (1.0, 0, 0))])
        self.assertAlmostEqual(g2.fraunhofer_m(2.4e9, "all"), 16.0, delta=0.05)     # 2 D^2 / lambda
        self.assertAlmostEqual(g2.aperture_m("all"), 1.0)

    def test_element_sets_and_channel_order(self):
        els = [G.Element(0, (0, 0, 0), rx=ChannelRef("b", 0)),
               G.Element(1, (0.1, 0, 0), rx=ChannelRef("a", 1), tx=ChannelRef("a", 0)),
               G.Element(2, (0.2, 0, 0), rx=ChannelRef("a", 0), enabled=False),
               G.Element(3, (0.3, 0, 0), tx=ChannelRef("a", 1))]
        g = G.ArrayGeometry(els, rx_order=[ChannelRef("a", 0), ChannelRef("a", 1), ChannelRef("b", 0)],
                            tx_order=[ChannelRef("a", 0), ChannelRef("a", 1)])
        self.assertEqual([e.id for e in g.elements_for("rx")], [1, 0])        # global order, disabled out
        self.assertEqual([e.id for e in g.elements_for("tx")], [1, 3])
        self.assertEqual([e.id for e in g.elements_for("all")], [0, 1, 3])
        np.testing.assert_allclose(g.positions("rx")[0], [0.1, 0, 0])
        refs = [ChannelRef("a", 0), ChannelRef("a", 1), ChannelRef("b", 0)]
        self.assertEqual(g.channel_indices("rx", refs), [1, 2])
        with self.assertRaises(GeometryError):
            g.channel_indices("rx", refs[:2])
        with self.assertRaises(ValueError):
            g.positions("both")

    def test_construction_errors(self):
        e0 = G.Element(0, (0, 0, 0), rx=ChannelRef("a", 0))
        with self.assertRaises(GeometryError) as cm:
            G.ArrayGeometry([e0, G.Element(0, (0.1, 0, 0))])
        self.assertEqual(cm.exception.code, "GEOMETRY")
        with self.assertRaises(GeometryError) as cm:
            G.ArrayGeometry([e0, G.Element(1, (0.1, 0, 0), rx=ChannelRef("a", 0))])
        self.assertEqual(cm.exception.code, "GEOM_MAP")
        with self.assertRaises(GeometryError):
            G.ArrayGeometry([e0])
        with self.assertRaises(GeometryError):
            G.ArrayGeometry([e0, G.Element(1, (0, 0, 0.00005))])
        with self.assertRaises(GeometryError):
            G.ArrayGeometry([e0, G.Element(1, (0.1, 0, 0), enabled=False)])
        with self.assertRaises(GeometryError):
            G.Element(0, (0, 0))
        with self.assertRaises(GeometryError):
            G.ElementPattern(kind="horn")
        with self.assertRaises(GeometryError):
            G.ElementPattern(kind="table", table=((0, 0, 1, 0), (10, 0, 1, 0), (0, 10, 1, 0)))  # not a grid


class TestLattice(unittest.TestCase):
    def test_detect_lattice(self):
        self.assertEqual(G.detect_lattice(G.preset_ula(6, 0.1).positions())["kind"], "ula")
        lat = G.detect_lattice(G.preset_ura(3, 4, 0.1, 0.12).positions())
        self.assertEqual(lat["kind"], "ura")
        self.assertTrue(lat["complete"])
        self.assertEqual(sorted([round(np.linalg.norm(lat["d1"]), 6), round(np.linalg.norm(lat["d2"]), 6)]),
                         [0.1, 0.12])
        lat = G.detect_lattice(G.preset_tri(4, 3, 0.1).positions())
        self.assertEqual(lat["kind"], "triangular")
        self.assertAlmostEqual(lat["spacing"], 0.1, places=9)
        lat = G.detect_lattice(G.preset_uca(7, radius_m=0.3).positions())
        self.assertEqual(lat["kind"], "uca")
        self.assertAlmostEqual(lat["radius"], 0.3, places=9)
        rng = np.random.default_rng(3)
        P = rng.uniform(-1, 1, (7, 3)) * [1, 1, 0]
        self.assertEqual(G.detect_lattice(P)["kind"], "irregular")
        # a thinned ULA is still periodic but not complete
        thin = np.array([[0, 0, 0], [0.1, 0, 0], [0.3, 0, 0], [0.6, 0, 0]])
        lat = G.detect_lattice(thin)
        self.assertEqual(lat["kind"], "ula")
        self.assertFalse(lat["complete"])
        # 1 % position noise is tolerated
        noisy = G.preset_ula(6, 0.1).positions() + [[0.0005, 0, 0]] * np.array([[0], [1], [-1], [0], [1], [0]])
        self.assertEqual(G.detect_lattice(noisy)["kind"], "ula")

    def test_reciprocal_vectors(self):
        g = G.reciprocal_vectors([0.1, 0, 0], [0.05, 0, 0.08])
        D = np.array([[0.1, 0, 0], [0.05, 0, 0.08]])
        np.testing.assert_allclose(g @ D.T, np.eye(2), atol=1e-12)
        self.assertAlmostEqual(G.min_reciprocal_norm([0.2, 0, 0]), 5.0)
        tri = G.min_reciprocal_norm([1, 0, 0], [0.5, 0, math.sqrt(3) / 2])
        self.assertAlmostEqual(tri, 2 / math.sqrt(3), places=9)


class TestValidateGeometry(unittest.TestCase):
    def test_ula_grating_rule(self):
        g = G.preset_ula(8, 0.5 * LAM)
        self.assertNotIn("GRATING_LOBE", codes(G.validate_geometry(g, F, F, 90)))
        g = G.preset_ula(8, 0.6 * LAM)
        iss = G.validate_geometry(g, F, F, 60)
        self.assertIn("GRATING_LOBE", codes(iss, "error"))
        self.assertNotIn("GRATING_LOBE", codes(G.validate_geometry(g, F, F, 40)))
        free = G.grating_free_scan_deg(g, F)
        self.assertAlmostEqual(free, math.degrees(math.asin(1 / 0.6 - 1)), places=6)   # 41.8 deg
        self.assertIn("41.8", [i for i in iss if i.code == "GRATING_LOBE"][0].message)
        # the check is applied at f_max
        g = G.preset_ula(8, 0.5 * LAM)
        self.assertIn("GRATING_LOBE", codes(G.validate_geometry(g, 0.9 * F, 1.3 * F, 60)))

    def test_ura_and_tri_rules(self):
        self.assertNotIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_tri(4, 4, 0.577 * LAM), F, F, 90)))
        self.assertIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_tri(4, 4, 0.62 * LAM), F, F, 90)))
        self.assertNotIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_ura(4, 4, 0.5 * LAM, 0.5 * LAM), F, F, 90)))
        self.assertIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_ura(4, 4, 0.5 * LAM, 0.7 * LAM), F, F, 60)))
        # a horizontal panel sees its own plane at the boresight (horizon): the rule is hemispheric
        gh = G.preset_ura(4, 4, 0.55 * LAM, 0.55 * LAM, plane="horizontal")
        self.assertIn("GRATING_LOBE", codes(G.validate_geometry(gh, F, F, 10)))

    def test_uca_rule(self):
        self.assertNotIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_uca(5, arc_spacing_m=0.5 * LAM), F, F, 90)))
        self.assertIn("GRATING_LOBE", codes(G.validate_geometry(G.preset_uca(5, arc_spacing_m=0.6 * LAM), F, F, 90)))

    def test_spacing_rank_tolerance_and_beacon(self):
        g = G.preset_ula(4, 0.04 * LAM)
        self.assertIn("GEOMETRY", codes(G.validate_geometry(g, F, F, 60), "error"))
        g = G.preset_ula(4, 0.2 * LAM)
        self.assertIn("GEOMETRY", codes(G.validate_geometry(g, F, F, 60), "warning"))
        g = G.ArrayGeometry(G.preset_ula(4, 0.5 * LAM).elements, element_size_m=0.6 * LAM)
        self.assertTrue(any("overlap" in i.message for i in G.validate_geometry(g, F, F, 60)))
        g = G.ArrayGeometry(G.preset_ula(4, 0.5 * LAM).elements, survey_sigma_m=0.002)
        self.assertTrue(any("lambda/360" in i.message for i in G.validate_geometry(g, F, F, 60)
                            if i.level == "warning"))
        uca = G.preset_uca(5, arc_spacing_m=0.5 * LAM)
        self.assertTrue(any("coplanar" in i.message for i in G.validate_geometry(uca, F, F, 90, look=(30, 20))))
        self.assertFalse(any("coplanar" in i.message for i in G.validate_geometry(uca, F, F, 90, look=(30, 0))))
        ula = G.preset_ula(8, 0.5 * LAM)
        iss = G.validate_geometry(ula, F, F, 60, beacon_range_m=1.0)
        self.assertTrue(any("Fraunhofer" in i.message for i in iss))
        self.assertEqual(G.validate_geometry(G.preset_ula(4, 0.1, map_tx=False), F, F, 60, which="tx"), [])
        one_tx = G.ArrayGeometry([G.Element(0, (0, 0, 0), rx=ChannelRef("s", 0), tx=ChannelRef("s", 0)),
                                  G.Element(1, (0.15, 0, 0), rx=ChannelRef("s", 1))])
        self.assertEqual(codes(G.validate_geometry(one_tx, F, F, 60, which="tx"), "warning"), ["GEOMETRY"])
        self.assertEqual(codes(G.validate_geometry(one_tx, F, F, 60, which="tx"), "error"), [])
        self.assertEqual(codes(G.validate_geometry(one_tx, F, F, 60, which="all"), "error"), [])


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, doc):
        p = os.path.join(self.d, name)
        with open(p, "w") as fh:
            fh.write(json.dumps(doc) if isinstance(doc, dict) else doc)
        return p

    def test_json_units_defaults_apriori(self):
        cfg = two_dev_cfg()
        doc = base_doc(units="mm", defaults={"pattern": {"kind": "cos_q", "q": 1.5}, "polarization": "horizontal"},
                       platform={"heading_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0})
        doc["elements"][1]["pos"] = [150.0, 0, 0]
        doc["elements"][1]["apriori"] = {"rx": {"delay_s": 1.2e-9, "phase_deg": 3.0, "gain_db": 0.2}}
        doc["elements"][1]["pattern"] = {"kind": "table", "table": [[0, 0, 5.0, 0.0], [30, 0, 4.1, 2.0]]}
        doc["elements"][1]["pos_sigma_m"] = 0.001
        g = G.load_geometry(self.write("g.json", doc), cfg)
        self.assertAlmostEqual(g.elements[1].pos_body_m[0], 0.15)
        np.testing.assert_allclose(g.positions()[1], [0, -0.15, 0], atol=1e-12)            # heading 90
        self.assertEqual(g.elements[0].pattern.kind, "cos_q")
        self.assertEqual(g.elements[0].polarization, "horizontal")
        self.assertEqual(g.elements[1].pattern.kind, "table")
        self.assertEqual(g.elements[1].apriori_rx, G.Apriori(1.2e-9, 3.0, 0.2))
        self.assertIsNone(g.elements[1].apriori_tx)
        self.assertEqual(g.elements[1].pos_sigma_m, 0.001)
        self.assertEqual(g.rx_order, cfg.rx_refs())

    def test_json_lambda_enu_lla(self):
        doc = base_doc(units="lambda", design_freq_hz=F, frame="enu",
                       platform={"heading_deg": 45.0})
        doc["elements"][1]["pos"] = [0.5, 0, 0]
        g = G.geometry_from_dict(doc)
        np.testing.assert_allclose(g.positions()[1], [0.5 * LAM, 0, 0], atol=1e-12)        # rotation ignored
        with self.assertRaises(GeometryError):
            G.geometry_from_dict(base_doc(units="lambda"))                                 # needs design freq
        lla = {"schema": G.GEOMETRY_SCHEMA, "frame": "lla",
               "platform": {"site_lla": [47.0, 19.0, 100.0]},
               "elements": [{"id": 0, "lla": [47.0, 19.0, 100.0], "rx": {"device": "a", "channel": 0}},
                            {"id": 1, "lla": [47.0, 19.0001, 100.0], "rx": {"device": "a", "channel": 1}}]}
        g = G.geometry_from_dict(lla)
        self.assertAlmostEqual(g.positions()[1][0], G.lla_to_enu(47.0, 19.0001, 100.0, 47.0, 19.0, 100.0)[0])
        self.assertGreater(g.positions()[1][0], 7.0)
        lla["platform"] = {}
        with self.assertRaises(GeometryError):
            G.geometry_from_dict(lla)

    def test_json_preset_form(self):
        cfg = C.default_config(devices={"sim0": {"family": "sim", "rx_channels": "0..5", "tx_channels": "0..3"}})
        doc = {"schema": G.GEOMETRY_SCHEMA, "units": "lambda", "design_freq_hz": F,
               "preset": {"type": "ula", "n": 6, "spacing": 0.5, "device": "sim0", "map": "auto"}}
        g = G.geometry_from_dict(doc, cfg=cfg)
        self.assertEqual(len(g.elements_for("rx")), 6)
        self.assertEqual(len(g.elements_for("tx")), 4)
        self.assertAlmostEqual(g.min_spacing_m(), 0.5 * LAM)
        doc["preset"] = {"type": "ura", "nx": 2, "ny": 3, "spacing": 0.5, "plane": "horizontal"}
        g = G.geometry_from_dict(doc)
        self.assertEqual(g.ambiguity(), "mirror_elevation")
        doc["preset"] = {"type": "ula", "n": 4, "spacing": 0.5, "map": "manual"}
        with self.assertRaises(GeometryError):
            G.geometry_from_dict(doc)
        doc["preset"] = {"type": "ula", "n": 4, "spacing": 0.5, "bogus": 1}
        with self.assertRaises(GeometryError):
            G.geometry_from_dict(doc)

    def test_json_errors(self):
        cfg = two_dev_cfg()
        bad = base_doc()
        bad["elements"][1]["id"] = 0
        with self.assertRaisesRegex(GeometryError, "duplicate element id"):
            G.geometry_from_dict(bad)
        bad = base_doc()
        bad["elements"][1]["id"] = 2
        with self.assertRaisesRegex(GeometryError, "consecutive"):
            G.geometry_from_dict(bad)
        bad = base_doc()
        bad["elements"][1]["rx"] = {"device": "a", "channel": 0}
        with self.assertRaises(GeometryError) as cm:
            G.geometry_from_dict(bad)
        self.assertEqual(cm.exception.code, "GEOM_MAP")
        bad = base_doc()
        bad["elements"][1]["rx"] = {"device": "zz", "channel": 0}
        with self.assertRaisesRegex(GeometryError, "not in \\[active_devices\\]"):
            G.geometry_from_dict(bad, cfg=cfg)
        bad = base_doc()
        bad["elements"][1]["rx"] = {"device": "b", "channel": 7}
        with self.assertRaisesRegex(GeometryError, "rx_channels"):
            G.geometry_from_dict(bad, cfg=cfg)
        bad = base_doc()
        bad["elements"] = bad["elements"][:1]
        with self.assertRaisesRegex(GeometryError, "at least 2"):
            G.geometry_from_dict(bad)
        bad = base_doc()
        bad["elements"][1]["pos"] = [0, 0, 0]
        with self.assertRaisesRegex(GeometryError, "coincident"):
            G.geometry_from_dict(bad)
        with self.assertRaisesRegex(GeometryError, "schema"):
            G.geometry_from_dict(base_doc(schema="other/1"))
        with self.assertRaisesRegex(GeometryError, "exactly one"):
            G.geometry_from_dict(base_doc(preset={"type": "ula", "n": 2, "spacing": 0.1}))
        with self.assertRaisesRegex(GeometryError, "unknown"):
            G.geometry_from_dict(base_doc(extra=1))
        bad = base_doc()
        bad["elements"][0]["poss"] = [0, 0, 0]
        with self.assertRaisesRegex(GeometryError, "unknown"):
            G.geometry_from_dict(bad)
        p = self.write("bad.json", "{ // comment\n}")
        with self.assertRaises(GeometryError):
            G.load_geometry(p)
        with self.assertRaises(GeometryError):
            G.load_geometry(os.path.join(self.d, "missing.json"))
        with self.assertRaises(GeometryError):
            G.load_geometry(self.write("g.txt", "x"))

    def test_json_round_trip(self):
        cfg = two_dev_cfg()
        doc = base_doc(platform={"heading_deg": 30.0, "pitch_deg": 5.0, "roll_deg": -2.0,
                                 "site_lla": [47.0, 19.0, 100.0]}, survey={"sigma_m": 0.0005})
        doc["elements"].append({"id": 2, "pos": [0.3, 0.01, 0], "rx": {"device": "b", "channel": 0},
                                "tx": {"device": "a", "channel": 1}, "enabled": False, "label": "E2",
                                "apriori": {"tx": {"delay_s": 2e-9, "phase_deg": -4.0, "gain_db": 0.1}}})
        g = G.geometry_from_dict(doc, cfg=cfg)
        p = os.path.join(self.d, "rt.json")
        G.save_geometry(g, p)
        g2 = G.load_geometry(p, cfg)
        self.assertEqual(g2.to_dict(), g.to_dict())
        np.testing.assert_allclose(g2.positions("all"), g.positions("all"))

    def test_csv_round_trip_and_loss(self):
        cfg = two_dev_cfg()
        doc = base_doc()
        doc["elements"].append({"id": 2, "pos": [0.31, 0.02, -0.01], "rx": {"device": "b", "channel": 2},
                                "tx": {"device": "a", "channel": 0}, "enabled": False, "label": "x",
                                "pattern": {"kind": "cos_q", "q": 2.0, "gain_dbi": 6.5}, "pos_sigma_m": 0.002})
        g = G.geometry_from_dict(doc, cfg=cfg)
        self.assertEqual(G.csv_loss_warnings(g), [])
        p = os.path.join(self.d, "g.csv")
        G.save_geometry(g, p)
        with open(p) as fh:
            self.assertTrue(fh.readline().startswith("# units=m"))
        g2 = G.load_geometry(p, cfg)
        np.testing.assert_allclose(g2.positions("all"), g.positions("all"), atol=1e-15)
        for a, b in zip(g.elements, g2.elements):
            self.assertEqual((a.id, a.rx, a.tx, a.enabled, a.label, a.pos_sigma_m),
                             (b.id, b.rx, b.tx, b.enabled, b.label, b.pos_sigma_m))
            self.assertEqual((a.pattern.kind, a.pattern.q, a.pattern.gain_dbi),
                             (b.pattern.kind, b.pattern.q, b.pattern.gain_dbi))
        doc["elements"][0]["apriori"] = {"rx": {"delay_s": 1e-9, "phase_deg": 0, "gain_db": 0}}
        doc["elements"][1]["pattern"] = {"kind": "table", "table": [[0, 0, 3.0, 0.0]]}
        lossy = G.geometry_from_dict(doc, cfg=cfg)
        w = G.csv_loss_warnings(lossy)
        self.assertTrue(any("a-priori" in x for x in w) and any("table" in x for x in w))

    def test_csv_units_and_errors(self):
        text = ("# units=mm design_freq_hz=915e6\n"
                "id,x_m,y_m,z_m,rx_device,rx_channel\n0,0,0,0,sim0,0\n1,163.8,0,0,sim0,1\n")
        g = G.load_geometry(self.write("u.csv", text))
        self.assertAlmostEqual(g.positions()[1][0], 0.1638)
        self.assertEqual(g.design_freq_hz, 915e6)
        with self.assertRaisesRegex(GeometryError, "unknown column"):
            G.load_geometry(self.write("c.csv", "id,x_m,y_m,z_m,colour\n0,0,0,0,red\n1,1,0,0,blue\n"))
        with self.assertRaisesRegex(GeometryError, "both"):
            G.load_geometry(self.write("d.csv", "id,x_m,y_m,z_m,rx_device,rx_channel\n0,0,0,0,sim0,\n1,1,0,0,,\n"))
        with self.assertRaisesRegex(GeometryError, "duplicate"):
            G.load_geometry(self.write("e.csv", "id,x_m,y_m,z_m\n0,0,0,0\n0,1,0,0\n"))


class TestPhysicalGeometryPinning(unittest.TestCase):
    """Physical element positions never follow the configured operating frequency."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_lambda_units_need_the_files_design_freq(self):
        cfg = C.default_config(rf={"center_freq_hz": 2.4e9}, geometry={"design_freq_hz": 2.4e9},
                               devices={"sim0": {"family": "sim", "rx_channels": "0..3"}})
        doc = {"schema": G.GEOMETRY_SCHEMA, "units": "lambda",
               "elements": [{"id": i, "pos": [0.5 * i, 0, 0], "rx": {"device": "sim0", "channel": i}}
                            for i in range(4)]}
        for c in (None, cfg, C.default_config(rf={"center_freq_hz": 2.4e9},
                                              devices={"sim0": {"family": "sim", "rx_channels": "0..3"}})):
            with self.assertRaisesRegex(GeometryError, "units = lambda requires design_freq_hz") as cm:
                G.geometry_from_dict(doc, cfg=c)
            self.assertEqual(cm.exception.code, "GEOMETRY")
        doc["design_freq_hz"] = 915e6                     # the array was built for 915 MHz
        g = G.geometry_from_dict(doc, cfg=cfg)
        self.assertAlmostEqual(g.min_spacing_m(), 0.5 * C0 / 915e6)
        # ... so at 2.4 GHz the grating check now fires (1.31 lambda pitch)
        iss = G.validate_geometry(g, 2.4e9, 2.4e9, 60)
        self.assertIn("GRATING_LOBE", codes(iss, "error"))
        # CSV: same rule for the '# units=lambda' header
        p = os.path.join(self.d, "l.csv")
        with open(p, "w") as fh:
            fh.write("# units=lambda\nid,x_m,y_m,z_m,rx_device,rx_channel\n0,0,0,0,sim0,0\n1,0.5,0,0,sim0,1\n")
        for c in (None, cfg):
            with self.assertRaisesRegex(GeometryError, "units = lambda requires design_freq_hz"):
                G.load_geometry(p, c)
        with open(p, "w") as fh:
            fh.write("# units=lambda design_freq_hz=915e6\nid,x_m,y_m,z_m,rx_device,rx_channel\n"
                     "0,0,0,0,sim0,0\n1,0.5,0,0,sim0,1\n")
        self.assertAlmostEqual(G.load_geometry(p).min_spacing_m(), 0.5 * C0 / 915e6)

    def test_metre_files_do_not_inherit_center_freq(self):
        cfg = C.default_config(rf={"center_freq_hz": 2.4e9}, devices={"a": {"family": "sim", "rx_channels": "0..1"}})
        g = G.geometry_from_dict(base_doc(), cfg=cfg)
        self.assertEqual(g.design_freq_hz, 0.0)           # label only; never the operating frequency
        self.assertAlmostEqual(g.min_spacing_m(), 0.15)

    def test_preset_pitch_derived_from_operating_freq(self):
        cfg = C.default_config()                          # spacing_m 0, design_freq_hz 0
        out = []
        g = G.geometry_from_config(cfg, issues=out)
        self.assertEqual(codes(out, "warning"), ["GEOMETRY"])
        self.assertIn("derived from the operating frequency", out[0].message)
        self.assertEqual(g.provenance_issues, tuple(out))
        self.assertAlmostEqual(g.preset_spacing_m, 0.5 * C0 / 915e6)
        self.assertIn("derived from the operating frequency",
                      " ".join(i.message for i in G.validate_geometry(g, 915e6, 915e6, 60)))
        tx = C.default_config(tx={"en_tx": 1})
        self.assertEqual(codes(G.geometry_config_issues(tx), "error"), ["GEOMETRY"])
        self.assertIn("GEOMETRY", codes(G.validate_geometry(G.geometry_from_config(tx), 915e6, 915e6, 60),
                                        "error"))
        for pinned in ({"spacing_m": 0.16}, {"design_freq_hz": 915e6}):
            c = C.default_config(tx={"en_tx": 1}, geometry=pinned)
            self.assertEqual(G.geometry_config_issues(c), [])
            self.assertEqual(G.geometry_from_config(c).provenance_issues, ())
        # the provenance survives derived geometries
        self.assertEqual(g.with_orientation(G.Orientation(10)).provenance_issues, g.provenance_issues)

    def test_uca_pinned_by_radius_is_not_frequency_derived(self):
        dev = {"sim0": {"family": "sim", "rx_channels": "0..7", "tx_channels": "0..7"}}
        geo = {"preset": "uca", "num_elements": 8, "radius_m": 0.3}
        cfg = C.default_config(geometry=geo, devices=dev, tx={"en_tx": 1})
        self.assertEqual(G.geometry_config_issues(cfg), [])
        out = []
        g = G.geometry_from_config(cfg, issues=out)
        self.assertEqual((out, g.provenance_issues), ([], ()))
        self.assertNotIn("GEOMETRY", codes(G.validate_geometry(g, 915e6, 915e6, 90, which="tx"), "error"))
        self.assertAlmostEqual(g.preset_spacing_m, 2 * 0.3 * math.sin(math.pi / 8))        # the arc pitch used
        self.assertAlmostEqual(g.min_spacing_m(), g.preset_spacing_m)
        g24 = G.geometry_from_config(C.default_config(geometry=geo, devices=dev, rf={"center_freq_hz": 2.4e9}))
        np.testing.assert_allclose(g24.positions(), g.positions())
        # a UCA sized by spacing_lambda (radius_m = 0) still follows the operating frequency
        loose = C.default_config(geometry={"preset": "uca", "num_elements": 8}, devices=dev, tx={"en_tx": 1})
        self.assertEqual(codes(G.geometry_config_issues(loose), "error"), ["GEOMETRY"])
        self.assertEqual(g.scan_limit_deg, 60.0)


class TestEnuFrameAttitude(unittest.TestCase):
    """enu / lla geometries keep their positions but take the platform attitude as boresight."""

    def _east_panel(self):
        d = 0.6 * LAM
        return G.preset_ura(3, 3, d, d).with_orientation(G.Orientation(heading_deg=90))

    def test_body_and_equivalent_enu_validate_identically(self):
        import active_pattern as AP
        body = self._east_panel()
        enu_pos = body.positions("all") + body.reference_point_enu()
        doc = {"schema": G.GEOMETRY_SCHEMA, "frame": "enu", "platform": {"heading_deg": 90.0},
               "elements": [{"id": i, "pos": [float(c) for c in p], "rx": {"device": "sim0", "channel": i},
                             "tx": {"device": "sim0", "channel": i}} for i, p in enumerate(enu_pos)]}
        enu = G.geometry_from_dict(doc)
        np.testing.assert_allclose(enu.positions(), body.positions(), atol=1e-12)
        np.testing.assert_allclose(enu.boresight_enu(), [1, 0, 0], atol=1e-12)
        vb = G.validate_geometry(body, F, F, 30)
        ve = G.validate_geometry(enu, F, F, 30)
        self.assertEqual([(i.level, i.code, i.message) for i in vb], [(i.level, i.code, i.message) for i in ve])
        self.assertEqual(codes(ve, "error"), [])
        self.assertAlmostEqual(G.grating_free_scan_deg(enu, F), G.grating_free_scan_deg(body, F))
        rb = AP.geometry_report(body, F, F, 30, step_deg=5.0)
        re_ = AP.geometry_report(enu, F, F, 30, step_deg=5.0)
        self.assertEqual(rb.grating_free_scan_deg, re_.grating_free_scan_deg)
        self.assertEqual([(i.level, i.code) for i in rb.issues], [(i.level, i.code) for i in re_.issues])
        self.assertEqual(len(rb.lobes), len(re_.lobes))
        # element patterns follow the attitude too (body +y = East)
        pat = G.ElementPattern("cos_q", q=2)
        els = [G.Element(i, (0.15 * i, 0, 0), rx=ChannelRef("s", i), pattern=pat) for i in range(2)]
        ge = G.ArrayGeometry(els, orientation=G.Orientation(heading_deg=90), frame="enu")
        np.testing.assert_allclose(ge.enu_to_body([1, 0, 0]), [0, 1, 0], atol=1e-12)
        np.testing.assert_allclose(ge.positions()[1], [0.15, 0, 0], atol=1e-12)          # not rotated

    def test_with_orientation_on_enu_changes_only_boresight(self):
        doc = base_doc(frame="enu")
        g = G.geometry_from_dict(doc)
        np.testing.assert_allclose(g.boresight_enu(), [0, 1, 0], atol=1e-12)
        g2 = g.with_orientation(G.Orientation(heading_deg=180))
        np.testing.assert_allclose(g2.positions(), g.positions(), atol=1e-15)
        np.testing.assert_allclose(g2.boresight_enu(), [0, -1, 0], atol=1e-12)
        self.assertAlmostEqual(g2.angle_from_boresight_deg(G.direction_vector(90, 0)), 90.0)

    def test_lla_attitude(self):
        lla = {"schema": G.GEOMETRY_SCHEMA, "frame": "lla",
               "platform": {"site_lla": [47.0, 19.0, 100.0], "heading_deg": 180.0},
               "elements": [{"id": 0, "lla": [47.0, 19.0, 100.0], "rx": {"device": "a", "channel": 0}},
                            {"id": 1, "lla": [47.0, 19.000002, 100.0], "rx": {"device": "a", "channel": 1}}]}
        g = G.geometry_from_dict(lla)
        np.testing.assert_allclose(g.boresight_enu(), [0, -1, 0], atol=1e-12)
        self.assertGreater(g.positions()[1][0], 0.1)                                      # still East
        self.assertAlmostEqual(g.positions()[1][1], 0.0, places=6)

    def test_csv_round_trip_of_enu_geometry_with_config_heading(self):
        cfg = C.default_config(geometry={"heading_deg": 90.0},
                               devices={"sim0": {"family": "sim", "rx_channels": "0..3"}})
        doc = {"schema": G.GEOMETRY_SCHEMA, "frame": "enu",
               "elements": [{"id": i, "pos": [0.15 * i, 0, 0], "rx": {"device": "sim0", "channel": i}}
                            for i in range(4)]}
        g = G.geometry_from_dict(doc, cfg=cfg)
        np.testing.assert_allclose(g.positions()[3], [0.45, 0, 0], atol=1e-12)
        np.testing.assert_allclose(g.boresight_enu(), [1, 0, 0], atol=1e-12)            # config attitude
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "g.csv")
            G.save_geometry(g, p)
            with open(p) as fh:
                self.assertIn("frame=enu", fh.readline())
            g2 = G.load_geometry(p, cfg)
        self.assertEqual(g2.frame, "enu")
        np.testing.assert_allclose(g2.positions(), g.positions(), atol=1e-15)            # East-West kept
        np.testing.assert_allclose(g2.boresight_enu(), g.boresight_enu(), atol=1e-12)
        self.assertTrue(any("attitude" in w for w in G.csv_loss_warnings(g)))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.csv")
            with open(p, "w") as fh:
                fh.write("# frame=lla\nid,x_m,y_m,z_m\n0,0,0,0\n1,1,0,0\n")
            with self.assertRaisesRegex(GeometryError, "frame"):
                G.load_geometry(p)


class TestFromConfig(unittest.TestCase):
    def test_default_preset(self):
        cfg = C.default_config()
        g = G.geometry_from_config(cfg)
        lam = C0 / 915e6
        self.assertEqual(len(g.elements_for("rx")), 4)
        self.assertEqual(g.elements_for("tx"), ())
        self.assertAlmostEqual(g.min_spacing_m(), lam / 2)
        np.testing.assert_allclose(g.positions()[3], [1.5 * lam, 0, 0], atol=1e-12)
        self.assertEqual(g.channel_indices("rx", cfg.rx_refs()), [0, 1, 2, 3])

    def test_multi_device_map_and_warnings(self):
        cfg = two_dev_cfg(geometry={"num_elements": 6, "spacing_m": 0.1, "heading_deg": 90,
                                    "element_pattern": "cos_q", "element_q": 2},
                          antenna={"en_antenna_profile": 1, "element_gain_dbi": 5.0},
                          calibration={"loopback_rx_chan": 6})
        g = G.geometry_from_config(cfg)
        self.assertEqual([e.rx for e in g.elements_for("rx")], cfg.rx_refs()[:6])
        self.assertEqual([e.tx for e in g.elements_for("tx")], cfg.tx_refs())
        self.assertEqual(g.elements[0].pattern, G.ElementPattern("cos_q", 2.0, 5.0))
        np.testing.assert_allclose(g.boresight_enu(), [1, 0, 0], atol=1e-12)
        iss = G.geometry_map_issues(g, cfg)
        self.assertEqual(codes(iss, "error"), [])
        self.assertFalse(any("global RX 6" in i.message for i in iss))          # loopback channel exempt
        cfg2 = two_dev_cfg(geometry={"num_elements": 5, "spacing_m": 0.1})
        iss = G.geometry_map_issues(G.geometry_from_config(cfg2), cfg2)
        self.assertTrue(any("global RX 5" in i.message for i in iss if i.level == "warning"))
        cfg3 = two_dev_cfg(geometry={"num_elements": 9, "spacing_m": 0.1})
        g3 = G.geometry_from_config(cfg3)
        self.assertEqual(len(g3.elements_for("rx")), 7)
        self.assertTrue(any("maps no RX or TX" in i.message for i in G.geometry_map_issues(g3, cfg3)))

    def test_presets_ura_uca_tri(self):
        for preset, extra, n in (("ura", {"nx": 2, "ny": 2}, 4), ("uca", {"num_elements": 4}, 4),
                                 ("tri", {"nx": 2, "ny": 2}, 4)):
            cfg = C.default_config(geometry=dict(preset=preset, **extra))
            g = G.geometry_from_config(cfg)
            self.assertEqual(len(g.elements_for("rx")), n)
        cfg = C.default_config(geometry={"preset": "uca", "num_elements": 4, "radius_m": 0.2})
        P = G.geometry_from_config(cfg).positions()
        np.testing.assert_allclose(np.linalg.norm(P - P.mean(axis=0), axis=1), 0.2, atol=1e-12)

    def test_geometry_file_relative(self):
        with tempfile.TemporaryDirectory() as d:
            doc = base_doc()
            doc["elements"][0]["rx"] = {"device": "b", "channel": 1}
            with open(os.path.join(d, "geo.json"), "w") as fh:
                json.dump(doc, fh)
            cfg = two_dev_cfg(geometry={"geometry_file": "geo.json"}, base_dir=d)
            g = G.geometry_from_config(cfg)
            self.assertEqual([e.id for e in g.elements_for("rx")], [1, 0])      # a:1 before b:1


class TestScanDirections(unittest.TestCase):
    def test_cone_sampling(self):
        U = G.scan_directions([0, 1, 0], 30, 2.0)
        ang = np.degrees(np.arccos(np.clip(U @ [0, 1, 0], -1, 1)))
        self.assertAlmostEqual(ang.max(), 30.0, places=9)
        self.assertAlmostEqual(ang.min(), 0.0, places=9)
        np.testing.assert_allclose(np.linalg.norm(U, axis=1), 1.0)
        self.assertEqual(G.scan_directions([0, 1, 0], 0).shape, (1, 3))


if __name__ == "__main__":
    unittest.main()
