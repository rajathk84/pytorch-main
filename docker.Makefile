DOCKER_REGISTRY          ?= docker.io
DOCKER_ORG               ?= $(shell docker info 2>/dev/null | sed '/Username:/!d;s/.* //')
DOCKER_IMAGE             ?= pytorch
DOCKER_FULL_NAME          = $(DOCKER_REGISTRY)/$(DOCKER_ORG)/$(DOCKER_IMAGE)

ifeq ("$(DOCKER_ORG)","")
$(warning WARNING: No docker user found using results from whoami)
DOCKER_ORG                = $(shell whoami)
endif

CUDA_VERSION_SHORT       ?= 12.1
CUDA_VERSION             ?= 12.1.1
CUDNN_VERSION            ?= 9
BASE_RUNTIME              = ubuntu:22.04
BASE_DEVEL                = nvidia/cuda:$(CUDA_VERSION)-devel-ubuntu22.04
CMAKE_VARS               ?=

# The conda channel to use to install cudatoolkit
CUDA_CHANNEL              = nvidia
# The conda channel to use to install pytorch / torchvision
INSTALL_CHANNEL          ?= whl

CUDA_PATH                ?= cpu
ifneq ("$(CUDA_VERSION_SHORT)","cpu")
CUDA_PATH                = cu$(subst .,,$(CUDA_VERSION_SHORT))
endif

PYTHON_VERSION           ?= 3.11
# Match versions that start with v followed by a number, to avoid matching with tags like ciflow
PYTORCH_VERSION          ?= $(shell git describe --tags --always --match "v[1-9]*.*")
# Can be either official / dev
BUILD_TYPE               ?= dev
BUILD_PROGRESS           ?= auto
# Intentionally left blank
TRITON_VERSION           ?=
BUILD_ARGS                = --build-arg BASE_IMAGE=$(BASE_IMAGE) \
							--build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
							--build-arg CUDA_VERSION=$(CUDA_VERSION) \
							--build-arg CUDA_PATH=$(CUDA_PATH) \
							--build-arg PYTORCH_VERSION=$(PYTORCH_VERSION) \
							--build-arg INSTALL_CHANNEL=$(INSTALL_CHANNEL) \
							--build-arg TRITON_VERSION=$(TRITON_VERSION) \
							--build-arg CMAKE_VARS="$(CMAKE_VARS)"
EXTRA_DOCKER_BUILD_FLAGS ?=

BUILD                    ?= build
# Intentionally left blank
PLATFORMS_FLAG           ?=
PUSH_FLAG                ?=
USE_BUILDX               ?=
BUILD_PLATFORMS          ?=
WITH_PUSH                ?= false
# Setup buildx flags
ifneq ("$(USE_BUILDX)","")
BUILD                     = buildx build
ifneq ("$(BUILD_PLATFORMS)","")
PLATFORMS_FLAG            = --platform="$(BUILD_PLATFORMS)"
endif
# Only set platforms flags if using buildx
ifeq ("$(WITH_PUSH)","true")
PUSH_FLAG                 = --push
endif
endif

DOCKER_BUILD              = docker $(BUILD) \
								--progress=$(BUILD_PROGRESS) \
								$(EXTRA_DOCKER_BUILD_FLAGS) \
								$(PLATFORMS_FLAG) \
								$(PUSH_FLAG) \
								--target $(BUILD_TYPE) \
								-t $(DOCKER_FULL_NAME):$(DOCKER_TAG) \
								$(BUILD_ARGS) .
DOCKER_PUSH               = docker push $(DOCKER_FULL_NAME):$(DOCKER_TAG)

.PHONY: all
all: devel-image

.PHONY: devel-image
devel-image: BASE_IMAGE := $(BASE_DEVEL)
devel-image: DOCKER_TAG := $(PYTORCH_VERSION)-cuda$(CUDA_VERSION_SHORT)-cudnn$(CUDNN_VERSION)-devel
devel-image:
	$(DOCKER_BUILD)

.PHONY: devel-push
devel-push: BASE_IMAGE := $(BASE_DEVEL)
devel-push: DOCKER_TAG := $(PYTORCH_VERSION)-cuda$(CUDA_VERSION_SHORT)-cudnn$(CUDNN_VERSION)-devel
devel-push:
	$(DOCKER_PUSH)

ifeq ("$(CUDA_VERSION_SHORT)","cpu")

.PHONY: runtime-image
runtime-image: BASE_IMAGE := $(BASE_RUNTIME)
runtime-image: DOCKER_TAG := $(PYTORCH_VERSION)-runtime
runtime-image:
	$(DOCKER_BUILD)

.PHONY: runtime-push
runtime-push: BASE_IMAGE := $(BASE_RUNTIME)
runtime-push: DOCKER_TAG := $(PYTORCH_VERSION)-runtime
runtime-push:
	$(DOCKER_PUSH)

else

.PHONY: runtime-image
runtime-image: BASE_IMAGE := $(BASE_RUNTIME)
runtime-image: DOCKER_TAG := $(PYTORCH_VERSION)-cuda$(CUDA_VERSION_SHORT)-cudnn$(CUDNN_VERSION)-runtime
runtime-image:
	$(DOCKER_BUILD)

.PHONY: runtime-push
runtime-push: BASE_IMAGE := $(BASE_RUNTIME)
runtime-push: DOCKER_TAG := $(PYTORCH_VERSION)-cuda$(CUDA_VERSION_SHORT)-cudnn$(CUDNN_VERSION)-runtime
runtime-push:
	$(DOCKER_PUSH)

endif

.PHONY: clean
clean:
	-docker rmi -f $(shell docker images -q $(DOCKER_FULL_NAME))
