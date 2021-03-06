import torch
import torch.nn as nn
import numpy as np
import scipy.misc
import matplotlib.pyplot as plt
import torch.nn.functional as F


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        weight_shape = list(m.weight.data.size())
        fan_in = np.prod(weight_shape[1:4])
        fan_out = np.prod(weight_shape[2:4]) * weight_shape[0]
        w_bound = np.sqrt(6. / (fan_in + fan_out))
        m.weight.data.uniform_(-w_bound, w_bound)
        m.bias.data.fill_(0)
    elif classname.find('Linear') != -1:
        weight_shape = list(m.weight.data.size())
        fan_in = weight_shape[1]
        fan_out = weight_shape[0]
        w_bound = np.sqrt(6. / (fan_in + fan_out))
        m.weight.data.uniform_(-w_bound, w_bound)
        m.bias.data.fill_(0)


class StreetviewImageModule(torch.nn.Module):

    def __init__(self, image_emb_size, input_num_channels,
                 image_height, image_width, using_recurrence=False):
        super(StreetviewImageModule, self).__init__()

        self.input_num_channels = input_num_channels
        self.input_dims = (input_num_channels, image_height, image_width)
        self.conv1 = nn.Conv2d(input_num_channels, 1024, kernel_size=3, stride=1, padding=1)
        self.conv2 = nn.Conv2d(1024, 512, kernel_size=3, stride=1, padding=1)
        self.conv3 = nn.Conv2d(512, 256, kernel_size=3, stride=1, padding=1)
        self.using_recurrence = using_recurrence
        self.global_id = 0

    def init_weights(self):
        self.apply(weights_init)

    def fix(self):
        parameters = self.parameters()
        for parameter in parameters:
            parameter.require_grad = False

    def forward(self, x):
        b, n, c, h, w = x.size()
        if self.using_recurrence:
            x = x.view(b * n, 1, c, h, w)
            num_batches = b * n
            num_channels = self.input_num_channels
        else:
            num_batches = b
            num_channels = n * c
        x = x.view(num_batches, num_channels, h, w)

        x = F.relu(self.conv1(x))
        x = F.relu(self.conv2(x))
        x_image_rep = F.relu(self.conv3(x))

        if self.using_recurrence:
            x_image_rep = x_image_rep.view(b, n, x_image_rep.size(1), x_image_rep.size(2), x_image_rep.size(3))
        else:
            x_image_rep = x_image_rep.view(b, x_image_rep.size(1), x_image_rep.size(2), x_image_rep.size(3))
        return x_image_rep
