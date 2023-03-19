# Shufflenet https://github.com/pytorch/vision/blob/main/torchvision/models/shufflenetv2.py
# FastGAN https://github.com/odegeasslbc/FastGAN-pytorch/blob/main/models.py

from functools import partial
from typing import Any, Callable, List, Optional

import torch
import torch.nn as nn
from torch import Tensor


def conv2d(*args, **kwargs):
    # The FastGAN paper used spectral normalization, but I don't think we need that for a convnet.
    # return spectral_norm(nn.Conv2d(*args, **kwargs))
    return nn.Conv2d(*args, **kwargs)


class Swish(nn.Module):
    """
    Also known as SiLU --> sigmoid linear unit

    Applies sigmoid to itself then multiplies
    """

    def forward(self, feat):
        return feat * torch.sigmoid(feat)


class SEBlock(nn.Module):
    """
    SLE block
    """

    def __init__(self, ch_in, ch_out):
        super().__init__()
        self.main = nn.Sequential(nn.AdaptiveAvgPool2d(4),
                                  # they used SiLU instead of LeakyReLU
                                  conv2d(ch_in, ch_out, 4, 1, 0, bias=False), Swish(),
                                  conv2d(ch_out, ch_out, 1, 1, 0, bias=False), nn.Sigmoid())

    def forward(self, feat_small, feat_big):
        return feat_big * self.main(feat_small)


# SHUFFLENET #############################################################################################

def channel_shuffle(x: Tensor, groups: int) -> Tensor:
    """
    Channel shuffle operation
    :param x: The tensor to shuffle (B C H W)
    :param groups: The number of groups to shuffle between
    :return: channel shuffled tensor, of same size as the original.
    """
    batchsize, num_channels, height, width = x.size()
    channels_per_group = num_channels // groups

    # reshape
    x = x.view(batchsize, groups, channels_per_group, height, width)

    x = torch.transpose(x, 1, 2).contiguous()

    # flatten
    x = x.view(batchsize, -1, height, width)

    return x


class InvertedResidual(nn.Module):
    """
    A residual block used in ShufflenetV2.
    """

    def __init__(self, inp: int, oup: int, stride: int) -> None:
        super().__init__()

        if not (1 <= stride <= 3):
            raise ValueError("illegal stride value")
        self.stride = stride

        branch_features = oup // 2
        if (self.stride == 1) and (inp != branch_features << 1):
            raise ValueError(
                f"Invalid combination of stride {stride}, inp {inp} and oup {oup} values. If stride == 1 then inp should be equal to oup // 2 << 1."
            )

        if self.stride > 1:
            self.branch1 = nn.Sequential(
                self.depthwise_conv(inp, inp, kernel_size=3, stride=self.stride, padding=1),
                nn.BatchNorm2d(inp),
                nn.Conv2d(inp, branch_features, kernel_size=1, stride=1, padding=0, bias=False),
                nn.BatchNorm2d(branch_features),
                nn.ReLU(inplace=True),
            )
        else:
            self.branch1 = nn.Sequential()

        self.branch2 = nn.Sequential(
            nn.Conv2d(
                inp if (self.stride > 1) else branch_features,
                branch_features,
                kernel_size=1,
                stride=1,
                padding=0,
                bias=False,
            ),
            nn.BatchNorm2d(branch_features),
            nn.ReLU(inplace=True),
            self.depthwise_conv(branch_features, branch_features, kernel_size=3, stride=self.stride, padding=1),
            nn.BatchNorm2d(branch_features),
            nn.Conv2d(branch_features, branch_features, kernel_size=1, stride=1, padding=0, bias=False),
            nn.BatchNorm2d(branch_features),
            nn.ReLU(inplace=True),
        )

    @staticmethod
    def depthwise_conv(
            i: int, o: int, kernel_size: int, stride: int = 1, padding: int = 0, bias: bool = False
    ) -> nn.Conv2d:
        return nn.Conv2d(i, o, kernel_size, stride, padding, bias=bias, groups=i)

    def forward(self, x: Tensor) -> Tensor:
        if self.stride == 1:
            x1, x2 = x.chunk(2, dim=1)
            out = torch.cat((x1, self.branch2(x2)), dim=1)
        else:
            out = torch.cat((self.branch1(x), self.branch2(x)), dim=1)

        out = channel_shuffle(out, 2)

        return out


class ShuffleNetV2(nn.Module):
    def __init__(
            self,
            stages_repeats: List[int],
            stages_out_channels: List[int],
            num_classes: int = 1000,
            inverted_residual: Callable[..., nn.Module] = InvertedResidual,
    ) -> None:
        super().__init__()

        if len(stages_repeats) != 3:
            raise ValueError("expected stages_repeats as list of 3 positive ints")
        if len(stages_out_channels) != 5:
            raise ValueError("expected stages_out_channels as list of 5 positive ints")
        self._stage_out_channels = stages_out_channels

        input_channels = 3
        output_channels = self._stage_out_channels[0]
        self.conv1 = nn.Sequential(
            nn.Conv2d(input_channels, output_channels, 3, 2, 1, bias=False),
            nn.BatchNorm2d(output_channels),
            nn.ReLU(inplace=True),
        )
        input_channels = output_channels

        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)

        # Static annotations for mypy
        self.stage2: nn.Sequential
        self.stage3: nn.Sequential
        self.stage4: nn.Sequential

        # repeats the residual blocks here for each stage
        # TODO change this to accomodate skip layer excitation
        stage_names = [f"stage{i}" for i in [2, 3, 4]]
        for name, repeats, output_channels in zip(stage_names, stages_repeats, self._stage_out_channels[1:]):
            seq = [inverted_residual(input_channels, output_channels, 2)]
            for i in range(repeats - 1):
                seq.append(inverted_residual(output_channels, output_channels, 1))
            setattr(self, name, nn.Sequential(*seq))
            input_channels = output_channels

        output_channels = self._stage_out_channels[-1]
        self.conv5 = nn.Sequential(
            nn.Conv2d(input_channels, output_channels, 1, 1, 0, bias=False),
            nn.BatchNorm2d(output_channels),
            nn.ReLU(inplace=True),
        )

        self.fc = nn.Linear(output_channels, num_classes)

        # SEBlocks
        self.se_1 = SEBlock(self._stage_out_channels[0], self._stage_out_channels[1])  # maxpool and stage2
        self.se_2 = SEBlock(self._stage_out_channels[1], self._stage_out_channels[2])  # stage2 to stage3
        self.se_3 = SEBlock(self._stage_out_channels[2], self._stage_out_channels[3])  # stage3 to stage4

    def _forward_SE(self, x: Tensor, return_intermediate=False) -> Tensor:
        # todo add the decoders
        # I think it would be best to return intermediate layer outputs here as well, and put them through
        # a decoder afterwards, so it's not just permanently part of the inference process
        x = self.conv1(x)
        p1 = self.maxpool(x)
        stage2 = self.stage2(p1)
        stage2 = self.se_1(p1, stage2)
        stage3 = self.stage3(stage2)
        stage3 = self.se_2(stage2, stage3)
        stage4 = self.stage4(stage3)
        stage4 = self.se_3(stage3, stage4)
        final_conv = self.conv5(stage4)
        final_conv = final_conv.mean([2, 3])  # global pool along H W
        return self.fc(final_conv)

    def _forward_impl(self, x: Tensor) -> Tensor:
        # See note [TorchScript super()]
        x = self.conv1(x)
        x = self.maxpool(x)
        x = self.stage2(x)
        x = self.stage3(x)
        x = self.stage4(x)
        x = self.conv5(x)
        x = x.mean([2, 3])  # globalpool
        x = self.fc(x)
        return x

    def forward(self, x: Tensor, **kwargs) -> Tensor:
        # return self._forward_impl(x)
        return self._forward_SE(x, **kwargs)


def shufflenet_v2_x0_5(
        *, weights=None, progress: bool = True, **kwargs: Any
) -> ShuffleNetV2:
    """
    Constructs a ShuffleNetV2 architecture with 0.5x output channels, as described in
    `ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design
    <https://arxiv.org/abs/1807.11164>`__.
    Args:
        weights (:class:`~torchvision.models.ShuffleNet_V2_X0_5_Weights`, optional): The
            pretrained weights to use. See
            :class:`~torchvision.models.ShuffleNet_V2_X0_5_Weights` below for
            more details, and possible values. By default, no pre-trained
            weights are used.
        progress (bool, optional): If True, displays a progress bar of the
            download to stderr. Default is True.
        **kwargs: parameters passed to the ``torchvision.models.shufflenetv2.ShuffleNetV2``
            base class. Please refer to the `source code
            <https://github.com/pytorch/vision/blob/main/torchvision/models/shufflenetv2.py>`_
            for more details about this class.
    .. autoclass:: torchvision.models.ShuffleNet_V2_X0_5_Weights
        :members:
    """
    weights = ShuffleNet_V2_X0_5_Weights.verify(weights)

    return _shufflenetv2(weights, progress, [4, 8, 4], [24, 48, 96, 192, 1024], **kwargs)


@register_model()
@handle_legacy_interface(weights=("pretrained", ShuffleNet_V2_X1_0_Weights.IMAGENET1K_V1))
def shufflenet_v2_x1_0(
        *, weights: Optional[ShuffleNet_V2_X1_0_Weights] = None, progress: bool = True, **kwargs: Any
) -> ShuffleNetV2:
    """
    Constructs a ShuffleNetV2 architecture with 1.0x output channels, as described in
    `ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design
    <https://arxiv.org/abs/1807.11164>`__.
    Args:
        weights (:class:`~torchvision.models.ShuffleNet_V2_X1_0_Weights`, optional): The
            pretrained weights to use. See
            :class:`~torchvision.models.ShuffleNet_V2_X1_0_Weights` below for
            more details, and possible values. By default, no pre-trained
            weights are used.
        progress (bool, optional): If True, displays a progress bar of the
            download to stderr. Default is True.
        **kwargs: parameters passed to the ``torchvision.models.shufflenetv2.ShuffleNetV2``
            base class. Please refer to the `source code
            <https://github.com/pytorch/vision/blob/main/torchvision/models/shufflenetv2.py>`_
            for more details about this class.
    .. autoclass:: torchvision.models.ShuffleNet_V2_X1_0_Weights
        :members:
    """
    weights = ShuffleNet_V2_X1_0_Weights.verify(weights)

    return _shufflenetv2(weights, progress, [4, 8, 4], [24, 116, 232, 464, 1024], **kwargs)


@register_model()
@handle_legacy_interface(weights=("pretrained", ShuffleNet_V2_X1_5_Weights.IMAGENET1K_V1))
def shufflenet_v2_x1_5(
        *, weights: Optional[ShuffleNet_V2_X1_5_Weights] = None, progress: bool = True, **kwargs: Any
) -> ShuffleNetV2:
    """
    Constructs a ShuffleNetV2 architecture with 1.5x output channels, as described in
    `ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design
    <https://arxiv.org/abs/1807.11164>`__.
    Args:
        weights (:class:`~torchvision.models.ShuffleNet_V2_X1_5_Weights`, optional): The
            pretrained weights to use. See
            :class:`~torchvision.models.ShuffleNet_V2_X1_5_Weights` below for
            more details, and possible values. By default, no pre-trained
            weights are used.
        progress (bool, optional): If True, displays a progress bar of the
            download to stderr. Default is True.
        **kwargs: parameters passed to the ``torchvision.models.shufflenetv2.ShuffleNetV2``
            base class. Please refer to the `source code
            <https://github.com/pytorch/vision/blob/main/torchvision/models/shufflenetv2.py>`_
            for more details about this class.
    .. autoclass:: torchvision.models.ShuffleNet_V2_X1_5_Weights
        :members:
    """
    weights = ShuffleNet_V2_X1_5_Weights.verify(weights)

    return _shufflenetv2(weights, progress, [4, 8, 4], [24, 176, 352, 704, 1024], **kwargs)


@register_model()
@handle_legacy_interface(weights=("pretrained", ShuffleNet_V2_X2_0_Weights.IMAGENET1K_V1))
def shufflenet_v2_x2_0(
        *, weights: Optional[ShuffleNet_V2_X2_0_Weights] = None, progress: bool = True, **kwargs: Any
) -> ShuffleNetV2:
    """
    Constructs a ShuffleNetV2 architecture with 2.0x output channels, as described in
    `ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design
    <https://arxiv.org/abs/1807.11164>`__.
    Args:
        weights (:class:`~torchvision.models.ShuffleNet_V2_X2_0_Weights`, optional): The
            pretrained weights to use. See
            :class:`~torchvision.models.ShuffleNet_V2_X2_0_Weights` below for
            more details, and possible values. By default, no pre-trained
            weights are used.
        progress (bool, optional): If True, displays a progress bar of the
            download to stderr. Default is True.
        **kwargs: parameters passed to the ``torchvision.models.shufflenetv2.ShuffleNetV2``
            base class. Please refer to the `source code
            <https://github.com/pytorch/vision/blob/main/torchvision/models/shufflenetv2.py>`_
            for more details about this class.
    .. autoclass:: torchvision.models.ShuffleNet_V2_X2_0_Weights
        :members:
    """
    weights = ShuffleNet_V2_X2_0_Weights.verify(weights)

    return _shufflenetv2(weights, progress, [4, 8, 4], [24, 244, 488, 976, 2048], **kwargs)
