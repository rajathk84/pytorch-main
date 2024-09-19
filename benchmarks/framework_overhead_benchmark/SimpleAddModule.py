from utils import NUM_LOOP_ITERS

import torch


def add_tensors_loop(x, y):
    z = torch.add(x, y)
    for i in range(NUM_LOOP_ITERS):
        z = torch.add(z, x)
    return z


class SimpleAddModule(torch.nn.Module):
    def __init__(self, add_op):
        super().__init__()
        self.add_op = add_op

    def forward(self, x, y):
        return self.add_op(x, y)
