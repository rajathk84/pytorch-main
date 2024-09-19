#!/bin/bash
set -ex -o pipefail

echo ""
echo "DIR: $(pwd)"
WORKSPACE=/Users/distiller/workspace
PROJ_ROOT=/Users/distiller/project
export TCLLIBPATH="/usr/local/lib"

# Install conda
curl --retry 3 -o ~/conda.sh https://repo.anaconda.com/miniconda/Miniconda3-py39_4.12.0-MacOSX-x86_64.sh
chmod +x ~/conda.sh
/bin/bash ~/conda.sh -b -p ~/anaconda
export PATH="~/anaconda/bin:${PATH}"
source ~/anaconda/bin/activate

# Install dependencies
conda install numpy ninja pyyaml mkl mkl-include setuptools cmake requests typing-extensions --yes
conda install -c conda-forge valgrind --yes
export CMAKE_PREFIX_PATH=${CONDA_PREFIX:-"$(dirname $(which conda))/../"}

# sync submodules
cd ${PROJ_ROOT}
git submodule sync
git submodule update --init --recursive

# run build script
chmod a+x ${PROJ_ROOT}/scripts/build_ios.sh
echo "########################################################"
cat ${PROJ_ROOT}/scripts/build_ios.sh
echo "########################################################"
echo "IOS_ARCH: ${IOS_ARCH}"
echo "IOS_PLATFORM: ${IOS_PLATFORM}"
echo "USE_PYTORCH_METAL: ${USE_PYTORCH_METAL}"
echo "USE_COREML_DELEGATE: ${USE_COREML_DELEGATE}"
export IOS_ARCH=${IOS_ARCH}
export IOS_PLATFORM=${IOS_PLATFORM}
export USE_PYTORCH_METAL=${USE_PYTORCH_METAL}
export USE_COREML_DELEGATE=${USE_COREML_DELEGATE}
unbuffer ${PROJ_ROOT}/scripts/build_ios.sh 2>&1 | ts

#store the binary
cd ${WORKSPACE}
DEST_DIR=${WORKSPACE}/ios
mkdir -p ${DEST_DIR}
cp -R ${PROJ_ROOT}/build_ios/install ${DEST_DIR}
mv ${DEST_DIR}/install ${DEST_DIR}/${IOS_ARCH}
