#include <ATen/TensorNames.h>
#include <ATen/WrapDimUtils.h>
#include <c10/util/irange.h>

namespace at::namedinference {


Dimname TensorName::toDimname() const {
  return name_;
}

const TensorName& TensorName::unify(const TensorName& other, const char* op_name) const {
  // unify(None, None)
  if (name_.isWildcard() && other.name_.isWildcard()) {
    return *this;
  }

  // unify(A, A)
  if (name_ == other.name_) {
    return *this;
  }

  // unify(A, None)
  if (other.name_.isWildcard()) {
    const auto it = std::find(other.origin_.begin(), other.origin_.end(), name_);
    TORCH_CHECK(it == other.origin_.end(),
        op_name, ":",
        " Cannot match ", *this, " with ", other,
        " because the latter names already have ", name_, ".",
        " Are your tensors misaligned?");
    return *this;
  }

  // unify(None, A)
  if (name_.isWildcard()) {
    return other.unify(*this, op_name);
  }

  // unify(A, B)
  TORCH_CHECK(name_ == other.name_,
      op_name, ":",
      " Expected ", *this,
      " to match ", other,
      " but they do not match.");
  return *this;
}

TensorNames::TensorNames(ArrayRef<Dimname> names) {
  names_.reserve(names.size());
  for (const auto idx : c10::irange(names.size())) {
    names_.emplace_back(names, idx);
  }
}

TensorNames::TensorNames(ArrayRef<Dimname> names, int64_t start, int64_t end) {
  int64_t names_size = static_cast<int64_t>(names.size());
  start = maybe_wrap_dim(start, names_size);
  end = maybe_wrap_dim(end, names_size);
  names_.reserve(end - start);
  for (const auto idx : c10::irange(start, end)) {
    names_.emplace_back(names, idx);
  }
}

TensorNames& TensorNames::unifyFromRightInplace(const TensorNames& other, const char* op_name) {

  if (names_.size() > other.names_.size()) {
    const auto size_diff = names_.size() - other.names_.size();
    for (const auto idx : c10::irange(size_diff, names_.size())) {
      names_[idx] = names_[idx].unify(other.names_[idx - size_diff], op_name);
    }
  } else {
    const auto size_diff = other.names_.size() - names_.size();
    // pad names_ to the same length as other.names_ before unification
    names_.insert(
        names_.begin(),
        other.names_.begin(),
        other.names_.begin() + size_diff);
    for (const auto idx : c10::irange(size_diff, names_.size())) {
      names_[idx] = names_[idx].unify(other.names_[idx], op_name);
    }
  }

  return *this;
}

void TensorNames::append(TensorName name) {
  names_.emplace_back(name);
}

void TensorNames::checkUnique(const char* op_name) const {
  // O(N^2), but named tensors can have at most N = 64 dimensions, so this
  // doesn't matter unless benchmarking tells us it does. The alternative is
  // to create some sort of set data structure but the overhead of that
  // might dominate for small sizes.
  for (auto it = names_.begin(); it != names_.end(); ++it) {
    const auto name = it->toDimname();
    if (name.isWildcard()) continue;

    auto dup = std::find_if(it + 1, names_.end(),
        [&](const TensorName& other) { return other.toDimname() == name; });
    TORCH_CHECK(dup == names_.end(),
        op_name, ": ",
        "Attempted to propagate dims ", *it, " and ", *dup, " to the output, ",
        "but that would create a tensor with duplicate names [", toDimnameVec(),
        "]. Please rename your inputs with Tensor.rename to prevent this.");
  }
}

// Let's say the TensorName represents 'C' in ['N', 'C', 'H, 'W'].
// It should print like:
// 'C' (index 1 of ['N', 'C', 'H', 'W'])
std::ostream& operator<<(std::ostream& out, const TensorName& tensorname) {
  out << tensorname.name_ << " (index ";
  out << tensorname.origin_idx_ << " of ";
  out << tensorname.origin_ << ")";
  return out;
}

std::vector<Dimname> TensorNames::toDimnameVec() const {
  std::vector<Dimname> result;
  result.reserve(names_.size());
  for (const auto& tensor_name : names_) {
    result.emplace_back(tensor_name.toDimname());
  }
  return result;
}


} // namespace at::namedinference
