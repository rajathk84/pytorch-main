#!/bin/bash

set -ex

if [ -n "$CLANG_VERSION" ]; then

  if [[ $CLANG_VERSION == 9 && $UBUNTU_VERSION == 18.04 ]]; then
    sudo apt-get update
    # gpg-agent is not available by default on 18.04
    sudo apt-get install  -y --no-install-recommends gpg-agent
    wget --no-check-certificate -O - https://apt.llvm.org/llvm-snapshot.gpg.key | sudo apt-key add  -
    apt-add-repository "deb http://apt.llvm.org/bionic/ llvm-toolchain-bionic-${CLANG_VERSION} main"
  elif [[ $UBUNTU_VERSION == 22.04 ]]; then
    # work around ubuntu apt-get conflicts
    sudo apt-get -y -f install
  fi

  sudo apt-get update
  apt-get install -y --no-install-recommends clang-"$CLANG_VERSION"
  apt-get install -y --no-install-recommends llvm-"$CLANG_VERSION"

  # Install dev version of LLVM.
  if [ -n "$LLVMDEV" ]; then
    sudo apt-get install -y --no-install-recommends llvm-"$CLANG_VERSION"-dev
  fi

  # Use update-alternatives to make this version the default
  update-alternatives --install /usr/bin/clang clang /usr/bin/clang-"$CLANG_VERSION" 50
  update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-"$CLANG_VERSION" 50
  # Override cc/c++ to clang as well
  update-alternatives --install /usr/bin/cc cc /usr/bin/clang 50
  update-alternatives --install /usr/bin/c++ c++ /usr/bin/clang++ 50

  # clang's packaging is a little messed up (the runtime libs aren't
  # added into the linker path), so give it a little help
  clang_lib=("/usr/lib/llvm-$CLANG_VERSION/lib/clang/"*"/lib/linux")
  echo "$clang_lib" > /etc/ld.so.conf.d/clang.conf
  ldconfig

  # Cleanup package manager
  apt-get autoclean && apt-get clean
  rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

fi
