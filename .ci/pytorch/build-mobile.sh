#!/usr/bin/env bash
# DO NOT ADD 'set -x' not to reveal CircleCI secret context environment variables
set -eu -o pipefail

# This script uses linux host toolchain + mobile build options in order to
# build & test mobile libtorch without having to setup Android/iOS
# toolchain/simulator.

# shellcheck source=./common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# shellcheck source=./common-build.sh
source "$(dirname "${BASH_SOURCE[0]}")/common-build.sh"

# Install torch & torchvision - used to download & trace test model.
# Ideally we should use the libtorch built on the PR so that backward
# incompatible changes won't break this script - but it will significantly slow
# down mobile CI jobs.
# Here we install nightly instead of stable so that we have an option to
# temporarily skip mobile CI jobs on BC-breaking PRs until they are in nightly.
retry pip install --pre torch torchvision \
  -f https://download.pytorch.org/whl/nightly/cpu/torch_nightly.html \
  --progress-bar off

# Run end-to-end process of building mobile library, linking into the predictor
# binary, and running forward pass with a real model.
if [[ "$BUILD_ENVIRONMENT" == *-mobile-custom-build-static* ]]; then
  TEST_CUSTOM_BUILD_STATIC=1 test/mobile/custom_build/build.sh
elif [[ "$BUILD_ENVIRONMENT" == *-mobile-lightweight-dispatch* ]]; then
  test/mobile/lightweight_dispatch/build.sh
else
  TEST_DEFAULT_BUILD=1 test/mobile/custom_build/build.sh
fi

print_sccache_stats
