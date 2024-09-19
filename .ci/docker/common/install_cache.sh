#!/bin/bash

set -ex

install_ubuntu() {
  echo "Preparing to build sccache from source"
  apt-get update
  # libssl-dev will not work as it is upgraded to libssl3 in Ubuntu-22.04.
  # Instead use lib and headers from OpenSSL1.1 installed in `install_openssl.sh``
  apt-get install -y cargo
  echo "Checking out sccache repo"
  git clone https://github.com/pytorch/sccache
  cd sccache
  echo "Building sccache"
  cargo build --release
  cp target/release/sccache /opt/cache/bin
  echo "Cleaning up"
  cd ..
  rm -rf sccache
  apt-get remove -y cargo rustc
  apt-get autoclean && apt-get clean
}

install_binary() {
  echo "Downloading sccache binary from S3 repo"
  curl --retry 3 https://s3.amazonaws.com/ossci-linux/sccache -o /opt/cache/bin/sccache
}

mkdir -p /opt/cache/bin
mkdir -p /opt/cache/lib
sed -e 's|PATH="\(.*\)"|PATH="/opt/cache/bin:\1"|g' -i /etc/environment
export PATH="/opt/cache/bin:$PATH"

# Setup compiler cache
if [ -n "$ROCM_VERSION" ]; then
  curl --retry 3 http://repo.radeon.com/misc/.sccache_amd/sccache -o /opt/cache/bin/sccache
else
  ID=$(grep -oP '(?<=^ID=).+' /etc/os-release | tr -d '"')
  # TODO: Install the pre-built binary from S3 as building from source
  # https://github.com/pytorch/sccache has started failing mysteriously
  # in which sccache server couldn't start with the following error:
  #   sccache: error: Invalid argument (os error 22)
  install_binary
fi
chmod a+x /opt/cache/bin/sccache

function write_sccache_stub() {
  # Unset LD_PRELOAD for ps because of asan + ps issues
  # https://gcc.gnu.org/bugzilla/show_bug.cgi?id=90589
  printf "#!/bin/sh\nif [ \$(env -u LD_PRELOAD ps -p \$PPID -o comm=) != sccache ]; then\n  exec sccache $(which $1) \"\$@\"\nelse\n  exec $(which $1) \"\$@\"\nfi" > "/opt/cache/bin/$1"
  chmod a+x "/opt/cache/bin/$1"
}

write_sccache_stub cc
write_sccache_stub c++
write_sccache_stub gcc
write_sccache_stub g++

# NOTE: See specific ROCM_VERSION case below.
if [ "x$ROCM_VERSION" = x ]; then
  write_sccache_stub clang
  write_sccache_stub clang++
fi

if [ -n "$CUDA_VERSION" ]; then
  # TODO: This is a workaround for the fact that PyTorch's FindCUDA
  # implementation cannot find nvcc if it is setup this way, because it
  # appears to search for the nvcc in PATH, and use its path to infer
  # where CUDA is installed.  Instead, we install an nvcc symlink outside
  # of the PATH, and set CUDA_NVCC_EXECUTABLE so that we make use of it.

  write_sccache_stub nvcc
  mv /opt/cache/bin/nvcc /opt/cache/lib/
fi

if [ -n "$ROCM_VERSION" ]; then
  # ROCm compiler is hcc or clang. However, it is commonly invoked via hipcc wrapper.
  # hipcc will call either hcc or clang using an absolute path starting with /opt/rocm,
  # causing the /opt/cache/bin to be skipped. We must create the sccache wrappers
  # directly under /opt/rocm while also preserving the original compiler names.
  # Note symlinks will chain as follows: [hcc or clang++] -> clang -> clang-??
  # Final link in symlink chain must point back to original directory.

  # Original compiler is moved one directory deeper. Wrapper replaces it.
  function write_sccache_stub_rocm() {
    OLDCOMP=$1
    COMPNAME=$(basename $OLDCOMP)
    TOPDIR=$(dirname $OLDCOMP)
    WRAPPED="$TOPDIR/original/$COMPNAME"
    mv "$OLDCOMP" "$WRAPPED"
    printf "#!/bin/sh\nexec sccache $WRAPPED \"\$@\"" > "$OLDCOMP"
    chmod a+x "$OLDCOMP"
  }

  if [[ -e "/opt/rocm/hcc/bin/hcc" ]]; then
    # ROCm 3.3 or earlier.
    mkdir /opt/rocm/hcc/bin/original
    write_sccache_stub_rocm /opt/rocm/hcc/bin/hcc
    write_sccache_stub_rocm /opt/rocm/hcc/bin/clang
    write_sccache_stub_rocm /opt/rocm/hcc/bin/clang++
    # Fix last link in symlink chain, clang points to versioned clang in prior dir
    pushd /opt/rocm/hcc/bin/original
    ln -s ../$(readlink clang)
    popd
  elif [[ -e "/opt/rocm/llvm/bin/clang" ]]; then
    # ROCm 3.5 and beyond.
    mkdir /opt/rocm/llvm/bin/original
    write_sccache_stub_rocm /opt/rocm/llvm/bin/clang
    write_sccache_stub_rocm /opt/rocm/llvm/bin/clang++
    # Fix last link in symlink chain, clang points to versioned clang in prior dir
    pushd /opt/rocm/llvm/bin/original
    ln -s ../$(readlink clang)
    popd
  else
    echo "Cannot find ROCm compiler."
    exit 1
  fi
fi
