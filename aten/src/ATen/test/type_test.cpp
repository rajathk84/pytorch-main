#include <ATen/ATen.h>
#include <gtest/gtest.h>
#include <torch/torch.h>
#include <ATen/core/jit_type.h>
#include <torch/csrc/jit/frontend/resolver.h>
#include <torch/csrc/jit/serialization/import_source.h>

namespace c10 {

TEST(TypeCustomPrinter, Basic) {
  TypePrinter printer =
      [](const Type& t) -> std::optional<std::string> {
    if (auto tensorType = t.cast<TensorType>()) {
      return "CustomTensor";
    }
    return std::nullopt;
  };

  // Tensor types should be rewritten
  torch::Tensor iv = torch::rand({2, 3});
  const auto type = TensorType::create(iv);
  EXPECT_EQ(type->annotation_str(), "Tensor");
  EXPECT_EQ(type->annotation_str(printer), "CustomTensor");

  // Unrelated types should not be affected
  const auto intType = IntType::get();
  EXPECT_EQ(intType->annotation_str(printer), intType->annotation_str());
}

TEST(TypeCustomPrinter, ContainedTypes) {
  TypePrinter printer =
      [](const Type& t) -> std::optional<std::string> {
    if (auto tensorType = t.cast<TensorType>()) {
      return "CustomTensor";
    }
    return std::nullopt;
  };
  torch::Tensor iv = torch::rand({2, 3});
  const auto type = TensorType::create(iv);

  // Contained types should work
  const auto tupleType = TupleType::create({type, IntType::get(), type});
  EXPECT_EQ(tupleType->annotation_str(), "Tuple[Tensor, int, Tensor]");
  EXPECT_EQ(
      tupleType->annotation_str(printer), "Tuple[CustomTensor, int, CustomTensor]");
  const auto dictType = DictType::create(IntType::get(), type);
  EXPECT_EQ(dictType->annotation_str(printer), "Dict[int, CustomTensor]");
  const auto listType = ListType::create(tupleType);
  EXPECT_EQ(
      listType->annotation_str(printer),
      "List[Tuple[CustomTensor, int, CustomTensor]]");
}

TEST(TypeCustomPrinter, NamedTuples) {
  TypePrinter printer =
      [](const Type& t) -> std::optional<std::string> {
    if (auto tupleType = t.cast<TupleType>()) {
      // Rewrite only NamedTuples
      if (tupleType->name()) {
        return "Rewritten";
      }
    }
    return std::nullopt;
  };
  torch::Tensor iv = torch::rand({2, 3});
  const auto type = TensorType::create(iv);

  std::vector<std::string> field_names = {"foo", "bar"};
  const auto namedTupleType = TupleType::createNamed(
      "my.named.tuple", field_names, {type, IntType::get()});
  EXPECT_EQ(namedTupleType->annotation_str(printer), "Rewritten");

  // Put it inside another tuple, should still work
  const auto outerTupleType = TupleType::create({IntType::get(), namedTupleType});
  EXPECT_EQ(outerTupleType->annotation_str(printer), "Tuple[int, Rewritten]");
}

static TypePtr importType(
    std::shared_ptr<CompilationUnit> cu,
    const std::string& qual_name,
    const std::string& src) {
  std::vector<at::IValue> constantTable;
  auto source = std::make_shared<torch::jit::Source>(src);
  torch::jit::SourceImporter si(
      cu,
      &constantTable,
      [&](const std::string& name) -> std::shared_ptr<torch::jit::Source> {
        return source;
      },
      /*version=*/2);
  return si.loadType(qual_name);
}

TEST(TypeEquality, ClassBasic) {
  // Even if classes have the same name across two compilation units, they
  // should not compare equal.
  auto cu = std::make_shared<CompilationUnit>();
  const auto src = R"JIT(
class First:
    def one(self, x: Tensor, y: Tensor) -> Tensor:
      return x
)JIT";

  auto classType = importType(cu, "__torch__.First", src);
  auto classType2 = cu->get_type("__torch__.First");
  // Trivially these should be equal
  EXPECT_EQ(*classType, *classType2);
}

TEST(TypeEquality, ClassInequality) {
  // Even if classes have the same name across two compilation units, they
  // should not compare equal.
  auto cu = std::make_shared<CompilationUnit>();
  const auto src = R"JIT(
class First:
    def one(self, x: Tensor, y: Tensor) -> Tensor:
      return x
)JIT";

  auto classType = importType(cu, "__torch__.First", src);

  auto cu2 = std::make_shared<CompilationUnit>();
  const auto src2 = R"JIT(
class First:
    def one(self, x: Tensor, y: Tensor) -> Tensor:
      return y
)JIT";

  auto classType2 = importType(cu2, "__torch__.First", src2);
  EXPECT_NE(*classType, *classType2);
}

TEST(TypeEquality, InterfaceEquality) {
  // Interfaces defined anywhere should compare equal, provided they share a
  // name and interface
  auto cu = std::make_shared<CompilationUnit>();
  const auto interfaceSrc = R"JIT(
class OneForward(Interface):
    def one(self, x: Tensor, y: Tensor) -> Tensor:
        pass
    def forward(self, x: Tensor) -> Tensor:
        pass
)JIT";
  auto interfaceType = importType(cu, "__torch__.OneForward", interfaceSrc);

  auto cu2 = std::make_shared<CompilationUnit>();
  auto interfaceType2 = importType(cu2, "__torch__.OneForward", interfaceSrc);

  EXPECT_EQ(*interfaceType, *interfaceType2);
}

TEST(TypeEquality, InterfaceInequality) {
  // Interfaces must match for them to compare equal, even if they share a name
  auto cu = std::make_shared<CompilationUnit>();
  const auto interfaceSrc = R"JIT(
class OneForward(Interface):
    def one(self, x: Tensor, y: Tensor) -> Tensor:
        pass
    def forward(self, x: Tensor) -> Tensor:
        pass
)JIT";
  auto interfaceType = importType(cu, "__torch__.OneForward", interfaceSrc);

  auto cu2 = std::make_shared<CompilationUnit>();
  const auto interfaceSrc2 = R"JIT(
class OneForward(Interface):
    def two(self, x: Tensor, y: Tensor) -> Tensor:
        pass
    def forward(self, x: Tensor) -> Tensor:
        pass
)JIT";
  auto interfaceType2 = importType(cu2, "__torch__.OneForward", interfaceSrc2);

  EXPECT_NE(*interfaceType, *interfaceType2);
}

TEST(TypeEquality, TupleEquality) {
  // Tuples should be structurally typed
  auto type = TupleType::create({IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});
  auto type2 = TupleType::create({IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});

  EXPECT_EQ(*type, *type2);
}

TEST(TypeEquality, NamedTupleEquality) {
  // Named tuples should compare equal if they share a name and field names
  std::vector<std::string> fields = {"a", "b", "c", "d"};
  std::vector<std::string> otherFields = {"wow", "so", "very", "different"};
  auto type = TupleType::createNamed(
      "MyNamedTuple",
      fields,
      {IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});
  auto type2 = TupleType::createNamed(
      "MyNamedTuple",
      fields,
      {IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});
  EXPECT_EQ(*type, *type2);

  auto differentName = TupleType::createNamed(
      "WowSoDifferent",
      fields,
      {IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});
  EXPECT_NE(*type, *differentName);

  auto differentField = TupleType::createNamed(
      "MyNamedTuple",
      otherFields,
      {IntType::get(), TensorType::get(), FloatType::get(), ComplexType::get()});
  EXPECT_NE(*type, *differentField);
}
} // namespace c10
