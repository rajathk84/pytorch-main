#!/bin/bash

# shellcheck source=./common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "Testing pytorch docs"

cd docs
TERM=vt100 make doctest
