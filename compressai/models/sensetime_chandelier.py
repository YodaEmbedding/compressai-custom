import math

import torch
import torch.nn as nn

from torch import Tensor

from compressai.entropy_models import EntropyBottleneck, GaussianConditional
from compressai.layers import AttentionBlock, conv3x3
from compressai.ops import quantize_ste
from compressai.registry import register_model

from .base import CompressionModel
from .utils import conv, deconv, update_registered_buffers

# From Balle's tensorflow compression examples
SCALES_MIN = 0.11
SCALES_MAX = 256
SCALES_LEVELS = 64


def get_scale_table(min=SCALES_MIN, max=SCALES_MAX, levels=SCALES_LEVELS):
    return torch.exp(torch.linspace(math.log(min), math.log(max), levels))


def conv1x1(in_ch: int, out_ch: int, stride: int = 1) -> nn.Module:
    """1x1 convolution."""
    return nn.Conv2d(in_ch, out_ch, kernel_size=1, stride=stride)


class CheckboardMaskedConv2d(nn.Conv2d):
    """
    if kernel_size == (5, 5)
    then mask:
        [[0., 1., 0., 1., 0.],
        [1., 0., 1., 0., 1.],
        [0., 1., 0., 1., 0.],
        [1., 0., 1., 0., 1.],
        [0., 1., 0., 1., 0.]]
    0: non-anchor
    1: anchor
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.register_buffer("mask", torch.zeros_like(self.weight.data))

        self.mask[:, :, 0::2, 1::2] = 1
        self.mask[:, :, 1::2, 0::2] = 1

    def forward(self, x):
        self.weight.data *= self.mask
        out = super().forward(x)

        return out


class ResidualBottleneckBlock(nn.Module):
    """Simple residual block with two 3x3 convolutions.

    Args:
        in_ch (int): number of input channels
        out_ch (int): number of output channels
    """

    def __init__(self, in_ch: int):
        super().__init__()
        self.conv1 = conv1x1(in_ch, in_ch // 2)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(in_ch // 2, in_ch // 2)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = conv1x1(in_ch // 2, in_ch)

    def forward(self, x: Tensor) -> Tensor:
        identity = x

        out = self.conv1(x)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.relu2(out)
        out = self.conv3(out)

        out = out + identity
        return out


class Quantizer:
    def quantize(self, inputs, quantize_type="noise"):
        if quantize_type == "noise":
            half = float(0.5)
            noise = torch.empty_like(inputs).uniform_(-half, half)
            inputs = inputs + noise
            return inputs
        elif quantize_type == "ste":
            return torch.round(inputs) - inputs.detach() + inputs
        else:
            return torch.round(inputs)


@register_model("cheng2020-anchor-elic-chandelier")
class TestModel(CompressionModel):
    def __init__(
        self,
        N=192,
        M=320,
        num_slices=5,
        groups=[0, 16, 16, 32, 64, 192],
        **kwargs,
    ):
        super().__init__()
        self.N = int(N)
        self.M = int(M)
        self.num_slices = num_slices

        """
             N: channel number of main network
             M: channnel number of latent space
        """
        self.groups = groups
        self.g_a = nn.Sequential(
            conv(3, N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            conv(N, N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            AttentionBlock(N),
            conv(N, N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            conv(N, M),
            AttentionBlock(M),
        )

        self.g_s = nn.Sequential(
            AttentionBlock(M),
            deconv(M, N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            deconv(N, N),
            AttentionBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            deconv(N, N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            ResidualBottleneckBlock(N),
            deconv(N, 3),
        )

        self.h_a = nn.Sequential(
            conv3x3(M, N),
            nn.ReLU(inplace=True),
            conv(N, N),
            nn.ReLU(inplace=True),
            conv(N, N),
        )

        self.h_s = nn.Sequential(
            deconv(N, N),
            nn.ReLU(inplace=True),
            deconv(N, N * 3 // 2),
            nn.ReLU(inplace=True),
            conv3x3(N * 3 // 2, 2 * M),
        )

        self.cc_transforms = nn.ModuleList(
            nn.Sequential(
                conv(
                    self.groups[min(1, i) if i > 0 else 0]
                    + self.groups[i if i > 1 else 0],
                    224,
                    stride=1,
                    kernel_size=5,
                ),
                nn.ReLU(inplace=True),
                conv(224, 128, stride=1, kernel_size=5),
                nn.ReLU(inplace=True),
                conv(128, self.groups[i + 1] * 2, stride=1, kernel_size=5),
            )
            for i in range(1, num_slices)
        )  ## from https://github.com/tensorflow/compression/blob/master/models/ms2020.py

        self.context_prediction = nn.ModuleList(
            CheckboardMaskedConv2d(
                self.groups[i + 1],
                2 * self.groups[i + 1],
                kernel_size=5,
                padding=2,
                stride=1,
            )
            for i in range(num_slices)
        )  ## from https://github.com/JiangWeibeta/Checkerboard-Context-Model-for-Efficient-Learned-Image-Compression/blob/main/version2/layers/CheckerboardContext.py

        self.ParamAggregation = nn.ModuleList(
            nn.Sequential(
                conv1x1(
                    M * 2
                    + self.groups[i + 1 if i > 0 else 0] * 2
                    + self.groups[i + 1] * 2,
                    M * 2,
                ),
                nn.ReLU(inplace=True),
                conv1x1(M * 2, 512),
                nn.ReLU(inplace=True),
                conv1x1(512, self.groups[i + 1] * 2),
            )
            for i in range(num_slices)
        )  ##from checkboard "Checkerboard Context Model for Efficient Learned Image Compression"" gep网络参数

        self.quantizer = Quantizer()

        self.gaussian_conditional = GaussianConditional(None)

        self.entropy_bottleneck = EntropyBottleneck(N)

    @property
    def downsampling_factor(self) -> int:
        return 2 ** (4 + 2)

    def init_weights(self):
        for m in self.modules():
            if isinstance(m, (nn.Conv2d, nn.ConvTranspose2d)):
                nn.init.kaiming_normal_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

    def forward(self, x, noisequant=False):
        y = self.g_a(x)
        B, C, H, W = y.shape

        z = self.h_a(y)
        z_hat, z_likelihoods = self.entropy_bottleneck(z)
        if not noisequant:
            z_offset = self.entropy_bottleneck._get_medians()
            z_tmp = z - z_offset
            z_hat = quantize_ste(z_tmp) + z_offset

        latent_means, latent_scales = self.h_s(z_hat).chunk(2, 1)

        anchor, non_anchor = self._unembed(y)

        anchor_split = torch.split(anchor, self.groups[1:], 1)
        non_anchor_split = torch.split(non_anchor, self.groups[1:], 1)

        y_slices = torch.split(y, self.groups[1:], 1)
        ctx_params_anchor_split = torch.split(
            torch.zeros(B, C * 2, H, W).to(x.device),
            [2 * i for i in self.groups[1:]],
            1,
        )

        y_likelihood = []
        y_hat_slices = []
        y_hat_slices_for_gs = []
        for slice_index, y_slice in enumerate(y_slices):
            support = self._calculate_support(
                slice_index, y_hat_slices, latent_means, latent_scales
            )

            y_anchor = anchor_split[slice_index]
            y_non_anchor = non_anchor_split[slice_index]
            scales_hat_split = torch.zeros_like(y_anchor).to(x.device)
            means_hat_split = torch.zeros_like(y_anchor).to(x.device)

            y_hat_i, y_hat_for_gs_i = self._checkerboard_forward(
                [y_anchor, y_non_anchor],
                slice_index,
                support,
                means_hat_split,
                scales_hat_split,
                ctx_params_anchor_split,
                noisequant=noisequant,
            )

            y_hat_slices.append(y_hat_i)
            y_hat_slices_for_gs.append(y_hat_for_gs_i)

            # entropy estimation
            _, y_slice_likelihood = self.gaussian_conditional(
                y_slice, scales_hat_split, means=means_hat_split
            )
            y_likelihood.append(y_slice_likelihood)

        y_likelihoods = torch.cat(y_likelihood, dim=1)
        """
        use STE(y) as the input of synthesizer
        """
        y_hat = torch.cat(y_hat_slices_for_gs, dim=1)
        x_hat = self.g_s(y_hat)

        return {
            "x_hat": x_hat,
            "likelihoods": {"y": y_likelihoods, "z": z_likelihoods},
        }

    def _checkerboard_forward(
        self,
        y_input,
        slice_index,
        support,
        means,
        scales,
        ctx_params_anchor_split,
        noisequant,
    ):
        y_anchor, y_non_anchor = y_input

        y_anchor_hat, y_anchor_hat_for_gs = self._checkerboard_forward_step(
            y_anchor,
            slice_index,
            support,
            means,
            scales,
            ctx_params=ctx_params_anchor_split[slice_index],
            noisequant=noisequant,
            mode="anchor",
        )

        y_non_anchor_hat, y_non_anchor_hat_for_gs = self._checkerboard_forward_step(
            y_non_anchor,
            slice_index,
            support,
            means,
            scales,
            ctx_params=self.context_prediction[slice_index](y_anchor_hat),
            noisequant=noisequant,
            mode="non_anchor",
        )

        y_hat = y_anchor_hat + y_non_anchor_hat
        y_hat_for_gs = y_anchor_hat_for_gs + y_non_anchor_hat_for_gs

        return y_hat, y_hat_for_gs

    def _checkerboard_forward_step(
        self, y, slice_index, support, means, scales, ctx_params, noisequant, mode
    ):
        means_new, scales_new = self.ParamAggregation[slice_index](
            torch.concat([ctx_params, support], dim=1)
        ).chunk(2, 1)

        if mode == "anchor":
            means[:, :, 0::2, 0::2] = means_new[:, :, 0::2, 0::2]
            means[:, :, 1::2, 1::2] = means_new[:, :, 1::2, 1::2]
            scales[:, :, 0::2, 0::2] = scales_new[:, :, 0::2, 0::2]
            scales[:, :, 1::2, 1::2] = scales_new[:, :, 1::2, 1::2]
        elif mode == "non_anchor":
            means[:, :, 0::2, 1::2] = means_new[:, :, 0::2, 1::2]
            means[:, :, 1::2, 0::2] = means_new[:, :, 1::2, 0::2]
            scales[:, :, 0::2, 1::2] = scales_new[:, :, 0::2, 1::2]
            scales[:, :, 1::2, 0::2] = scales_new[:, :, 1::2, 0::2]

        y_hat, y_hat_for_gs = self._apply_quantizer(y, means_new, noisequant)

        if mode == "anchor":
            y_hat[:, :, 0::2, 1::2] = 0
            y_hat[:, :, 1::2, 0::2] = 0
            y_hat_for_gs[:, :, 0::2, 1::2] = 0
            y_hat_for_gs[:, :, 1::2, 0::2] = 0
        elif mode == "non_anchor":
            y_hat[:, :, 0::2, 0::2] = 0
            y_hat[:, :, 1::2, 1::2] = 0
            y_hat_for_gs[:, :, 0::2, 0::2] = 0
            y_hat_for_gs[:, :, 1::2, 1::2] = 0

        return y_hat, y_hat_for_gs

    # def load_state_dict(self, state_dict):
    #     update_registered_buffers(
    #         self.gaussian_conditional,
    #         "gaussian_conditional",
    #         ["_quantized_cdf", "_offset", "_cdf_length", "scale_table"],
    #         state_dict,
    #     )
    #     return super().load_state_dict(state_dict)

    @classmethod
    def from_state_dict(cls, state_dict):
        """Return a new model instance from `state_dict`."""
        net = cls()
        net.load_state_dict(state_dict)
        return net

    def update(self, scale_table=None, force=False):
        if scale_table is None:
            scale_table = get_scale_table()
        updated = self.gaussian_conditional.update_scale_table(scale_table, force=force)
        updated |= super().update(force=force)
        return updated

    def compress(self, x):
        import time

        device = x.device

        y_enc_start = time.time()
        y = self.g_a(x)
        y_enc = time.time() - y_enc_start
        B, C, H, W = y.size()  ## The shape of y to generate the mask

        z_enc_start = time.time()
        z = self.h_a(y)
        z_enc = time.time() - z_enc_start
        z_strings = self.entropy_bottleneck.compress(z)
        z_hat = self.entropy_bottleneck.decompress(z_strings, z.size()[-2:])

        z_dec_start = time.time()
        latent_means, latent_scales = self.h_s(z_hat).chunk(2, 1)
        z_dec = time.time() - z_dec_start

        y_slices = torch.split(y, self.groups[1:], 1)
        ctx_params_anchor_split = torch.split(
            torch.zeros(B, C * 2, H, W).to(x.device),
            [2 * i for i in self.groups[1:]],
            1,
        )

        y_strings = []
        y_hat_slices = []
        params_start = time.time()
        for slice_index, y_slice in enumerate(y_slices):
            support = self._calculate_support(
                slice_index, y_hat_slices, latent_means, latent_scales
            )

            anchor_strings, y_anchor_decode = self._checkerboard_codec_step(
                y_slices[slice_index].clone(),
                slice_index,
                support,
                ctx_params=ctx_params_anchor_split[slice_index],
                device=device,
                mode="compress,anchor",
            )

            non_anchor_strings, y_non_anchor_decode = self._checkerboard_codec_step(
                y_slices[slice_index].clone(),
                slice_index,
                support,
                ctx_params=self.context_prediction[slice_index](y_anchor_decode),
                device=device,
                mode="compress,non_anchor",
            )

            y_hat_slices.append(y_anchor_decode + y_non_anchor_decode)
            y_strings.append([anchor_strings, non_anchor_strings])

        # Flatten for interface compatibility:
        # strings = [y_strings, z_strings]
        y_strings_flat = [x for xs in y_strings for x in xs]
        strings = [*y_strings_flat, z_strings]

        params_time = time.time() - params_start
        return {
            "strings": strings,
            "shape": z.size()[-2:],
            "time": {
                "y_enc": y_enc,
                "z_enc": z_enc,
                "z_dec": z_dec,
                "params": params_time,
            },
        }

    def _checkerboard_codec_step(
        self, y_input, slice_index, support, ctx_params, device, mode
    ):
        [mode_codec, mode_step] = mode.split(",")

        means, scales = self.ParamAggregation[slice_index](
            torch.concat([ctx_params, support], dim=1)
        ).chunk(2, 1)

        decode_shape = means.shape
        B, C, H, W = decode_shape
        encode_shape = (B, C, H, W // 2)

        means_encode = torch.zeros(encode_shape).to(device)
        scales_encode = torch.zeros(encode_shape).to(device)

        if mode_step == "anchor":
            means_encode[:, :, 0::2, :] = means[:, :, 0::2, 0::2]
            means_encode[:, :, 1::2, :] = means[:, :, 1::2, 1::2]
            scales_encode[:, :, 0::2, :] = scales[:, :, 0::2, 0::2]
            scales_encode[:, :, 1::2, :] = scales[:, :, 1::2, 1::2]
        elif mode_step == "non_anchor":
            means_encode[:, :, 0::2, :] = means[:, :, 0::2, 1::2]
            means_encode[:, :, 1::2, :] = means[:, :, 1::2, 0::2]
            scales_encode[:, :, 0::2, :] = scales[:, :, 0::2, 1::2]
            scales_encode[:, :, 1::2, :] = scales[:, :, 1::2, 0::2]

        indexes = self.gaussian_conditional.build_indexes(scales_encode)

        if mode_codec == "compress":
            y = y_input
            y_encode = torch.zeros(encode_shape).to(device)

            if mode_step == "anchor":
                y_encode[:, :, 0::2, :] = y[:, :, 0::2, 0::2]
                y_encode[:, :, 1::2, :] = y[:, :, 1::2, 1::2]
            elif mode_step == "non_anchor":
                y_encode[:, :, 0::2, :] = y[:, :, 0::2, 1::2]
                y_encode[:, :, 1::2, :] = y[:, :, 1::2, 0::2]

            strings = self.gaussian_conditional.compress(
                y_encode, indexes, means=means_encode
            )

        elif mode_codec == "decompress":
            strings = y_input

        quantized = self.gaussian_conditional.decompress(
            strings, indexes, means=means_encode
        )

        y_decode = torch.zeros(decode_shape).to(device)

        if mode_step == "anchor":
            y_decode[:, :, 0::2, 0::2] = quantized[:, :, 0::2, :]
            y_decode[:, :, 1::2, 1::2] = quantized[:, :, 1::2, :]
        elif mode_step == "non_anchor":
            y_decode[:, :, 0::2, 1::2] = quantized[:, :, 0::2, :]
            y_decode[:, :, 1::2, 0::2] = quantized[:, :, 1::2, :]

        return strings, y_decode

    def decompress(self, strings, shape, **kwargs):
        # Interface compatibility (strings should be list[list[str]]):
        assert isinstance(strings, list)
        [*y_strings_flat, z_strings] = strings
        y_strings = [
            y_strings_flat[i : i + 2] for i in range(0, len(y_strings_flat), 2)
        ]
        strings = [y_strings, z_strings]

        assert isinstance(strings, list) and len(strings) == 2

        # FIXME: we don't respect the default entropy coder and directly call thse
        # range ANS decoder

        z_hat = self.entropy_bottleneck.decompress(strings[1], shape)
        B, _, _, _ = z_hat.size()

        latent_means, latent_scales = self.h_s(z_hat).chunk(2, 1)

        device = z_hat.device

        y_shape = [z_hat.shape[2] * 4, z_hat.shape[3] * 4]
        y_strings = strings[0]

        ctx_params_anchor = torch.zeros(
            (B, self.M * 2, z_hat.shape[2] * 4, z_hat.shape[3] * 4)
        ).to(device)
        ctx_params_anchor_split = torch.split(
            ctx_params_anchor, [2 * i for i in self.groups[1:]], 1
        )

        y_hat_slices = []
        for slice_index in range(len(self.groups) - 1):
            support = self._calculate_support(
                slice_index, y_hat_slices, latent_means, latent_scales
            )

            _, y_anchor_decode = self._checkerboard_codec_step(
                y_strings[slice_index][0],
                slice_index,
                support,
                ctx_params=ctx_params_anchor_split[slice_index],
                device=device,
                mode="decompress,anchor",
            )

            _, y_non_anchor_decode = self._checkerboard_codec_step(
                y_strings[slice_index][1],
                slice_index,
                support,
                ctx_params=self.context_prediction[slice_index](y_anchor_decode),
                device=device,
                mode="decompress,non_anchor",
            )

            y_hat_slices.append(y_anchor_decode + y_non_anchor_decode)

        y_hat = torch.cat(y_hat_slices, dim=1)

        import time

        y_dec_start = time.time()
        x_hat = self.g_s(y_hat).clamp_(0, 1)
        y_dec = time.time() - y_dec_start

        return {"x_hat": x_hat, "time": {"y_dec": y_dec}}

    def inference(self, x):
        import time

        y_enc_start = time.time()
        y = self.g_a(x)
        y_enc = time.time() - y_enc_start
        B, C, H, W = y.size()  ## The shape of y to generate the mask

        z_enc_start = time.time()
        z = self.h_a(y)
        z_enc = time.time() - z_enc_start
        z_hat, z_likelihoods = self.entropy_bottleneck(z)
        z_offset = self.entropy_bottleneck._get_medians()
        z_tmp = z - z_offset
        z_hat = quantize_ste(z_tmp) + z_offset

        z_dec_start = time.time()
        latent_means, latent_scales = self.h_s(z_hat).chunk(2, 1)
        z_dec = time.time() - z_dec_start

        anchor, non_anchor = self._unembed(y)

        anchor_split = torch.split(anchor, self.groups[1:], 1)
        non_anchor_split = torch.split(non_anchor, self.groups[1:], 1)

        y_slices = torch.split(y, self.groups[1:], 1)
        ctx_params_anchor_split = torch.split(
            torch.zeros(B, C * 2, H, W).to(x.device),
            [2 * i for i in self.groups[1:]],
            1,
        )

        y_likelihood = []
        y_hat_slices = []
        params_start = time.time()
        for slice_index, y_slice in enumerate(y_slices):
            support = self._calculate_support(
                slice_index, y_hat_slices, latent_means, latent_scales
            )
            ### checkboard process 1
            y_anchor = anchor_split[slice_index]
            params = self.ParamAggregation[slice_index](
                torch.concat([ctx_params_anchor_split[slice_index], support], dim=1)
            )
            means_anchor, scales_anchor = params.chunk(2, 1)

            scales_hat_split = torch.zeros_like(y_anchor).to(x.device)
            means_hat_split = torch.zeros_like(y_anchor).to(x.device)

            scales_hat_split[:, :, 0::2, 0::2] = scales_anchor[:, :, 0::2, 0::2]
            scales_hat_split[:, :, 1::2, 1::2] = scales_anchor[:, :, 1::2, 1::2]
            means_hat_split[:, :, 0::2, 0::2] = means_anchor[:, :, 0::2, 0::2]
            means_hat_split[:, :, 1::2, 1::2] = means_anchor[:, :, 1::2, 1::2]

            y_anchor_quantilized_for_gs = (
                self.quantizer.quantize(y_anchor - means_anchor, "ste") + means_anchor
            )

            y_anchor_quantilized_for_gs[:, :, 0::2, 1::2] = 0
            y_anchor_quantilized_for_gs[:, :, 1::2, 0::2] = 0

            ### checkboard process 2
            masked_context = self.context_prediction[slice_index](
                y_anchor_quantilized_for_gs
            )
            params = self.ParamAggregation[slice_index](
                torch.concat([masked_context, support], dim=1)
            )
            means_non_anchor, scales_non_anchor = params.chunk(2, 1)

            scales_hat_split[:, :, 0::2, 1::2] = scales_non_anchor[:, :, 0::2, 1::2]
            scales_hat_split[:, :, 1::2, 0::2] = scales_non_anchor[:, :, 1::2, 0::2]
            means_hat_split[:, :, 0::2, 1::2] = means_non_anchor[:, :, 0::2, 1::2]
            means_hat_split[:, :, 1::2, 0::2] = means_non_anchor[:, :, 1::2, 0::2]
            # entropy estimation
            _, y_slice_likelihood = self.gaussian_conditional(
                y_slice, scales_hat_split, means=means_hat_split
            )

            y_non_anchor = non_anchor_split[slice_index]

            y_non_anchor_quantilized_for_gs = (
                self.quantizer.quantize(y_non_anchor - means_non_anchor, "ste")
                + means_non_anchor
            )
            y_non_anchor_quantilized_for_gs[:, :, 0::2, 0::2] = 0
            y_non_anchor_quantilized_for_gs[:, :, 1::2, 1::2] = 0

            y_hat_slice = y_anchor_quantilized_for_gs + y_non_anchor_quantilized_for_gs
            y_hat_slices.append(y_hat_slice)
            ### ste for synthesis model
            y_likelihood.append(y_slice_likelihood)

        params_time = time.time() - params_start
        y_likelihoods = torch.cat(y_likelihood, dim=1)
        """
        use STE(y) as the input of synthesizer
        """
        y_hat = torch.cat(y_hat_slices, dim=1)
        y_dec_start = time.time()
        x_hat = self.g_s(y_hat)
        y_dec = time.time() - y_dec_start
        return {
            "x_hat": x_hat,
            "likelihoods": {"y": y_likelihoods, "z": z_likelihoods},
            "time": {
                "y_enc": y_enc,
                "y_dec": y_dec,
                "z_enc": z_enc,
                "z_dec": z_dec,
                "params": params_time,
            },
        }

    def _apply_quantizer(self, y, means, noisequant):
        if noisequant:
            quantized = self.quantizer.quantize(y, "noise")
            quantized_for_g_s = self.quantizer.quantize(y, "ste")
        else:
            quantized = self.quantizer.quantize(y - means, "ste") + means
            quantized_for_g_s = self.quantizer.quantize(y - means, "ste") + means
        return quantized, quantized_for_g_s

    def _calculate_support(
        self, slice_index, y_hat_slices, latent_means, latent_scales
    ):
        if slice_index == 0:
            return torch.concat([latent_means, latent_scales], dim=1)

        support_slices_ch = self.cc_transforms[slice_index - 1](
            y_hat_slices[0]
            if slice_index == 1
            else torch.concat([y_hat_slices[0], y_hat_slices[slice_index - 1]], dim=1)
        )
        support_slices_ch_mean, support_slices_ch_scale = support_slices_ch.chunk(2, 1)
        support = [
            support_slices_ch_mean,
            support_slices_ch_scale,
            latent_means,
            latent_scales,
        ]
        return torch.concat(support, dim=1)

    def _unembed(self, y):
        anchor = torch.zeros_like(y).to(y.device)
        non_anchor = torch.zeros_like(y).to(y.device)
        anchor[:, :, 0::2, 0::2] = y[:, :, 0::2, 0::2]
        anchor[:, :, 1::2, 1::2] = y[:, :, 1::2, 1::2]
        non_anchor[:, :, 0::2, 1::2] = y[:, :, 0::2, 1::2]
        non_anchor[:, :, 1::2, 0::2] = y[:, :, 1::2, 0::2]
        return anchor, non_anchor
