#pragma once

namespace at::mps {

static const char * indexing_metal_shaders = R"INDEX_METAL(
#include <metal_stdlib>
#include <metal_atomic>

using namespace metal;

struct IndexAB {
    constant int64_t* indexArray;
};

template<typename T, typename OffsetsT>
kernel void index_select(
    constant IndexAB  * indexAB           [[buffer(0)]],
    constant void     * indexSizes        [[buffer(1)]],
    constant void     * indexStrides      [[buffer(2)]],
    constant OffsetsT * offsets           [[buffer(3)]],
    constant void     * inputData         [[buffer(4)]],
    device   void     * outputData        [[buffer(5)]],
    constant uint32_t & num_indices       [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]) {
    constant int64_t * index_sizes   = (constant int64_t *)indexSizes;
    constant int64_t * index_strides = (constant int64_t *)indexStrides;
    int64_t offset = 0;
    for (uint32_t i = 0; i < num_indices; i++) {
        constant int64_t* indexArray = indexAB[i].indexArray;
        int64_t index = indexArray[offsets[thread_index].z / sizeof(int64_t)];
        if (index < 0) {
            index += index_sizes[i];
        }
        offset += index * index_strides[i];
     }
    device T * out = (device T*)((device char*)outputData + offsets[thread_index].x);
    constant T * in  = (constant T*)((constant char*)inputData  + offsets[thread_index].y + offset);
    *out = *in;
}

template<typename T, typename OffsetsT>
void index_put_impl(
    constant IndexAB  * indexAB,
    constant int64_t  * index_sizes,
    constant int64_t  * index_strides,
    constant OffsetsT * offsets,
    constant void     * inputData,
    device   void     * outputData,
    constant uint32_t & num_indices,
    uint thread_index) {
    int64_t offset = 0;
    for (uint32_t i = 0; i < num_indices; i++) {
        constant int64_t* indexArray = indexAB[i].indexArray;
        int64_t index = indexArray[offsets[thread_index].z / sizeof(int64_t)];

        if (index < 0) {
            index += index_sizes[i];
        }
        offset += index * index_strides[i];
    }
    device T * out = (device T*)((device char*)outputData + offsets[thread_index].x + offset);
    constant T * in  = (constant T*)((constant char*)inputData  + offsets[thread_index].y);
    *out = *in;
}

template<typename T, typename OffsetsT>
kernel void index_put_serial(
    constant IndexAB  * indexAB           [[buffer(0)]],
    constant void     * indexSizes        [[buffer(1)]],
    constant void     * indexStrides      [[buffer(2)]],
    constant OffsetsT * offsets           [[buffer(3)]],
    constant void     * inputData         [[buffer(4)]],
    device   void     * outputData        [[buffer(5)]],
    constant uint32_t & num_indices       [[buffer(6)]],
    constant uint     * numIters          [[buffer(7)]],
    uint thread_index [[thread_position_in_grid]]) {

    constant int64_t * index_sizes   = (constant int64_t *)indexSizes;
    constant int64_t * index_strides = (constant int64_t *)indexStrides;

    for (uint iter_i = 0; iter_i < *numIters; iter_i++) {
        index_put_impl<T>(indexAB, index_sizes, index_strides, offsets, inputData, outputData, num_indices, iter_i);
    }
}

template<typename T, typename OffsetsT>
kernel void index_put(
    constant IndexAB  * indexAB           [[buffer(0)]],
    constant void     * indexSizes        [[buffer(1)]],
    constant void     * indexStrides      [[buffer(2)]],
    constant OffsetsT * offsets           [[buffer(3)]],
    constant void     * inputData         [[buffer(4)]],
    device   void     * outputData        [[buffer(5)]],
    constant uint32_t & num_indices       [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]) {

    constant int64_t * index_sizes   = (constant int64_t *)indexSizes;
    constant int64_t * index_strides = (constant int64_t *)indexStrides;
    index_put_impl<T>(indexAB, index_sizes, index_strides, offsets, inputData, outputData, num_indices, thread_index);
}

#define REGISTER_INDEX_OP(DTYPE_SIZE, IDX_SIZE, DTYPE, INDEX_OP_TYPE, IDX_DTYPE)   \
template                                                                           \
[[host_name("index_" #INDEX_OP_TYPE "_" #DTYPE_SIZE "_" #IDX_SIZE)]]               \
kernel void index_ ## INDEX_OP_TYPE<DTYPE, IDX_DTYPE>(                             \
    constant IndexAB * indexAB           [[buffer(0)]],                            \
    constant void    * indexSizes        [[buffer(1)]],                            \
    constant void    * indexStrides      [[buffer(2)]],                            \
    constant IDX_DTYPE   * offsets           [[buffer(3)]],                        \
    constant void    * inputData         [[buffer(4)]],                            \
    device   void    * outputData        [[buffer(5)]],                            \
    constant uint32_t & num_indices      [[buffer(6)]],                            \
    uint thread_index [[thread_position_in_grid]]);

#define REGISTER_INDEX_OP_ALL_DTYPES(INDEX_OP_TYPE)     \
    REGISTER_INDEX_OP(8bit,  idx32, char,  INDEX_OP_TYPE, uint3);     \
    REGISTER_INDEX_OP(8bit,  idx64, char,  INDEX_OP_TYPE, ulong3);    \
    REGISTER_INDEX_OP(16bit, idx32, short, INDEX_OP_TYPE, uint3);     \
    REGISTER_INDEX_OP(16bit, idx64, short, INDEX_OP_TYPE, ulong3);    \
    REGISTER_INDEX_OP(32bit, idx32, int,   INDEX_OP_TYPE, uint3);     \
    REGISTER_INDEX_OP(32bit, idx64, int,   INDEX_OP_TYPE, ulong3);    \
    REGISTER_INDEX_OP(64bit, idx32, long,  INDEX_OP_TYPE, uint3);     \
    REGISTER_INDEX_OP(64bit, idx64, long,  INDEX_OP_TYPE, ulong3);

REGISTER_INDEX_OP_ALL_DTYPES(select);
REGISTER_INDEX_OP_ALL_DTYPES(put);

#define REGISTER_SINGLE_THREADED_INDEX_OP(DTYPE_SIZE, IDX_SIZE, DTYPE, INDEX_OP_TYPE, IDX_DTYPE)   \
template                                                                                           \
[[host_name("index_" #INDEX_OP_TYPE "_" #DTYPE_SIZE "_" #IDX_SIZE)]]                               \
kernel void index_ ## INDEX_OP_TYPE<DTYPE, IDX_DTYPE>(                                             \
    constant IndexAB   * indexAB           [[buffer(0)]],                                          \
    constant void      * indexSizes        [[buffer(1)]],                                          \
    constant void      * indexStrides      [[buffer(2)]],                                          \
    constant IDX_DTYPE * offsets           [[buffer(3)]],                                          \
    constant void      * inputData         [[buffer(4)]],                                          \
    device   void      * outputData        [[buffer(5)]],                                          \
    constant uint32_t  & num_indices       [[buffer(6)]],                                          \
    constant uint      * numIters          [[buffer(7)]],                                          \
    uint thread_index [[thread_position_in_grid]]);

#define REGISTER_SINGLE_THREADED_INDEX_OP_ALL_DTYPES(INDEX_OP_TYPE)                   \
    REGISTER_SINGLE_THREADED_INDEX_OP(8bit,  idx32, char,  INDEX_OP_TYPE, uint3);     \
    REGISTER_SINGLE_THREADED_INDEX_OP(8bit,  idx64, char,  INDEX_OP_TYPE, ulong3);    \
    REGISTER_SINGLE_THREADED_INDEX_OP(16bit, idx32, short, INDEX_OP_TYPE, uint3);     \
    REGISTER_SINGLE_THREADED_INDEX_OP(16bit, idx64, short, INDEX_OP_TYPE, ulong3);    \
    REGISTER_SINGLE_THREADED_INDEX_OP(32bit, idx32, int,   INDEX_OP_TYPE, uint3);     \
    REGISTER_SINGLE_THREADED_INDEX_OP(32bit, idx64, int,   INDEX_OP_TYPE, ulong3);    \
    REGISTER_SINGLE_THREADED_INDEX_OP(64bit, idx32, long,  INDEX_OP_TYPE, uint3);     \
    REGISTER_SINGLE_THREADED_INDEX_OP(64bit, idx64, long,  INDEX_OP_TYPE, ulong3);

REGISTER_SINGLE_THREADED_INDEX_OP_ALL_DTYPES(put_serial);

template<typename StridesT, typename DataT>
kernel void kernel_index_offsets(constant StridesT * strides         [[buffer(0)]],
                                device DataT      * data_offsets    [[buffer(1)]],
                                constant uint     * iter_shape      [[buffer(2)]],
                                constant uint     & num_dimensions  [[buffer(3)]],
                                uint thread_index [[thread_position_in_grid]]) {
    data_offsets[thread_index] = 0;
    uint32_t idx = thread_index;
    for (uint32_t dim = 0; dim < num_dimensions; dim++) {
        uint32_t remainder = idx % iter_shape[dim];
        idx /= iter_shape[dim];

        data_offsets[thread_index] += remainder * DataT(strides[dim]);
    }
}

template
[[host_name("kernel_index_offsets_32")]]
kernel void kernel_index_offsets<packed_uint3, uint3>(
                constant packed_uint3 * strides         [[buffer(0)]],
                device uint3          * data_offsets    [[buffer(1)]],
                constant uint         * iter_shape      [[buffer(2)]],
                constant uint         & num_dimensions  [[buffer(3)]],
                uint thread_index [[thread_position_in_grid]]);

template
[[host_name("kernel_index_offsets_64")]]
kernel void kernel_index_offsets<packed_uint3, ulong3>(
                constant packed_uint3 * strides         [[buffer(0)]],
                device ulong3          * data_offsets    [[buffer(1)]],
                constant uint         * iter_shape      [[buffer(2)]],
                constant uint         & num_dimensions  [[buffer(3)]],
                uint thread_index [[thread_position_in_grid]]);

template<typename T, typename E, typename OffsetsT>
kernel void index_put_accumulate_native_dtypes(
    constant IndexAB  * indexAB     [[buffer(0)]],
    constant void     * indexSizes   [[buffer(1)]],
    constant void     * indexStrides [[buffer(2)]],
    constant OffsetsT * offsets      [[buffer(3)]],
    constant void     * inputData    [[buffer(4)]],
    device void       * outputData   [[buffer(5)]],
    constant uint32_t & num_indices  [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]) {
    constant int64_t * index_sizes   = (constant int64_t *)indexSizes;
    constant int64_t * index_strides = (constant int64_t *)indexStrides;
    int64_t offset = 0;
    for (uint32_t i = 0; i < num_indices; i++) {
        constant int64_t* indexArray = indexAB[i].indexArray;
        int64_t index = indexArray[offsets[thread_index].z / sizeof(int64_t)];
        if (index < 0) {
            index += index_sizes[i];
        }
        offset += index * index_strides[i];
    }
    device T * out = (device T*)((device char*)outputData + offsets[thread_index].x + offset);
    constant E * in  = (constant E*)((constant char*)inputData  + offsets[thread_index].y);
    atomic_fetch_add_explicit(out, *in, memory_order_relaxed);
}

template<typename T>
__attribute__((__always_inline__)) void atomic_fetch_add_relaxed(device void * addr, T value) {
    device atomic_uint* uintAddr = (device atomic_uint*)addr;
    uint expected = atomic_load_explicit(uintAddr, memory_order_relaxed);
    T updated = as_type<T>(expected) + value;
    while (!atomic_compare_exchange_weak_explicit(uintAddr, &expected, as_type<uint>(updated), memory_order_relaxed, memory_order_relaxed)) {
        updated = as_type<T>(expected) + value;
    }
}

template<typename T, typename OffsetsT>
kernel void atomic_index_put_accumulate(
    constant IndexAB  * indexAB           [[buffer(0)]],
    constant void     * indexSizes        [[buffer(1)]],
    constant void     * indexStrides      [[buffer(2)]],
    constant OffsetsT * offsets           [[buffer(3)]],
    constant void     * inputData         [[buffer(4)]],
    device   void     * outputData        [[buffer(5)]],
    constant uint32_t & num_indices       [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]) {
    constant int64_t * index_sizes   = (constant int64_t *)indexSizes;
    constant int64_t * index_strides = (constant int64_t *)indexStrides;
    int64_t offset = 0;
    for (uint32_t i = 0; i < num_indices; i++) {
        constant int64_t* indexArray = indexAB[i].indexArray;
        int64_t index = indexArray[offsets[thread_index].z / sizeof(int64_t)];
        if (index < 0) {
            index += index_sizes[i];
        }
        offset += index * index_strides[i];
    }
    device void * out = (device void*)((device char*)outputData + offsets[thread_index].x + offset);
    constant T  * in  = (constant T*)((constant char*)inputData + offsets[thread_index].y);
    atomic_fetch_add_relaxed<T>(out, *in);
}

template
[[host_name("index_put_accumulate_32bit_float_idx32")]]
kernel void atomic_index_put_accumulate<float, uint3>(
    constant IndexAB  * indexAB     [[buffer(0)]],
    constant void     * indexSizes   [[buffer(1)]],
    constant void     * indexStrides [[buffer(2)]],
    constant uint3    * offsets      [[buffer(3)]],
    constant void     * inputData    [[buffer(4)]],
    device   void     * outputData   [[buffer(5)]],
    constant uint32_t & num_indices  [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]);

template
[[host_name("index_put_accumulate_32bit_float_idx64")]]
kernel void atomic_index_put_accumulate<float, ulong3>(
    constant IndexAB  * indexAB     [[buffer(0)]],
    constant void     * indexSizes   [[buffer(1)]],
    constant void     * indexStrides [[buffer(2)]],
    constant ulong3   * offsets      [[buffer(3)]],
    constant void     * inputData    [[buffer(4)]],
    device   void     * outputData   [[buffer(5)]],
    constant uint32_t & num_indices  [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]);

template
[[host_name("index_put_accumulate_32bit_int_idx32")]]
kernel void index_put_accumulate_native_dtypes<atomic_int, int, uint3>(
    constant IndexAB  * indexAB     [[buffer(0)]],
    constant void     * indexSizes   [[buffer(1)]],
    constant void     * indexStrides [[buffer(2)]],
    constant uint3    * offsets      [[buffer(3)]],
    constant void     * inputData    [[buffer(4)]],
    device   void     * outputData   [[buffer(5)]],
    constant uint32_t & num_indices [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]);

template
[[host_name("index_put_accumulate_32bit_int_idx64")]]
kernel void index_put_accumulate_native_dtypes<atomic_int, int, ulong3>(
    constant IndexAB  * indexAB     [[buffer(0)]],
    constant void     * indexSizes   [[buffer(1)]],
    constant void     * indexStrides [[buffer(2)]],
    constant ulong3   * offsets      [[buffer(3)]],
    constant void     * inputData    [[buffer(4)]],
    device   void     * outputData   [[buffer(5)]],
    constant uint32_t & num_indices [[buffer(6)]],
    uint thread_index [[thread_position_in_grid]]);
)INDEX_METAL";

static const char *SCATTER_OPS_TEMPLATE = R"METAL_SCATTER(
struct __attribute__ ((packed)) packed_uint5{{
  uint32_t x; uint32_t y; uint32_t z; uint32_t w; uint32_t u;
}};

template<typename Y, typename X>
Y cast(const X x);

template<>
{1} cast<{1}, {0}>(const {0} x) {{
 return {2};
}}

kernel void scatter_kernel_5(uint linear_index              [[thread_position_in_grid]],
                             constant void * src_           [[buffer(0)]],
                             device void * dst_             [[buffer(1)]],
                             constant packed_uint5 & size   [[buffer(2)]],
                             constant packed_uint5 & stride [[buffer(3)]],
                             constant uint32_t & numel      [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint5 local_index;
    local_index.x = linear_index / (size.u * size.w * size.z * size.y) % size.x;
    local_index.y = linear_index / (size.u * size.w * size.z) % size.y;
    local_index.z = linear_index / (size.u * size.w) % size.z;
    local_index.w = linear_index / size.u % size.w;
    local_index.u = linear_index % size.u;

    packed_uint5 strided_index;
    strided_index.x = local_index.x * stride.x;
    strided_index.y = local_index.y * stride.y;
    strided_index.z = local_index.z * stride.z;
    strided_index.w = local_index.w * stride.w;
    strided_index.u = local_index.u * stride.u;

    dst[strided_index.x + strided_index.y + strided_index.z + strided_index.w + strided_index.u] = cast<{1}>(src[linear_index]);
}}

kernel void scatter_kernel_4(uint linear_index              [[thread_position_in_grid]],
                             constant void * src_           [[buffer(0)]],
                             device void * dst_             [[buffer(1)]],
                             constant packed_uint4 & size   [[buffer(2)]],
                             constant packed_uint4 & stride [[buffer(3)]],
                             constant uint32_t & numel      [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint4 local_index;
    local_index.x = linear_index / (size[3] * size[2] * size[1]) % size[0];
    local_index.y = linear_index / (size[3] * size[2]) % size[1];
    local_index.z = linear_index / size[3] % size[2];
    local_index.w = linear_index % size[3];

    const packed_uint4 strided_index = local_index * stride;
    dst[strided_index.x + strided_index.y + strided_index.z + strided_index.w] = cast<{1}>(src[linear_index]);
}}

kernel void scatter_kernel_3(uint linear_index              [[thread_position_in_grid]],
                             constant void * src_           [[buffer(0)]],
                             device void * dst_             [[buffer(1)]],
                             constant packed_uint3 & size   [[buffer(2)]],
                             constant packed_uint3 & stride [[buffer(3)]],
                             constant uint32_t & numel      [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint3 local_index;
    local_index.x = linear_index / (size[2] * size[1]) % size[0];
    local_index.y = linear_index / size[2] % size[1];
    local_index.z = linear_index % size[2];

    const packed_uint3 strided_index = local_index * stride;
    dst[strided_index.x + strided_index.y + strided_index.z] = cast<{1}>(src[linear_index]);
}}

kernel void scatter_kernel_2(uint linear_index              [[thread_position_in_grid]],
                             constant void * src_           [[buffer(0)]],
                             device void * dst_             [[buffer(1)]],
                             constant packed_uint2 & size   [[buffer(2)]],
                             constant packed_uint2 & stride [[buffer(3)]],
                             constant uint32_t & numel      [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint2 local_index;
    local_index.x = linear_index / size[1] % size[0];
    local_index.y = linear_index % size[1];

    const packed_uint2 strided_index = local_index * stride;
    dst[strided_index.x + strided_index.y] = cast<{1}>(src[linear_index]);
}}

kernel void scatter_kernel_1(uint linear_index              [[thread_position_in_grid]],
                             constant void * src_           [[buffer(0)]],
                             device void * dst_             [[buffer(1)]],
                             constant int & size            [[buffer(2)]],
                             constant int & stride          [[buffer(3)]],
                             constant uint32_t & numel      [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    const int local_index = linear_index % size;
    const int strided_index = local_index * stride;
    dst[strided_index] = cast<{1}>(src[linear_index]);
}}
)METAL_SCATTER";

static const char *GATHER_OPS_TEMPLATE = R"METAL_GATHER(
struct __attribute__ ((packed)) packed_uint5{{
  uint32_t x; uint32_t y; uint32_t z; uint32_t w; uint32_t u;
}};

template<typename Y, typename X>
Y cast(const X x);

template<>
{1} cast<{1}, {0}>(const {0} x) {{
 return {2};
}}

kernel void gather_kernel_5(uint linear_index               [[thread_position_in_grid]],
                            constant void * src_            [[buffer(0)]],
                            device void * dst_              [[buffer(1)]],
                            constant packed_uint5 & size    [[buffer(2)]],
                            constant packed_uint5 & stride  [[buffer(3)]],
                            constant uint32_t & numel       [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;


    packed_uint5 local_index;
    local_index.x = linear_index / (size.u * size.w * size.z * size.y) % size.x;
    local_index.y = linear_index / (size.u * size.w * size.z) % size.y;
    local_index.z = linear_index / (size.u * size.w) % size.z;
    local_index.w = linear_index / size.u % size.w;
    local_index.u = linear_index % size.u;

    packed_uint5 strided_index;
    strided_index.x = local_index.x * stride.x;
    strided_index.y = local_index.y * stride.y;
    strided_index.z = local_index.z * stride.z;
    strided_index.w = local_index.w * stride.w;
    strided_index.u = local_index.u * stride.u;

    dst[linear_index] = cast<{1}>(src[strided_index.x + strided_index.y + strided_index.z + strided_index.w + strided_index.u]);
}}

kernel void gather_kernel_4(uint linear_index               [[thread_position_in_grid]],
                            constant void * src_            [[buffer(0)]],
                            device void * dst_              [[buffer(1)]],
                            constant packed_uint4 & size    [[buffer(2)]],
                            constant packed_uint4 & stride  [[buffer(3)]],
                            constant uint32_t & numel       [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint4 local_index;
    local_index.x = linear_index / (size[3] * size[2] * size[1]) % size[0];
    local_index.y = linear_index / (size[3] * size[2]) % size[1];
    local_index.z = linear_index / size[3] % size[2];
    local_index.w = linear_index % size[3];

    const packed_uint4 strided_index = local_index * stride;
    dst[linear_index] = cast<{1}>(src[strided_index.x + strided_index.y + strided_index.z + strided_index.w]);
}}

kernel void gather_kernel_3(uint linear_index               [[thread_position_in_grid]],
                            constant void * src_            [[buffer(0)]],
                            device void * dst_              [[buffer(1)]],
                            constant packed_uint3 & size    [[buffer(2)]],
                            constant packed_uint3 & stride  [[buffer(3)]],
                            constant uint32_t & numel       [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint3 local_index;
    local_index.x = linear_index / (size[2] * size[1]) % size[0];
    local_index.y = linear_index / size[2] % size[1];
    local_index.z = linear_index % size[2];

    const packed_uint3 strided_index = local_index * stride;
    dst[linear_index] = cast<{1}>(src[strided_index.x + strided_index.y + strided_index.z]);
}}

kernel void gather_kernel_2(uint linear_index               [[thread_position_in_grid]],
                            constant void * src_            [[buffer(0)]],
                            device void * dst_              [[buffer(1)]],
                            constant packed_uint2 & size    [[buffer(2)]],
                            constant packed_uint2 & stride  [[buffer(3)]],
                            constant uint32_t & numel       [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    packed_uint2 local_index;
    local_index.x = linear_index / size[1] % size[0];
    local_index.y = linear_index % size[1];

    const packed_uint2 strided_index = local_index * stride;
    dst[linear_index] = cast<{1}>(src[strided_index.x + strided_index.y]);
}}

kernel void gather_kernel_1(uint linear_index               [[thread_position_in_grid]],
                            constant void * src_            [[buffer(0)]],
                            device void * dst_              [[buffer(1)]],
                            constant int & size             [[buffer(2)]],
                            constant int & stride           [[buffer(3)]],
                            constant uint32_t & numel       [[buffer(4)]]) {{
    if (linear_index >= numel) return;

    constant {0} * src = (constant {0} *)src_;
    device {1} * dst = (device {1} *)dst_;

    const int local_index = linear_index % size;
    const int strided_index = local_index * stride;
    dst[linear_index] = cast<{1}>(src[strided_index]);
}}
)METAL_GATHER";
} // namespace at::mps
