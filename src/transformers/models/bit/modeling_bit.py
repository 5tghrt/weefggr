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
from typing import Optional, Tuple

import torch
import torch.utils.checkpoint
from torch import Tensor, nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from ...activations import ACT2FN
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
    # See all BiT models at https://huggingface.co/models?filter=bit
]


def get_padding_value(padding=None, kernel_size=7, stride=1, dilation=1) -> Tuple[Tuple, bool]:
    r"""
    Utility function to get the tuple padding value given the kernel_size and padding

    Args:
        padding, Union[`str`, `int`]:
            padding value, can be either `"same"`, `"valid"`. If a different value is provided the default padding from
            PyTorch is used.
        kernel_size, `int`:
            Kernel size of the convolution layers.
    """
    dynamic = False
    if padding is None:
        padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
        return padding, dynamic

    if isinstance(padding, str):
        # for any string padding, the padding will be calculated for you, one of three ways
        padding = padding.lower()
        if padding == "same":
            # TF compatible 'SAME' padding, has a performance and GPU memory allocation impact
            if stride == 1 and (dilation * (kernel_size - 1)) % 2 == 0:
                # static case, no extra overhead
                padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
            else:
                # dynamic 'SAME' padding, has runtime/GPU memory overhead
                padding = 0
                dynamic = True
        elif padding == "valid":
            # 'VALID' padding, same as padding=0
            padding = 0
        else:
            # Default to PyTorch style 'same'-ish symmetric padding
            padding = ((stride - 1) + dilation * (kernel_size - 1)) // 2
    return padding, dynamic


class WeightStandardizedConv2d(nn.Conv2d):
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
        if is_dynamic:
            self.pad = DynamicPad2d(kernel_size, stride, dilation)
        else:
            self.pad = None
        self.eps = eps

    def forward(self, x):
        if self.pad is not None:
            x = self.pad(x)
        weight = nn.functional.batch_norm(
            self.weight.reshape(1, self.out_channels, -1), None, None, training=True, momentum=0.0, eps=self.eps
        ).reshape_as(self.weight)
        x = nn.functional.conv2d(x, weight, self.bias, self.stride, self.padding, self.dilation, self.groups)
        return x


def _num_groups(num_channels, num_groups, group_size):
    if group_size:
        if num_channels % group_size == 0:
            raise ValueError("num_channels must divide group_size")
        return num_channels // group_size
    return num_groups


class BitGroupNormActivation(nn.GroupNorm):
    # NOTE num_channel and num_groups order flipped for easier layer swaps / binding of fixed args
    def __init__(
        self,
        config,
        num_channels,
        eps=1e-5,
        affine=True,
        group_size=None,
        apply_act=True,
        drop_layer=None,
    ):

        super(BitGroupNormActivation, self).__init__(
            _num_groups(num_channels, config.num_groups, group_size), num_channels, eps=eps, affine=affine
        )
        self.drop = drop_layer() if drop_layer is not None else nn.Identity()
        if apply_act:
            self.activation = ACT2FN[config.hidden_act]
        else:
            self.activation = nn.Identity()

    def forward(self, x):
        x = nn.functional.group_norm(x, self.num_groups, self.weight, self.bias, self.eps)
        x = self.drop(x)
        x = self.activation(x)
        return x


class DynamicPad2d(nn.Module):
    r"""
    A module that wraps dynamic padding of any input, given the parameters of the convolutional layer and the input
    hidden states.
    """

    def __init__(self, kernel_size, stride, dilation, value=0):
        super().__init__()
        # Safety checkers
        if isinstance(kernel_size, int):
            kernel_size = (kernel_size, kernel_size)

        if isinstance(stride, int):
            stride = (stride, stride)

        if isinstance(dilation, int):
            dilation = (dilation, dilation)

        self.kernel_size = kernel_size
        self.stride = stride
        self.dilation = dilation
        self.value = value

        def compute_padding(x, kernel_size, stride, dilation):
            return max((math.ceil(x / stride) - 1) * stride + (kernel_size - 1) * dilation + 1 - x, 0)

        self.compute_padding = compute_padding

    def __call__(self, input):
        # Get width and height
        input_height, input_width = input.size()[-2:]

        # Compute the padding values
        padding_height = self.compute_padding(input_height, self.kernel_size[0], self.stride[0], self.dilation[0])
        padding_width = self.compute_padding(input_width, self.kernel_size[1], self.stride[1], self.dilation[1])

        # apply pad
        if padding_height > 0 or padding_width > 0:
            input = nn.functional.pad(
                input,
                [
                    padding_width // 2,
                    padding_width - padding_width // 2,
                    padding_height // 2,
                    padding_height - padding_height // 2,
                ],
                value=self.value,
            )
        return input


class BitMaxPool2d(nn.MaxPool2d):
    """Tensorflow like 'SAME' wrapper for 2D max pooling"""

    def __init__(
        self,
        kernel_size: int,
        stride=None,
        dilation=1,
        ceil_mode=False,
        padding=(0, 0),
        padding_value=0,
        use_dynamic_padding=True,
    ):
        kernel_size = kernel_size if isinstance(kernel_size, collections.abc.Iterable) else (kernel_size, kernel_size)
        stride = stride if isinstance(stride, collections.abc.Iterable) else (stride, stride)
        dilation = dilation if isinstance(dilation, collections.abc.Iterable) else (dilation, dilation)
        super(BitMaxPool2d, self).__init__(kernel_size, stride, padding, dilation, ceil_mode)
        if use_dynamic_padding:
            self.pad = DynamicPad2d(kernel_size, stride, dilation, padding_value)
        else:
            self.pad = nn.Identity()

    def forward(self, x):
        x = self.pad(x)
        return nn.functional.max_pool2d(x, self.kernel_size, self.stride, self.padding, self.dilation, self.ceil_mode)


class BitEmbeddings(nn.Module):
    """
    BiT Embeddings (stem) composed of a single aggressive convolution.
    """

    def __init__(self, config: BitConfig, conv_layer: nn.Module):
        super().__init__()

        self.convolution = conv_layer(config.num_channels, config.embedding_size, kernel_size=7, stride=2)

        self.pooler = BitMaxPool2d(kernel_size=3, stride=2, use_dynamic_padding=config.embedding_dynamic_padding)

        # Use the same padding strategy as convolutional layers
        if config.global_padding is not None and config.global_padding.upper() == "SAME":
            self.pad = nn.Identity()
        else:
            self.pad = nn.ConstantPad2d(padding=(1, 1, 1, 1), value=0.0)

        if not config.layer_type == "preactivation":
            self.norm = partial(BitGroupNormActivation, config=config)(num_channels=config.embedding_size)
        else:
            self.norm = nn.Identity()

        self.num_channels = config.num_channels

    def forward(self, pixel_values: Tensor) -> Tensor:
        num_channels = pixel_values.shape[1]
        if num_channels != self.num_channels:
            raise ValueError(
                "Make sure that the channel dimension of the pixel values match with the one set in the configuration."
            )

        # Conv
        embedding = self.convolution(pixel_values)

        # Eventually pad
        embedding = self.pad(embedding)

        # Eventually use BitGroupNorm
        embedding = self.norm(embedding)

        # and pool
        embedding = self.pooler(embedding)

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


def make_div(value, divisor=8):
    min_value = divisor
    new_value = max(min_value, int(value + divisor / 2) // divisor * divisor)
    if new_value < 0.9 * value:
        new_value += divisor
    return new_value


class BitPreActivationBottleneckLayer(nn.Module):
    """Pre-activation (v2) bottleneck block.
    Follows the implementation of "Identity Mappings in Deep Residual Networks":
    https://github.com/KaimingHe/resnet-1k-layers/blob/master/resnet-pre-act.lua

    Except it puts the stride on 3x3 conv when available.
    """

    def __init__(
        self,
        config,
        in_channels,
        out_channels=None,
        bottle_ratio=0.25,
        stride=1,
        dilation=1,
        first_dilation=None,
        groups=1,
        drop_path_rate=0.0,
        is_first_layer=False,
        use_activation=False,
    ):
        super().__init__()

        first_dilation = first_dilation or dilation

        conv_layer = partial(WeightStandardizedConv2d, eps=1e-8, padding=config.global_padding)

        norm_layer = partial(BitGroupNormActivation, config=config)

        out_channels = out_channels or in_channels
        mid_channels = make_div(out_channels * bottle_ratio)

        if is_first_layer:
            self.downsample = BitDownsampleConv(
                in_channels,
                out_channels,
                stride=stride,
                preact=True,
                conv_layer=conv_layer,
                norm_layer=norm_layer,
            )
        else:
            self.downsample = None

        self.norm1 = norm_layer(num_channels=in_channels)
        self.conv1 = conv_layer(in_channels, mid_channels, 1)

        self.norm2 = norm_layer(num_channels=mid_channels)
        self.conv2 = conv_layer(mid_channels, mid_channels, 3, stride=stride, dilation=first_dilation, groups=groups)

        self.norm3 = norm_layer(num_channels=mid_channels)
        self.conv3 = conv_layer(mid_channels, out_channels, 1)

        self.drop_path = BitDropPath(drop_path_rate) if drop_path_rate > 0 else nn.Identity()

        if use_activation:
            self.activation = ACT2FN[config.hidden_act]
        else:
            self.activation = nn.Identity()

    def forward(self, x):
        x_preact = self.norm1(x)

        # shortcut branch
        shortcut = x
        if self.downsample is not None:
            shortcut = self.downsample(x_preact)

        # residual branch
        x = self.conv1(x_preact)
        x = self.conv2(self.norm2(x))
        x = self.conv3(self.norm3(x))
        x = self.drop_path(x)
        return self.activation(x + shortcut)


class BitBottleneckLayer(nn.Module):
    """Non Pre-activation bottleneck block, equivalent to V1.5/V1b bottleneck. Used for ViT."""

    def __init__(
        self,
        config,
        in_channels,
        out_channels=None,
        bottle_ratio=0.25,
        stride=1,
        dilation=1,
        first_dilation=None,
        groups=1,
        drop_path_rate=0.0,
        is_first_layer=False,
    ):
        super().__init__()
        first_dilation = first_dilation or dilation

        conv_layer = partial(WeightStandardizedConv2d, eps=1e-8, padding=config.global_padding)

        norm_layer = partial(BitGroupNormActivation, config=config)
        out_channels = out_channels or in_channels
        mid_chs = make_div(out_channels * bottle_ratio)

        if is_first_layer:
            self.downsample = BitDownsampleConv(
                in_channels,
                out_channels,
                stride=stride,
                preact=False,
                conv_layer=conv_layer,
                norm_layer=norm_layer,
            )
        else:
            self.downsample = None

        self.conv1 = conv_layer(in_channels, mid_chs, 1)
        self.norm1 = norm_layer(num_channels=mid_chs)
        self.conv2 = conv_layer(mid_chs, mid_chs, 3, stride=stride, dilation=first_dilation, groups=groups)
        self.norm2 = norm_layer(num_channels=mid_chs)
        self.conv3 = conv_layer(mid_chs, out_channels, 1)
        self.norm3 = norm_layer(num_channels=out_channels, apply_act=False)
        self.drop_path = BitDropPath(drop_path_rate) if drop_path_rate > 0 else nn.Identity()

        self.activation = ACT2FN[config.hidden_act]

    def forward(self, x):
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
        x = self.activation(x + shortcut)
        return x


class BitDownsampleConv(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        stride=1,
        preact=True,
        conv_layer=None,
        norm_layer=None,
    ):
        super(BitDownsampleConv, self).__init__()
        self.conv_layer = conv_layer
        self.conv = conv_layer(in_channels, out_channels, 1, stride=stride)
        self.norm = nn.Identity() if preact else norm_layer(num_channels=out_channels, apply_act=False)

    def forward(self, x):
        return self.norm(self.conv(x))


class BitStage(nn.Module):
    """
    A ResNet v2 stage composed by stacked layers.
    """

    def __init__(
        self,
        config,
        in_channels,
        out_channels,
        stride,
        dilation,
        depth,
        bottle_ratio=0.25,
        layer_dropout=None,
    ):
        super().__init__()

        first_dilation = 1 if dilation in (1, 2) else 2

        # Get the layer type
        if config.layer_type == "bottleneck":
            layer_fn = partial(BitBottleneckLayer)
        else:
            layer_fn = partial(BitPreActivationBottleneckLayer, use_activation=False)

        prev_chs = in_channels
        self.layers = nn.Sequential()
        for layer_idx in range(depth):
            # Get the current hyper-parameters
            stride, drop_path_rate, is_first_layer = self._get_updated_hyperparameters(
                layer_idx, stride, layer_dropout
            )

            self.layers.add_module(
                str(layer_idx),
                layer_fn(
                    config,
                    prev_chs,
                    out_channels,
                    stride=stride,
                    dilation=dilation,
                    bottle_ratio=bottle_ratio,
                    first_dilation=first_dilation,
                    drop_path_rate=drop_path_rate,
                    is_first_layer=is_first_layer,
                ),
            )
            prev_chs = out_channels
            first_dilation = dilation

    def _get_updated_hyperparameters(self, layer_idx, stride, layer_dropout):
        r"""
        Get the new hyper-parameters with respect to the previous ones and the index of the current layer.
        """
        if layer_dropout:
            drop_path_rate = layer_dropout[layer_idx]
        else:
            drop_path_rate = 0.0

        if layer_idx != 0:
            stride = 1

        is_first_layer = layer_idx == 0

        return stride, drop_path_rate, is_first_layer

    def forward(self, input: Tensor) -> Tensor:
        hidden_state = input
        for _, layer in enumerate(self.layers):
            hidden_state = layer(hidden_state)
        return hidden_state


class BitEncoder(nn.Module):
    def __init__(self, config: BitConfig, conv_layer: nn.Module):
        super().__init__()
        self.stages = nn.ModuleList([])

        prev_chs = config.embedding_size

        # These needs to stay hardcoded
        current_stride = 4
        dilation = 1

        layer_dropouts = [
            x.tolist() for x in torch.linspace(0, config.drop_path_rate, sum(config.depths)).split(config.depths)
        ]

        for stage_idx, (current_depth, current_hidden_size, layer_dropout) in enumerate(
            zip(config.depths, config.hidden_sizes, layer_dropouts)
        ):
            # Get the updated hyper params
            out_channels, stride, dilation = self._get_updated_hyperparameters(
                stage_idx, current_stride, current_hidden_size, dilation, config
            )

            stage = BitStage(
                config,
                prev_chs,
                out_channels,
                stride=stride,
                dilation=dilation,
                depth=current_depth,
                layer_dropout=layer_dropout,
            )

            prev_chs = out_channels
            current_stride *= stride

            self.stages.add_module(str(stage_idx), stage)

    def _get_updated_hyperparameters(self, stage_idx, current_stride, current_hidden_size, dilation, config):
        out_channels = make_div(current_hidden_size * config.width_factor)
        stride = 1 if stage_idx == 0 else 2
        if current_stride >= config.output_stride:
            dilation *= stride
            stride = 1
        return out_channels, stride, dilation

    def forward(
        self, hidden_state: Tensor, output_hidden_states: bool = False, return_dict: bool = True
    ) -> BaseModelOutputWithNoAttention:
        hidden_states = () if output_hidden_states else None

        for stage_module in self.stages:
            if output_hidden_states:
                hidden_states = hidden_states + (hidden_state,)

            hidden_state = stage_module(hidden_state)

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
    base_model_prefix = "bit"
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

        conv_layer = partial(WeightStandardizedConv2d, eps=1e-8, padding=config.global_padding)

        self.embedder = BitEmbeddings(config, conv_layer)

        self.encoder = BitEncoder(config, conv_layer)
        norm_layer = BitGroupNormActivation
        self.norm = (
            norm_layer(config, num_channels=config.hidden_sizes[-1])
            if config.layer_type == "preactivation"
            else nn.Identity()
        )

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
        self.bit = BitModel(config)
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

        outputs = self.bit(pixel_values, output_hidden_states=output_hidden_states, return_dict=return_dict)

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
        self.bit = BitModel(config)

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

        >>> processor = AutoImageProcessor.from_pretrained("google/resnetnv2-50")
        >>> model = AutoBackbone.from_pretrained("google/resnetnv2-50")

        >>> inputs = processor(image, return_tensors="pt")
        >>> outputs = model(**inputs)
        ```"""
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )

        outputs = self.bit(pixel_values, output_hidden_states=True, return_dict=True)

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