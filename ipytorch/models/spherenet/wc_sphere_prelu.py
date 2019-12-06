# this code is writen by liujing
import math
import torch
import torch.nn as nn
import torch.nn.init as init
from .margin_linear import MarginLinear
import prune

__all__ = ["wcSphereNet"]


def conv3x3(in_planes, out_planes, stride=1, rate=0.):
    """3x3 convolution with padding"""
    return prune.wcConv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, rate=rate)


class wcSphereBlock(nn.Module):

    def __init__(self, planes, rate=0.):
        super(wcSphereBlock, self).__init__()
        self.rate = rate
        self.conv1 = conv3x3(planes, planes, rate=self.rate)
        self.relu = nn.PReLU(planes)
        self.conv2 = conv3x3(planes, planes, rate=self.rate)

        self._init_weight()

    def _init_weight(self):
        # init conv1
        init.normal(self.conv1.weight, std=0.01)
        init.constant(self.conv1.bias, 0)
        # init conv2
        init.normal(self.conv2.weight, std=0.01)
        init.constant(self.conv2.bias, 0)

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.relu(out)

        out += residual
        return out


class wcSphereNet(nn.Module):
    """SphereNet class

    Note: Input must be 112x96
    """

    def __init__(self, depth, num_output=10572, num_features=512, margin_inner_product_type='quadruple', rate=0.):
        super(wcSphereNet, self).__init__()
        if depth == 4:
            layers = [0, 0, 0, 0]
        elif depth == 10:
            layers = [0, 1, 2, 0]
        elif depth == 20:
            layers = [1, 2, 4, 1]
        elif depth == 38:
            layers = [2, 4, 8, 2]
        elif depth == 64:
            layers = [3, 8, 16, 3]
        else:
            assert False, "invalid depth: %d, only support: 4, 10, 20, 38, 64" % depth

        self.depth = depth
        block = wcSphereBlock
        self.rate = rate
        # define network structure
        self.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=2, padding=1)
        self.relu1 = nn.PReLU(64)
        self.layer1 = self._make_layer(block, 64, layers[0])

        self.conv2 = prune.wcConv2d(64, 128, kernel_size=3, stride=2, padding=1, rate=self.rate)
        self.relu2 = nn.PReLU(128)
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        
        self.conv3 = prune.wcConv2d(128, 256, kernel_size=3, stride=2, padding=1, rate=self.rate)
        self.relu3 = nn.PReLU(256)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        
        self.conv4 = prune.wcConv2d(256, 256, kernel_size=3, stride=2, padding=1, rate=self.rate)
        self.relu4 = nn.PReLU(256)
        self.layer4 = self._make_layer(block, 256, layers[3], stride=2)
        
        # self.fc = prune.wcLinear(256*7*6, num_features, rate=0.1)
        self.fc = nn.Linear(256*7*6, num_features)

        self.margin_inner_product_type = margin_inner_product_type
        if margin_inner_product_type == 'single':
            margin_inner_product_type = 1
        elif margin_inner_product_type == 'double':
            margin_inner_product_type = 2
        elif margin_inner_product_type == 'triple':
            margin_inner_product_type = 3
        elif margin_inner_product_type == 'quadruple':
            margin_inner_product_type = 4
        else:
            print('Unknown margin type.')
        self.margin_linear = MarginLinear(num_output=num_output,
                                          num_features=num_features,
                                          margin_inner_product_type=margin_inner_product_type)
        self._init_weight()

    def _init_weight(self):
        # init conv1
        init.xavier_normal(self.conv1.weight)
        # init.kaiming_normal(self.conv1.weight)
        init.constant(self.conv1.bias, 0)
        # init conv2
        init.xavier_normal(self.conv2.weight)
        # init.kaiming_normal(self.conv2.weight)
        init.constant(self.conv2.bias, 0)
        # init conv3
        init.xavier_normal(self.conv3.weight)
        # init.kaiming_normal(self.conv3.weight)
        init.constant(self.conv3.bias, 0)
        # init conv4
        init.xavier_normal(self.conv4.weight)
        # init.kaiming_normal(self.conv4.weight)
        init.constant(self.conv4.bias, 0)
        # init fc
        init.xavier_normal(self.fc.weight)
        init.constant(self.fc.bias, 0)

    def _make_layer(self, block, planes, blocks, stride=1):
        layers = []
        for i in range(blocks):
            layers.append(block(planes, rate=self.rate))
        return nn.Sequential(*layers)

    def forward(self, x, target=None):
        x = self.conv1(x)
        x = self.relu1(x)
        x = self.layer1(x)

        x = self.conv2(x)
        x = self.relu2(x)
        x = self.layer2(x)

        x = self.conv3(x)
        x = self.relu3(x)
        x = self.layer3(x)

        x = self.conv4(x)
        x = self.relu4(x)
        x = self.layer4(x)

        x = x.view(x.size(0), -1)
        x = self.fc(x)

        if target is not None:
            x = self.margin_linear(x, target)
        return x
