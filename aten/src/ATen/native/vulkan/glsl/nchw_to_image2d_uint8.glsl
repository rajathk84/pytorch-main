#version 450 core
#define PRECISION ${PRECISION}
#define FORMAT ${FORMAT}

layout(std430) buffer;

/*
 * Output Image
 */
layout(set = 0, binding = 0, rgba8ui) uniform PRECISION restrict writeonly uimage2D uImage;

/*
 * Input Buffer
 */
layout(set = 0, binding = 1) buffer  PRECISION restrict readonly Buffer {
  uint data[];
}
uBuffer;

/*
 * Params Buffer
 */
layout(set = 0, binding = 2) uniform PRECISION restrict Block {
  // xyz contain the extents of the output texture, w contains HxW to help
  // calculate buffer offsets
  ivec4 out_extents;
}
uBlock;

/*
 * Local Work Group Size
 */
layout(local_size_x_id = 0, local_size_y_id = 1, local_size_z_id = 2) in;

void main() {
  const ivec3 pos = ivec3(gl_GlobalInvocationID);

  if (any(greaterThanEqual(pos, uBlock.out_extents.xyz))) {
    return;
  }

  const int base_index =
      pos.x + uBlock.out_extents.x * pos.y + (4 * uBlock.out_extents.w) * pos.z;
  const ivec4 buf_indices =
      base_index + ivec4(0, 1, 2, 3) * uBlock.out_extents.w;

  int shift = (1 << 8) - 1;
  ivec4 masks;
  masks.x = shift << 8 * (buf_indices.x % 4);
  masks.y = shift << 8 * (buf_indices.y % 4);
  masks.z = shift << 8 * (buf_indices.z % 4);
  masks.w = shift << 8 * (buf_indices.w % 4);

  uint buf_in_1 = uBuffer.data[buf_indices.x / 4];
  uint a_v = (buf_in_1 & masks.x) >> 8 * (buf_indices.x % 4);

  uint buf_in_2 = uBuffer.data[buf_indices.y / 4];
  uint b_v = (buf_in_2 & masks.y) >> 8 * (buf_indices.y % 4);

  uint buf_in_3 = uBuffer.data[buf_indices.z / 4];
  uint g_v = (buf_in_3 & masks.z) >> 8 * (buf_indices.z % 4);

  uint buf_in_4 = uBuffer.data[buf_indices.w / 4];
  uint r_v = (buf_in_4 & masks.w) >> 8 * (buf_indices.w % 4);

  imageStore(uImage, pos.xy, uvec4(a_v, b_v, g_v, r_v));
}
