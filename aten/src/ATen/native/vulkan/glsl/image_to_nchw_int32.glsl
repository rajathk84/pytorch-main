#version 450 core
// clang-format off
#define PRECISION ${PRECISION}
// clang-format on

layout(std430) buffer;

/*
 * Input Sampler
 */
layout(set = 0, binding = 0) uniform PRECISION isampler3D uImage;

/*
 * Output Buffer
 */
layout(set = 0, binding = 1) buffer PRECISION restrict writeonly Buffer {
  int data[];
}
uBuffer;

/*
 * Params Buffer
 */
layout(set = 0, binding = 2) uniform PRECISION restrict Block {
  // xyz contain the extents of the input texture, w contains HxW to help
  // calculate buffer offsets
  ivec4 in_extents;
}
uBlock;

/*
 * Local Work Group in_extents
 */
layout(local_size_x_id = 0, local_size_y_id = 1, local_size_z_id = 2) in;

void main() {
  const ivec3 pos = ivec3(gl_GlobalInvocationID);

  if (any(greaterThanEqual(pos, uBlock.in_extents.xyz))) {
    return;
  }

  const ivec4 intex = texelFetch(uImage, pos, 0);

  const int base_index =
      pos.x + uBlock.in_extents.x * pos.y + (4 * uBlock.in_extents.w) * pos.z;
  const ivec4 buf_indices =
      base_index + ivec4(0, 1, 2, 3) * uBlock.in_extents.w;

  uBuffer.data[buf_indices.x] = intex.x;
  uBuffer.data[buf_indices.y] = intex.y;
  uBuffer.data[buf_indices.z] = intex.z;
  uBuffer.data[buf_indices.w] = intex.w;
}
