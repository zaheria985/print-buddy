"""Scale and copy-count choices offered in Discord. No slicer: fit checks are stubbed."""
import unittest

from print_buddy.core import orca_adapter as oa
from print_buddy.core import quantity_selection as qs
import math


class FeatureClearanceTests(unittest.TestCase):
    def test_margin_applied_once_and_features_add_to_edge_only(self):
        state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
                 'resolved_hop_types': ['Normal Lift'],
                 'brim_policy': {'type': 'outer_only', 'width_mm': 2}}
        self.assertEqual(oa.required_feature_edge(state), 2)
        self.assertEqual(oa._usable(state), (214.0, 214.0, 220.0))
        self.assertEqual(oa._usable(state, single=False), (210.0, 210.0, 220.0))

    def test_feature_sweep_and_brim_are_added_once_to_distinct_margins(self):
        state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
                 'resolved_hop_types': ['Spiral Lift'], 'z_hop': 0.4, 'travel_slope': 3,
                 'brim_policy': {'type': 'outer_only', 'width_mm': 2}}
        sweep = 0.4 / math.tan(math.radians(3)) / (2 * math.pi)
        required = 2 + sweep
        self.assertAlmostEqual(oa.required_feature_edge(state), required)
        self.assertAlmostEqual(oa._usable(state)[0], 220 - 2 * (1 + required))
        self.assertAlmostEqual(oa._usable(state, single=False)[0], 220 - 2 * (3 + required))
        self.assertAlmostEqual(oa._usable(state, single=False)[0], 220 - 2*3 - 2*required)
        self.assertAlmostEqual(oa.resolved_travel_sweep({'resolved_hop_types': ['Normal Lift'], 'z_hop': 0.4, 'travel_slope': 3}), 0)

    def test_scale_fit_keeps_10mm_part_gap_distinct_from_edge_clearance(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(oa, 'source_triangles', return_value=None), \
             patch.object(oa, 'orca_plate_capacity', return_value=1) as capacity:
            oa.verified_scale_choices(__import__('pathlib').Path(tmp) / 'tiny.stl',
                                     {'bed_mm': [220, 220], 'resolved_hop_types': ['Normal Lift']})
        self.assertEqual(capacity.call_args.kwargs['copy_limit'], 1)
        self.assertNotIn('spacing_mm', capacity.call_args.kwargs)  # 10 mm packing gap belongs only to multi-copy layout.
        self.assertIsNone(capacity.call_args.kwargs['brim_policy'])

    def test_fit_question_names_part_extent_options_and_future_cut_issue(self):
        from print_buddy.core.prepared_intake import fit_question
        text = fit_question('Model', {'scales': [0.95], 'parts': [{'name': 'Stand', 'extent_mm': [130, 230, 30],
                             'orientations_checked': ['authored XY', 'best yaw 90°']} ]})
        self.assertIn('Stand (130 × 230 × 30 mm)', text)
        self.assertNotIn('Orientations checked', text)
        self.assertIn('95%', text)
        self.assertNotIn('issues/83', text)  # no GitHub links or option essays in chat

    def test_painted_scale_search_uses_small_coarse_ladder(self):
        from print_buddy.core.model_job import PAINTED_SCALE_STEPS
        self.assertLessEqual(len(PAINTED_SCALE_STEPS), 12)
        self.assertEqual(PAINTED_SCALE_STEPS[0], 1.0)
        self.assertEqual(PAINTED_SCALE_STEPS[-1], 0.5)

    def test_fine_search_deferred_for_painted_multi_plate_jobs(self):
        from unittest.mock import patch
        with patch.object(oa, '_source_plate_count', return_value=2), \
             patch.object(oa, 'source_triangles', return_value=None):
            self.assertEqual(oa.verified_scale_choices('painted.3mf', {'resolved_hop_types': ['Normal Lift']}), [])

    def test_explicit_brim_policy_is_selected_for_tight_fit(self):
        from unittest.mock import patch
        with patch.object(oa, 'verified_scale_choices', side_effect=lambda _s, _st, brim_policy=None: [0.95] if brim_policy['width_mm'] == 1 else []), \
             patch.object(oa, 'rotation_alternatives', return_value=[]):
            options = oa.fit_options('stand.stl', {'z_hop_types': ['Normal Lift']})
        self.assertEqual(options['brim_policy'], {'type': 'outer_only', 'width_mm': 1.0, 'label': 'Outer-only brim, 1 mm'})

    def test_operator_brim_override_is_respected(self):
        from unittest.mock import patch
        with patch.object(oa, 'verified_scale_choices', return_value=[0.9]) as verify, \
             patch.object(oa, 'rotation_alternatives', return_value=[]):
            options = oa.fit_options('stand.stl', {'z_hop_types': ['Normal Lift']}, brim_override=4)
        self.assertEqual(options['brim_policy']['width_mm'], 4.0)
        self.assertEqual(verify.call_args.kwargs['brim_policy']['width_mm'], 4.0)

    def test_spiral_sweep_is_radius_formula(self):
        state = {'resolved_hop_types': ['Spiral Lift'], 'z_hop': 0.4, 'travel_slope': 3}
        import math
        self.assertAlmostEqual(oa.resolved_travel_sweep(state), 0.4 / math.tan(math.radians(3)) / (2 * math.pi))


class ScaleLabelTests(unittest.TestCase):
    def test_whole_and_fractional_percent(self):
        self.assertEqual(oa.scale_label(1), "100%")
        self.assertEqual(oa.scale_label(0.75), "75%")
        self.assertEqual(oa.scale_label(0.986), "98.6%")


class ScaleLadderTests(unittest.TestCase):
    def test_too_small_to_offer(self):
        self.assertEqual(oa._scale_ladder(0.51), [])

    def test_large_maximum_offers_75_and_50(self):
        self.assertEqual(oa._scale_ladder(0.9), [0.9, 0.75, 0.5])

    def test_medium_sits_between_max_and_half(self):
        self.assertEqual(oa._scale_ladder(0.7), [0.7, 0.6, 0.5])

    def test_choices_never_repeat(self):
        ladder = oa._scale_ladder(0.52)
        self.assertEqual(len(set(ladder)), 3)


class ConfirmScaleTests(unittest.TestCase):
    def test_finds_largest_fitting_tenth_of_a_percent(self):
        self.assertEqual(oa._confirm_scale(lambda s: s <= 0.873, 0.95), 0.873)

    def test_bound_that_fits_is_returned_without_search(self):
        calls = []
        self.assertEqual(oa._confirm_scale(lambda s: calls.append(s) or True, 0.9), 0.9)
        self.assertEqual(calls, [0.9])

    def test_bound_under_half_size_gives_up(self):
        self.assertIsNone(oa._confirm_scale(lambda s: True, 0.4))

    def test_one_orca_crash_is_retried(self):
        seen = set()

        def flaky(scale):
            if scale not in seen:
                seen.add(scale)
                raise RuntimeError("bad_rational")
            return True
        self.assertEqual(oa._confirm_scale(flaky, 0.8), 0.8)

    def test_broken_orca_is_not_reported_as_does_not_fit(self):
        def broken(scale):
            raise RuntimeError("orca died")
        with self.assertRaises(RuntimeError):
            oa._confirm_scale(broken, 0.9)


class QuantityTests(unittest.TestCase):
    def test_buttons(self):
        self.assertEqual(oa.quantity_buttons(0), [1])
        self.assertEqual(oa.quantity_buttons(2), [1, 2])
        self.assertEqual(oa.quantity_buttons(6), [1, 2, 6])

    def test_options(self):
        self.assertEqual(qs.quantity_options(1), [("One copy", 1)])
        self.assertEqual(qs.quantity_options(5)[-1], ("Max (5 copies)", 5))

    def test_parse_accepts_plain_phrasing(self):
        self.assertEqual(qs.parse_quantity("3", 4), 3)
        self.assertEqual(qs.parse_quantity("Print 2 copies", 4), 2)

    def test_parse_rejects_vague_or_too_many(self):
        with self.assertRaises(ValueError):
            qs.parse_quantity("a few", 4)
        with self.assertRaisesRegex(ValueError, "will not fit"):
            qs.parse_quantity("9", 4)
