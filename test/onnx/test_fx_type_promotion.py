# Owner(s): ["module: onnx"]

from torch.onnx._internal.fx.passes import type_promotion
from torch.testing._internal import common_utils


class TestGeneratedTypePromotionRuleSet(common_utils.TestCase):
    def test_generated_rule_set_is_up_to_date(self):
        generated_set = type_promotion._GENERATED_ATEN_TYPE_PROMOTION_RULE_SET
        latest_set = type_promotion.ElementwiseTypePromotionRuleSetGenerator.generate_from_torch_refs()

        # Please update the list in torch/onnx/_internal/fx/passes/type_promotion.py following the instruction
        # if this test fails
        self.assertEqual(generated_set, latest_set)

    def test_initialize_type_promotion_table_succeeds(self):
        type_promotion.TypePromotionTable()


if __name__ == "__main__":
    common_utils.run_tests()
