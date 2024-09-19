#include <torch/csrc/jit/runtime/print_handler.h>

#include <atomic>
#include <iostream>
#include <string>

namespace torch::jit {

namespace {

std::atomic<PrintHandler> print_handler(getDefaultPrintHandler());

} // namespace

PrintHandler getDefaultPrintHandler() {
  return [](const std::string& s) { std::cout << s; };
}

PrintHandler getPrintHandler() {
  return print_handler.load();
}

void setPrintHandler(PrintHandler ph) {
  print_handler.store(ph);
}

} // namespace torch::jit
