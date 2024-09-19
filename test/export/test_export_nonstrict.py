# Owner(s): ["oncall: export"]

try:
    from . import test_export, testing
except ImportError:
    import test_export  # @manual=fbcode//caffe2/test:test_export-library
    import testing  # @manual=fbcode//caffe2/test:test_export-library

from torch.export import export


test_classes = {}


def mocked_non_strict_export(*args, **kwargs):
    # If user already specified strict, don't make it non-strict
    if "strict" in kwargs:
        return export(*args, **kwargs)
    return export(*args, **kwargs, strict=False)


def make_dynamic_cls(cls):
    cls_prefix = "NonStrictExport"

    test_class = testing.make_test_cls_with_mocked_export(
        cls,
        cls_prefix,
        test_export.NON_STRICT_SUFFIX,
        mocked_non_strict_export,
        xfail_prop="_expected_failure_non_strict",
    )

    test_classes[test_class.__name__] = test_class
    # REMOVING THIS LINE WILL STOP TESTS FROM RUNNING
    globals()[test_class.__name__] = test_class
    test_class.__module__ = __name__
    return test_class


tests = [
    test_export.TestDynamismExpression,
    test_export.TestExport,
]
for test in tests:
    make_dynamic_cls(test)
del test

if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
