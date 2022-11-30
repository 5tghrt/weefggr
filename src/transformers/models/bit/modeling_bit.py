# coding=utf-8
# Copyright 2022 Google AI and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" PyTorch BiT model. Also supports backbone for ViT hybrid."""

import collections
import math
from functools import partial
from typing import List, Optional, Tuple

import torch
import torch.utils.checkpoint
from torch import Tensor, nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ...modeling_outputs import (
    BackboneOutput,
    BaseModelOutputWithNoAttention,
    BaseModelOutputWithPoolingAndNoAttention,
    ImageClassifierOutputWithNoAttention,
)
from ...modeling_utils import PreTrainedModel
from ...utils import (
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
    replace_return_docstrings,
)
from .configuration_bit import BitConfig


logger = logging.get_logger(__name__)

# General docstring
_CONFIG_FOR_DOC = "BitConfig"
_FEAT_EXTRACTOR_FOR_DOC = "AutoFeatureExtractor"

# Base docstring
_CHECKPOINT_FOR_DOC = "google/resnetnv2-50"
_EXPECTED_OUTPUT_SHAPE = [1, 2048, 7, 7]

# Image classification docstring
_IMAGE_CLASS_CHECKPOINT = "google/resnetnv2-50"
_IMAGE_CLASS_EXPECTED_OUTPUT = "tiger cat"

BIT_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "google/resnetnv2-50",
    # See all BiT models at https://huggingface.co/models?filter=resnetv2
]


# Can SAME padding for given args be done statically?
def is_static_pad(kernel_size: int, stride: int = 1, dilation: int = 1, **_):
    return stride == 1 and (dilation * (kernel_size - 1)) % 2 == 0


def get_padding_value(padding, kernel_size, **kwargs) -> Tuple[Tuple, bool]:
    dynamic = False
    if isinstance(padding, str):
        # for any string padding, the padding will be calculated for you, one of three ways
        padding = padding.lower()
        if padding == "same":
            # TF compatible 'SAME' padding, has a performance and GPU memory allocation impact
            if is_static_pad(kernel_size, **kwargs):
                # static case, no extra overhead
                padding = get_padding(kernel_size, **kwargs)
            else:
                # dynamic 'SAME' padding, has runtime/GPU memory overhead
                padding = 0
                dynamic = True
        elif padding == "valid":
            # 'VALID' padding, same as padding=0
            padding = 0
        else:
            # Default to PyTorch style 'same'-ish symmetric padding
            padding = get_padding(kernel_size, **kwargs)
    return padding, dynamic


class StdConv2dSame(nn.Conv2d):
    """Conv2d with Weight Standardization. TF compatible SAME padding. Used for ViT Hybrid model.

    Paper: [Micro-Batch Training with Batch-Channel Normalization and Weight
    Standardization](https://arxiv.org/abs/1903.10520v2)
    """

    def __init__(
        self,
        in_channel,
        out_channels,
        kernel_size,
        stride=1,
        padding="SAME",
        dilation=1,
        groups=1,
        bias=False,
        eps=1e-6,
    ):
        padding, is_dynamic = get_padding_value(padding, kernel_size, stride=stride, dilation=dilation)
        super().__init__(
            in_channel,
            out_channels,
            kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
        )
        self.same_pad = is_dynamic
        self.eps = eps

    def forward(self, x):
        if self.same_pad:
            x = pad_same(x, self.kernel_size, self.stride, self.dilation)
        weight = nn.functional.batch_norm(
            self.weight.reshape(1, self.out_channels, -1), None, None, training=True, momentum=0.0, eps=self.eps
        ).reshape_as(self.weight)
        x = nn.functional.conv2d(x, weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        return x


def _num_groups(num_channels, num_groups, group_size):
    if group_size:
        assert num_channels % group_size == 0
        return num_channels // group_size
    return num_groups


class BitGroupNormActivation(nn.GroupNorm):
    # NOTE num_channel and num_groups order flipped for easier layer swaps / binding of fixed args
    def __init__(
        self,
        num_channels,
        num_groups=32,
        eps=1e-5,
        affine=True,
        group_size=None,
        apply_act=True,
        act_layer=nn.ReLU,
        inplace=True,
        drop_layer=None,
    ):
        super(BitGroupNormActivation, self).__init__(
            _num_groups(num_channels, num_groups, group_size), num_channels, eps=eps, affine=affine
        )
        self.drop = drop_layer() if drop_layer is not None else nn.Identity()
        # act_layer = get_act_layer(act_layer)  # string -> nn.Module
        if act_layer is not None and apply_act:
            act_args = dict(inplace=True) if inplace else {}
            self.act = act_layer(**act_args)
        else:
            self.act = nn.Identity()
        self._fast_norm = False  # TODO add support for fast norm

    def forward(self, x):
        # if self._fast_norm:
        #     x = fast_group_norm(x, self.num_groups, self.weight, self.bias, self.eps)
        # else:
        x = nn.functional.group_norm(x, self.num_groups, self.weight, self.bias, self.eps)
        x = self.drop(x)
        x = self.act(x)
        return x


# Calculate symmetric padding for a convolution
def get_padding(kernel_size: int, stride: int = 1, dilation: int = 1, **_) -> int:
    padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
    return padding


class StdConv2d(nn.Conv2d):
    """Conv2d with Weight Standardization. Used for BiT ResNet-V2 models.

    Paper: `Micro-Batch Training with Batch-Channel Normalization and Weight Standardization` -
        https://arxiv.org/abs/1903.10520v2
    """

    def __init__(
        self, in_channel, out_channels, kernel_size, stride=1, padding=None, dilation=1, groups=1, bias=False, eps=1e-6
    ):
        if padding is None:
            padding = get_padding(kernel_size, stride, dilation)
        super().__init__(
            in_channel,
            out_channels,
            kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
        )
        self.eps = eps

    def forward(self, x):
        weight = nn.functional.batch_norm(
            self.weight.reshape(1, self.out_channels, -1), None, None, training=True, momentum=0.0, eps=self.eps
        ).reshape_as(self.weight)
        x = nn.functional.conv2d(x, weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        return x


# Calculate asymmetric TensorFlow-like 'SAME' padding for a convolution
def get_same_padding(x: int, k: int, s: int, d: int):
    return max((math.ceil(x / s) - 1) * s + (k - 1) * d + 1 - x, 0)


# Dynamically pad input x with 'SAME' padding for conv with specified args
def pad_same(x, k: List[int], s: List[int], d: List[int] = (1, 1), value: float = 0):
    ih, iw = x.size()[-2:]
    pad_h, pad_w = get_same_padding(ih, k[0], s[0], d[0]), get_same_padding(iw, k[1], s[1], d[1])
    if pad_h > 0 or pad_w > 0:
        x = nn.functional.pad(x, [pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2], value=value)
    return x


class MaxPool2dSame(nn.MaxPool2d):
    """Tensorflow like 'SAME' wrapper for 2D max pooling"""

    def __init__(self, kernel_size: int, stride=None, dilation=1, ceil_mode=False):
        kernel_size = kernel_size if isinstance(kernel_size, collections.abc.Iterable) else (kernel_size, kernel_size)
        stride = stride if isinstance(stride, collections.abc.Iterable) else (stride, stride)
        dilation = dilation if isinstance(dilation, collections.abc.Iterable) else (dilation, dilation)
        super(MaxPool2dSame, self).__init__(kernel_size, stride, (0, 0), dilation, ceil_mode)

    def forward(self, x):
        x = pad_same(x, self.kernel_size, self.stride, value=-float("inf"))
        return nn.functional.max_pool2d(x, self.kernel_size, self.stride, (0, 0), self.dilation, self.ceil_mode)


class BitEmbeddings(nn.Module):
    """
    BiT Embeddings (stem) composed of a single aggressive convolution.
    """

    def __init__(self, config: BitConfig):
        super().__init__()
        if config.conv_layer == "std_conv":
            conv_layer = partial(StdConv2d, eps=1e-8)
        elif config.conv_layer == "std_conv_same":
            conv_layer = partial(StdConv2dSame, eps=1e-8)

        self.convolution = conv_layer(
            config.num_channels, config.embedding_size, kernel_size=7, stride=2, padding=3, bias=False
        )
       
        self.norm = None
        if not config.layer_type == "preactivation":
            self.norm = partial(BitGroupNormActivation, num_groups=32)(config.embedding_size)
        
        if config.stem_type == "same":
            self.pooler = MaxPool2dSame(kernel_size=3, stride=2)
        else:
            self.pooler = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.num_channels = config.num_channels

    def forward(self, pixel_values: Tensor) -> Tensor:
        num_channels = pixel_values.shape[1]
        if num_channels != self.num_channels:
            raise ValueError(
                "Make sure that the channel dimension of the pixel values match with the one set in the configuration."
            )

        embedding = self.convolution(pixel_values)

        print("Shape of embeddings after conv2d:", embedding.shape)
        print("First values:", embedding[0, 0, :3, :3])

        if self.norm is not None:
            embedding = self.norm(embedding)

        embedding = self.pooler(embedding)

        print("Shape of BiT embeddings:", embedding.shape)
        print("First values of BiT embeddings:", embedding[0,0,:3,:3])

        return embedding


# Copied from transformers.models.convnext.modeling_convnext.drop_path
def drop_path(input, drop_prob: float = 0.0, training: bool = False):
    """
    Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks).

    Comment by Ross Wightman: This is the same as the DropConnect impl I created for EfficientNet, etc networks,
    however, the original name is misleading as 'Drop Connect' is a different form of dropout in a separate paper...
    See discussion: https://github.com/tensorflow/tpu/issues/494#issuecomment-532968956 ... I've opted for changing the
    layer and argument names to 'drop path' rather than mix DropConnect as a layer name and use 'survival rate' as the
    argument.
    """
    if drop_prob == 0.0 or not training:
        return input
    keep_prob = 1 - drop_prob
    shape = (input.shape[0],) + (1,) * (input.ndim - 1)  # work with diff dim tensors, not just 2D ConvNets
    random_tensor = keep_prob + torch.rand(shape, dtype=input.dtype, device=input.device)
    random_tensor.floor_()  # binarize
    output = input.div(keep_prob) * random_tensor
    return output


# Copied from transformers.models.convnext.modeling_convnext.ConvNextDropPath
class BitDropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample (when applied in main path of residual blocks)."""

    def __init__(self, drop_prob: Optional[float] = None) -> None:
        super().__init__()
        self.drop_prob = drop_prob

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return drop_path(x, self.drop_prob, self.training)

    def extra_repr(self) -> str:
        return "p={}".format(self.drop_prob)


def make_div(v, divisor=8):
    min_value = divisor
    new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
    if new_v < 0.9 * v:
        new_v += divisor
    return new_v


class BitPreActivationBottleneckLayer(nn.Module):
    """Pre-activation (v2) bottleneck block.
    Follows the implementation of "Identity Mappings in Deep Residual Networks":
    https://github.com/KaimingHe/resnet-1k-layers/blob/master/resnet-pre-act.lua

    Except it puts the stride on 3x3 conv when available.
    """

    def __init__(
        self,
        in_channels,
        out_channels=None,
        bottle_ratio=0.25,
        stride=1,
        dilation=1,
        first_dilation=None,
        groups=1,
        act_layer=None,
        conv_layer=None,
        norm_layer=None,
        proj_layer=None,
        drop_path_rate=0.0,
    ):
        super().__init__()

        first_dilation = first_dilation or dilation
        conv_layer = conv_layer or StdConv2d
        norm_layer = norm_layer or partial(BitGroupNormActivation, num_groups=32)
        out_channels = out_channels or in_channels
        mid_channels = make_div(out_channels * bottle_ratio)

        if proj_layer is not None:
            self.downsample = proj_layer(
                in_channels,
                out_channels,
                stride=stride,
                dilation=dilation,
                first_dilation=first_dilation,
                preact=True,
                conv_layer=conv_layer,
                norm_layer=norm_layer,
            )
        else:
            self.downsample = None

        self.norm1 = norm_layer(in_channels)
        self.conv1 = conv_layer(in_channels, mid_channels, 1)
        self.norm2 = norm_layer(mid_channels)
        self.conv2 = conv_layer(mid_channels, mid_channels, 3, stride=stride, dilation=first_dilation, groups=groups)
        self.norm3 = norm_layer(mid_channels)
        self.conv3 = conv_layer(mid_channels, out_channels, 1)
        self.drop_path = BitDropPath(drop_path_rate) if drop_path_rate > 0 else nn.Identity()

    def forward(self, x, print_values=False):
        x_preact = self.norm1(x)

        # if print_values:
        #     print("Hidden states after first norm:", x_preact[0, 0, :3, :3])

        # shortcut branch
        shortcut = x
        if self.downsample is not None:
            shortcut = self.downsample(x_preact, print_values)

        # if print_values:
        #     print("Hidden states after downsample:", shortcut[0, 0, :3, :3])

        # residual branch
        x = self.conv1(x_preact)
        x = self.conv2(self.norm2(x))
        x = self.conv3(self.norm3(x))
        x = self.drop_path(x)
        return x + shortcut


class BitBottleneckLayer(nn.Module):
    """Non Pre-activation bottleneck block, equivalent to V1.5/V1b bottleneck. Used for ViT."""

    def __init__(
        self,
        in_channels,
        out_channels=None,
        bottle_ratio=0.25,
        stride=1,
        dilation=1,
        first_dilation=None,
        groups=1,
        act_layer=None,
        conv_layer=None,
        norm_layer=None,
        proj_layer=None,
        drop_path_rate=0.0,
    ):
        super().__init__()
        first_dilation = first_dilation or dilation
        act_layer = act_layer or nn.ReLU
        conv_layer = conv_layer or StdConv2d
        norm_layer = norm_layer or partial(BitGroupNormActivation, num_groups=32)
        out_channels = out_channels or in_channels
        mid_chs = make_div(out_channels * bottle_ratio)

        if proj_layer is not None:
            self.downsample = proj_layer(
                in_channels,
                out_channels,
                stride=stride,
                dilation=dilation,
                preact=False,
                conv_layer=conv_layer,
                norm_layer=norm_layer,
            )
        else:
            self.downsample = None

        self.conv1 = conv_layer(in_channels, mid_chs, 1)
        self.norm1 = norm_layer(mid_chs)
        self.conv2 = conv_layer(mid_chs, mid_chs, 3, stride=stride, dilation=first_dilation, groups=groups)
        self.norm2 = norm_layer(mid_chs)
        self.conv3 = conv_layer(mid_chs, out_channels, 1)
        self.norm3 = norm_layer(out_channels, apply_act=False)
        self.drop_path = BitDropPath(drop_path_rate) if drop_path_rate > 0 else nn.Identity()
        self.act3 = act_layer(inplace=True)

    def forward(self, x, print_values=False):
        # shortcut branch
        shortcut = x
        if self.downsample is not None:
            shortcut = self.downsample(x)

        # residual
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.conv2(x)
        x = self.norm2(x)
        x = self.conv3(x)
        x = self.norm3(x)
        x = self.drop_path(x)
        x = self.act3(x + shortcut)
        return x


class BitDownsampleConv(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        stride=1,
        dilation=1,
        first_dilation=None,
        preact=True,
        conv_layer=None,
        norm_layer=None,
    ):
        super(BitDownsampleConv, self).__init__()
        self.conv_layer = conv_layer
        self.conv = conv_layer(in_channels, out_channels, 1, stride=stride)
        self.norm = nn.Identity() if preact else norm_layer(out_channels, apply_act=False)

    def forward(self, x, print_values=False):
        # if print_values:
        #     print("Conv layer:", self.conv_layer)
        #     print("Hidden states before downsample conv:", x[0, 0, :3, :3])

        z = self.conv(x)

        # if print_values:
        #     print("Hidden states after downsample conv:", z[0, 0, :3, :3])

        return self.norm(self.conv(x))


class BitStage(nn.Module):
    """
    A ResNet v2 stage composed by stacked layers.
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        stride,
        dilation,
        depth,
        bottle_ratio=0.25,
        groups=1,
        avg_down=False,
        layer_dpr=None,
        layer_fn=BitPreActivationBottleneckLayer,
        act_layer=None,
        conv_layer=None,
        norm_layer=None,
        **layer_kwargs
    ):
        super().__init__()

        first_dilation = 1 if dilation in (1, 2) else 2
        layer_kwargs = dict(act_layer=act_layer, conv_layer=conv_layer, norm_layer=norm_layer)
        if avg_down:
            # TODO add support for avg_down
            raise NotImplementedError("avg_down is not implemented")
        proj_layer = BitDownsampleConv
        prev_chs = in_channels
        self.layers = nn.Sequential()
        for layer_idx in range(depth):
            drop_path_rate = layer_dpr[layer_idx] if layer_dpr else 0.0
            stride = stride if layer_idx == 0 else 1
            self.layers.add_module(
                str(layer_idx),
                layer_fn(
                    prev_chs,
                    out_channels,
                    stride=stride,
                    dilation=dilation,
                    bottle_ratio=bottle_ratio,
                    groups=groups,
                    first_dilation=first_dilation,
                    proj_layer=proj_layer,
                    drop_path_rate=drop_path_rate,
                    **layer_kwargs,
                ),
            )
            prev_chs = out_channels
            first_dilation = dilation
            proj_layer = None

    def forward(self, input: Tensor, print_values=False) -> Tensor:
        hidden_state = input
        for idx, layer in enumerate(self.layers):
            # if idx == 0 and print_values:
            #     print(f"Hidden states before block {idx}", hidden_state[0, 0, :3, :3])
            hidden_state = layer(hidden_state, print_values=idx == 0)
            # if idx == 0 and print_values:
            #     print(f"Hidden states after block {idx}", hidden_state[0, 0, :3, :3])
        return hidden_state


class BitEncoder(nn.Module):
    def __init__(self, config: BitConfig):
        super().__init__()
        self.stages = nn.ModuleList([])

        act_layer = nn.ReLU
        if config.conv_layer == "std_conv":
            conv_layer = partial(StdConv2d, eps=1e-8)
        elif config.conv_layer == "std_conv_same":
            conv_layer = partial(StdConv2dSame, eps=1e-8)

        norm_layer = partial(BitGroupNormActivation, num_groups=32)

        prev_chs = config.embedding_size
        curr_stride = 4
        dilation = 1
        layer_dprs = [
            x.tolist() for x in torch.linspace(0, config.drop_path_rate, sum(config.depths)).split(config.depths)
        ]
        if config.layer_type == "bottleneck":
            layer_fn = BitBottleneckLayer
        elif config.layer_type == "preactivation":
            layer_fn = BitPreActivationBottleneckLayer
        else:
            raise ValueError("Unknown layer type: {}".format(config.layer_type))

        for stage_idx, (d, c, bdpr) in enumerate(zip(config.depths, config.hidden_sizes, layer_dprs)):
            out_channels = make_div(c * config.width_factor)
            stride = 1 if stage_idx == 0 else 2
            if curr_stride >= config.output_stride:
                dilation *= stride
                stride = 1
            stage = BitStage(
                prev_chs,
                out_channels,
                stride=stride,
                dilation=dilation,
                depth=d,
                avg_down=False,
                act_layer=act_layer,
                conv_layer=conv_layer,
                norm_layer=norm_layer,
                layer_dpr=bdpr,
                layer_fn=layer_fn,
            )
            prev_chs = out_channels
            curr_stride *= stride
            self.stages.add_module(str(stage_idx), stage)

    def forward(
        self, hidden_state: Tensor, output_hidden_states: bool = False, return_dict: bool = True
    ) -> BaseModelOutputWithNoAttention:
        hidden_states = () if output_hidden_states else None

        for idx, stage_module in enumerate(self.stages):
            if output_hidden_states:
                hidden_states = hidden_states + (hidden_state,)

            hidden_state = stage_module(hidden_state, print_values=idx == 0)

        if output_hidden_states:
            hidden_states = hidden_states + (hidden_state,)

        if not return_dict:
            return tuple(v for v in [hidden_state, hidden_states] if v is not None)

        return BaseModelOutputWithNoAttention(
            last_hidden_state=hidden_state,
            hidden_states=hidden_states,
        )


# Copied from transformers.models.resnet.modeling_resnet.ResNetPreTrainedModel with ResNet->Bit,resnet->resnetv2
class BitPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = BitConfig
    base_model_prefix = "resnetv2"
    main_input_name = "pixel_values"
    supports_gradient_checkpointing = True

    def _init_weights(self, module):
        if isinstance(module, nn.Conv2d):
            nn.init.kaiming_normal_(module.weight, mode="fan_out", nonlinearity="relu")
        elif isinstance(module, (nn.BatchNorm2d, nn.GroupNorm)):
            nn.init.constant_(module.weight, 1)
            nn.init.constant_(module.bias, 0)

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, BitModel):
            module.gradient_checkpointing = value


BIT_START_DOCSTRING = r"""
    This model is a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass. Use it
    as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage and
    behavior.

    Parameters:
        config ([`BitConfig`]): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""

BIT_INPUTS_DOCSTRING = r"""
    Args:
        pixel_values (`torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`):
            Pixel values. Pixel values can be obtained using [`AutoFeatureExtractor`]. See
            [`AutoFeatureExtractor.__call__`] for details.

        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare BiT model outputting raw features without any specific head on top.",
    BIT_START_DOCSTRING,
)
class BitModel(BitPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.embedder = BitEmbeddings(config)

        self.encoder = BitEncoder(config)
        norm_layer = partial(BitGroupNormActivation, num_groups=32)
        self.norm = norm_layer(config.hidden_sizes[-1]) if config.layer_type == "preactivation" else nn.Identity()

        self.pooler = nn.AdaptiveAvgPool2d((1, 1))
        # Initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(BIT_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        processor_class=_FEAT_EXTRACTOR_FOR_DOC,
        checkpoint=_CHECKPOINT_FOR_DOC,
        output_type=BaseModelOutputWithPoolingAndNoAttention,
        config_class=_CONFIG_FOR_DOC,
        modality="vision",
        expected_output=_EXPECTED_OUTPUT_SHAPE,
    )
    def forward(
        self, pixel_values: Tensor, output_hidden_states: Optional[bool] = None, return_dict: Optional[bool] = None
    ) -> BaseModelOutputWithPoolingAndNoAttention:
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        embedding_output = self.embedder(pixel_values)

        encoder_outputs = self.encoder(
            embedding_output, output_hidden_states=output_hidden_states, return_dict=return_dict
        )

        last_hidden_state = encoder_outputs[0]

        last_hidden_state = self.norm(last_hidden_state)

        pooled_output = self.pooler(last_hidden_state)

        if not return_dict:
            return (last_hidden_state, pooled_output) + encoder_outputs[1:]

        return BaseModelOutputWithPoolingAndNoAttention(
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=encoder_outputs.hidden_states,
        )


@add_start_docstrings(
    """
    BiT Model with an image classification head on top (a linear layer on top of the pooled features), e.g. for
    ImageNet.
    """,
    BIT_START_DOCSTRING,
)
class BitForImageClassification(BitPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.resnetv2 = BitModel(config)
        # classification head
        self.classifier = nn.Sequential(
            nn.Flatten(),
            nn.Linear(config.hidden_sizes[-1], config.num_labels) if config.num_labels > 0 else nn.Identity(),
        )
        # initialize weights and apply final processing
        self.post_init()

    @add_start_docstrings_to_model_forward(BIT_INPUTS_DOCSTRING)
    @add_code_sample_docstrings(
        processor_class=_FEAT_EXTRACTOR_FOR_DOC,
        checkpoint=_IMAGE_CLASS_CHECKPOINT,
        output_type=ImageClassifierOutputWithNoAttention,
        config_class=_CONFIG_FOR_DOC,
        expected_output=_IMAGE_CLASS_EXPECTED_OUTPUT,
    )
    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> ImageClassifierOutputWithNoAttention:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the image classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.resnetv2(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

        pooled_output = outputs.pooler_output if return_dict else outputs[1]

        logits = self.classifier(pooled_output)

        loss = None

        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"
            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return (loss,) + output if loss is not None else output

        return ImageClassifierOutputWithNoAttention(loss=loss, logits=logits, hidden_states=outputs.hidden_states)


@add_start_docstrings(
    """
    BiT backbone, to be used with frameworks like DETR and MaskFormer.
    """,
    BIT_START_DOCSTRING,
)
class BitBackbone(BitPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.stage_names = config.stage_names
        self.resnetv2 = BitModel(config)

        self.out_features = config.out_features

        out_feature_channels = {}
        out_feature_channels["stem"] = config.embedding_size
        for idx, stage in enumerate(self.stage_names[1:]):
            out_feature_channels[stage] = config.hidden_sizes[idx]

        self.out_feature_channels = out_feature_channels

        # initialize weights and apply final processing
        self.post_init()

    @property
    def channels(self):
        return [self.out_feature_channels[name] for name in self.out_features]

    @add_start_docstrings_to_model_forward(BIT_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=BackboneOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self, pixel_values: Tensor, output_hidden_states: Optional[bool] = None, return_dict: Optional[bool] = None
    ) -> BackboneOutput:
        """
        Returns:

        Examples:

        ```python
        >>> from transformers import AutoImageProcessor, AutoBackbone
        >>> import torch
        >>> from PIL import Image
        >>> import requests

        >>> url = "http://images.cocodataset.org/val2017/000000039769.jpg"
        >>> image = Image.open(requests.get(url, stream=True).raw)

        >>> processor = AutoImageProcessor.from_pretrained("microsoft/resnet-50")
        >>> model = AutoBackbone.from_pretrained("microsoft/resnet-50")

        >>> inputs = processor(image, return_tensors="pt")
        >>> outputs = model(**inputs)
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )

        outputs = self.resnetv2(pixel_values, output_hidden_states=True, return_dict=True)

        hidden_states = outputs.hidden_states

        feature_maps = ()
        for idx, stage in enumerate(self.stage_names):
            if stage in self.out_features:
                feature_maps += (hidden_states[idx],)

        if not return_dict:
            output = (feature_maps,)
            if output_hidden_states:
                output += (outputs.hidden_states,)
            return output

        return BackboneOutput(
            feature_maps=feature_maps,
            hidden_states=outputs.hidden_states if output_hidden_states else None,
            attentions=None,
        )