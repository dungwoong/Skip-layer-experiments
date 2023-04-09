'''ShuffleNetV2 in PyTorch.
See the paper "ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design" for more details.
'''

# https://github.com/kuangliu/pytorch-cifar/blob/master/models/shufflenetv2.py
import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F


class ShuffleBlock(nn.Module):
    def __init__(self, groups=2):
        super(ShuffleBlock, self).__init__()
        self.groups = groups

    def forward(self, x):
        '''Channel shuffle: [N,C,H,W] -> [N,g,C/g,H,W] -> [N,C/g,g,H,w] -> [N,C,H,W]'''
        N, C, H, W = x.size()
        g = self.groups
        return x.view(N, g, C // g, H, W).permute(0, 2, 1, 3, 4).reshape(N, C, H, W)


class SplitBlock(nn.Module):
    def __init__(self, ratio):
        super(SplitBlock, self).__init__()
        self.ratio = ratio

    def forward(self, x):
        c = int(x.size(1) * self.ratio)
        return x[:, :c, :, :], x[:, c:, :, :]


class BasicBlock(nn.Module):
    def __init__(self, in_channels, split_ratio=0.5):
        super(BasicBlock, self).__init__()
        self.split = SplitBlock(split_ratio)
        in_channels = int(in_channels * split_ratio)
        self.conv1 = nn.Conv2d(in_channels, in_channels,
                               kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(in_channels)
        self.conv2 = nn.Conv2d(in_channels, in_channels,
                               kernel_size=3, stride=1, padding=1, groups=in_channels, bias=False)
        self.bn2 = nn.BatchNorm2d(in_channels)
        self.conv3 = nn.Conv2d(in_channels, in_channels,
                               kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(in_channels)
        self.shuffle = ShuffleBlock()

    def forward(self, x):
        x1, x2 = self.split(x)
        out = F.relu(self.bn1(self.conv1(x2)))
        out = self.bn2(self.conv2(out))
        out = F.relu(self.bn3(self.conv3(out)))
        out = torch.cat([x1, out], 1)
        out = self.shuffle(out)
        return out


class DownBlock(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(DownBlock, self).__init__()
        mid_channels = out_channels // 2
        # left
        self.conv1 = nn.Conv2d(in_channels, in_channels,
                               kernel_size=3, stride=2, padding=1, groups=in_channels, bias=False)
        self.bn1 = nn.BatchNorm2d(in_channels)
        self.conv2 = nn.Conv2d(in_channels, mid_channels,
                               kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm2d(mid_channels)
        # right
        self.conv3 = nn.Conv2d(in_channels, mid_channels,
                               kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(mid_channels)
        self.conv4 = nn.Conv2d(mid_channels, mid_channels,
                               kernel_size=3, stride=2, padding=1, groups=mid_channels, bias=False)
        self.bn4 = nn.BatchNorm2d(mid_channels)
        self.conv5 = nn.Conv2d(mid_channels, mid_channels,
                               kernel_size=1, bias=False)
        self.bn5 = nn.BatchNorm2d(mid_channels)

        self.shuffle = ShuffleBlock()

    def forward(self, x):
        # left
        out1 = self.bn1(self.conv1(x))
        out1 = F.relu(self.bn2(self.conv2(out1)))
        # right
        out2 = F.relu(self.bn3(self.conv3(x)))
        out2 = self.bn4(self.conv4(out2))
        out2 = F.relu(self.bn5(self.conv5(out2)))
        # concat
        out = torch.cat([out1, out2], 1)
        out = self.shuffle(out)
        return out


class ShuffleNetV2(nn.Module):
    def __init__(self, net_size):
        super(ShuffleNetV2, self).__init__()
        out_channels = configs[net_size]['out_channels']
        num_blocks = configs[net_size]['num_blocks']

        self.conv1 = nn.Conv2d(3, 24, kernel_size=3,
                               stride=1, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(24)
        self.in_channels = 24
        self.layer1 = self._make_layer(out_channels[0], num_blocks[0])
        self.layer2 = self._make_layer(out_channels[1], num_blocks[1])
        self.layer3 = self._make_layer(out_channels[2], num_blocks[2])
        self.conv2 = nn.Conv2d(out_channels[2], out_channels[3],
                               kernel_size=1, stride=1, padding=0, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channels[3])
        self.linear = nn.Linear(out_channels[3], 10)

    def _make_layer(self, out_channels, num_blocks):
        layers = [DownBlock(self.in_channels, out_channels)]
        for i in range(num_blocks):
            layers.append(BasicBlock(out_channels))
            self.in_channels = out_channels
        return nn.Sequential(*layers)

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        # out = F.max_pool2d(out, 3, stride=2, padding=1)
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = F.relu(self.bn2(self.conv2(out)))
        out = F.avg_pool2d(out, 4)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out


class SEBlock(nn.Module):

    def __init__(self, oup, reduction):
        """

        :param oup: features
        :param reduction: reduction factor
        """
        super().__init__()
        mid_channels = oup // reduction
        self.se_block = nn.Sequential(nn.AdaptiveAvgPool2d(output_size=1),
                                      nn.Conv2d(oup, mid_channels, kernel_size=1, bias=True),
                                      # conv1x1 works with the matrix shape better
                                      nn.ReLU(),
                                      nn.Conv2d(mid_channels, oup, kernel_size=1, bias=True),
                                      nn.Sigmoid()
                                      )
        self.oup = oup
        self.mid_channels = mid_channels

        self.flops = None
        self._init_weights()

    def forward(self, x):
        w = self.se_block(x)
        return w * x

    def _init_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, mode='fan_out')
                if m.bias is not None:
                    init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                init.constant_(m.weight, 1)
                init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                init.normal_(m.weight, std=1e-3)
                if m.bias is not None:
                    init.constant_(m.bias, 0)


class ShuffleNetSE(ShuffleNetV2):
    def __init__(self, *args, **kwargs):
        self.stage = 2
        super().__init__(*args, **kwargs)

    def _make_layer(self, out_channels, num_blocks):
        reduction = 4 if self.stage == 2 else 8
        layers = [DownBlock(self.in_channels, out_channels),
                  SEBlock(out_channels, reduction)]
        for i in range(num_blocks):
            layers.append(BasicBlock(out_channels))
            layers.append(SEBlock(out_channels, reduction))
            self.in_channels = out_channels
        self.stage += 1
        return nn.Sequential(*layers)


class Swish(nn.Module):
    """
    Also known as SiLU --> sigmoid linear unit

    Applies sigmoid to itself then multiplies
    """

    def forward(self, feat):
        return feat * torch.sigmoid(feat)


class SLEBlock(nn.Module):
    """
    SLE block
    """

    def __init__(self, ch_in, ch_out):
        super().__init__()
        self.main = nn.Sequential(nn.AdaptiveAvgPool2d(4),
                                  # they used SiLU instead of LeakyReLU
                                  nn.Conv2d(ch_in, ch_out, 4, 1, 0, bias=False), Swish(),
                                  nn.Conv2d(ch_out, ch_out, 1, 1, 0, bias=False),
                                  nn.Sigmoid())
        self.ch_in, self.ch_out = ch_in, ch_out
        self.flops = None

    def forward(self, feat_small, feat_big):
        return feat_big * self.main(feat_small)


class ShuffleNetSLE(ShuffleNetV2):
    def __init__(self, net_size, *args, **kwargs):
        super().__init__(net_size, *args, **kwargs)
        out_channels = configs[net_size]['out_channels']
        self.sle_1 = SLEBlock(24, out_channels[0])  # maxpool and stage2
        self.sle_2 = SLEBlock(out_channels[0], out_channels[1])  # stage2 to stage3
        self.sle_3 = SLEBlock(out_channels[1], out_channels[2])  # stage3 to stage4

    def forward(self, x):
        c1 = F.relu(self.bn1(self.conv1(x)))
        # out = F.max_pool2d(out, 3, stride=2, padding=1)
        s2 = self.layer1(c1)
        s2 = self.sle_1(c1, s2)
        s3 = self.layer2(s2)
        s3 = self.sle_2(s2, s3)
        s4 = self.layer3(s3)
        s4 = self.sle_3(s3, s4)
        c5 = F.relu(self.bn2(self.conv2(s4)))
        out = F.avg_pool2d(c5, 4)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out


def init_params(net):
    """Init layer parameters."""
    for m in net.modules():
        if isinstance(m, nn.Conv2d):
            init.kaiming_normal_(m.weight, mode='fan_out')
            if m.bias is not None:
                init.constant_(m.bias, 0)
        elif isinstance(m, nn.BatchNorm2d):
            init.constant_(m.weight, 1)
            init.constant_(m.bias, 0)
        elif isinstance(m, nn.Linear):
            init.normal_(m.weight, std=1e-3)
            if m.bias is not None:
                init.constant_(m.bias, 0)


configs = {
    0.5: {
        'out_channels': (48, 96, 192, 1024),
        'num_blocks': (3, 7, 3)
    },

    1: {
        'out_channels': (116, 232, 464, 1024),
        'num_blocks': (3, 7, 3)
    },
    1.5: {
        'out_channels': (176, 352, 704, 1024),
        'num_blocks': (3, 7, 3)
    },
    2: {
        'out_channels': (224, 488, 976, 2048),
        'num_blocks': (3, 7, 3)
    }
}


def test(net):
    x = torch.randn(3, 3, 32, 32)
    y = net(x)
    print(y.shape)


if __name__ == "__main__":
    torch.cuda.empty_cache()
    mod = ShuffleNetSLE(net_size=0.5)
    init_params(mod)
    test(mod)
