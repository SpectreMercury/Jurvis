from __future__ import print_function
import torch

X = torch.rand(5, 3)
print(torch.cuda.is_available())
