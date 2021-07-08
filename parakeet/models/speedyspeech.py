# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import math

import numpy as np
import paddle
from paddle import Tensor
from paddle import nn
from paddle.nn import functional as F
from paddle.nn import initializer as I

from parakeet.modules.positional_encoding import sinusoid_position_encoding
from parakeet.modules.expansion import expand


class ResidualBlock(nn.Layer):
    def __init__(self, channels, kernel_size, dilation, n=2):
        super().__init__()
        blocks = [
            nn.Sequential(
                nn.Conv1D(
                    channels,
                    channels,
                    kernel_size,
                    dilation=dilation,
                    padding="same",
                    data_format="NLC"),
                nn.ReLU(),
                nn.BatchNorm1D(
                    channels, data_format="NLC"), ) for _ in range(n)
        ]
        self.blocks = nn.Sequential(*blocks)

    def forward(self, x):
        return x + self.blocks(x)


class TextEmbedding(nn.Layer):
    def __init__(self,
                 vocab_size: int,
                 embedding_size: int,
                 tone_vocab_size: int=None,
                 tone_embedding_size: int=None,
                 padding_idx: int=None,
                 tone_padding_idx: int=None,
                 concat: bool=False):
        super().__init__()
        self.text_embedding = nn.Embedding(vocab_size, embedding_size,
                                           padding_idx)
        if tone_vocab_size:
            tone_embedding_size = tone_embedding_size or embedding_size
            if tone_embedding_size != embedding_size and not concat:
                raise ValueError(
                    "embedding size != tone_embedding size, only conat is avaiable."
                )
            self.tone_embedding = nn.Embedding(
                tone_vocab_size, tone_embedding_size, tone_padding_idx)
        self.concat = concat

    def forward(self, text, tone=None):
        text_embed = self.text_embedding(text)
        if tone is None:
            return text_embed
        tone_embed = self.tone_embedding(tone)
        if self.concat:
            embed = paddle.concat([text_embed, tone_embed], -1)
        else:
            embed = text_embed + tone_embed
        return embed


class SpeedySpeechEncoder(nn.Layer):
    def __init__(self, vocab_size, tone_size, hidden_size, kernel_size,
                 dilations):
        super().__init__()
        self.embedding = TextEmbedding(
            vocab_size,
            hidden_size,
            tone_size,
            padding_idx=0,
            tone_padding_idx=0)
        self.prenet = nn.Sequential(
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(), )
        res_blocks = [
            ResidualBlock(
                hidden_size, kernel_size, d, n=2) for d in dilations
        ]
        self.res_blocks = nn.Sequential(*res_blocks)

        self.postnet1 = nn.Sequential(nn.Linear(hidden_size, hidden_size))
        self.postnet2 = nn.Sequential(
            nn.ReLU(),
            nn.BatchNorm1D(
                hidden_size, data_format="NLC"),
            nn.Linear(hidden_size, hidden_size), )

    def forward(self, text, tones):
        embedding = self.embedding(text, tones)
        embedding = self.prenet(embedding)
        x = self.res_blocks(embedding)
        x = embedding + self.postnet1(x)
        x = self.postnet2(x)
        return x


class DurationPredictor(nn.Layer):
    def __init__(self, hidden_size):
        super().__init__()
        self.layers = nn.Sequential(
            ResidualBlock(
                hidden_size, 4, 1, n=1),
            ResidualBlock(
                hidden_size, 3, 1, n=1),
            ResidualBlock(
                hidden_size, 1, 1, n=1),
            nn.Linear(hidden_size, 1))

    def forward(self, x):
        return paddle.squeeze(self.layers(x), -1)


class SpeedySpeechDecoder(nn.Layer):
    def __init__(self, hidden_size, output_size, kernel_size, dilations):
        super().__init__()
        res_blocks = [
            ResidualBlock(
                hidden_size, kernel_size, d, n=2) for d in dilations
        ]
        self.res_blocks = nn.Sequential(*res_blocks)

        self.postnet1 = nn.Sequential(nn.Linear(hidden_size, hidden_size))
        self.postnet2 = nn.Sequential(
            ResidualBlock(
                hidden_size, kernel_size, 1, n=2),
            nn.Linear(hidden_size, output_size))

    def forward(self, x):
        xx = self.res_blocks(x)
        x = x + self.postnet1(xx)
        x = self.postnet2(x)
        return x


class SpeedySpeech(nn.Layer):
    def __init__(
            self,
            vocab_size,
            encoder_hidden_size,
            encoder_kernel_size,
            encoder_dilations,
            duration_predictor_hidden_size,
            decoder_hidden_size,
            decoder_output_size,
            decoder_kernel_size,
            decoder_dilations,
            tone_size=None, ):
        super().__init__()
        encoder = SpeedySpeechEncoder(vocab_size, tone_size,
                                      encoder_hidden_size, encoder_kernel_size,
                                      encoder_dilations)
        duration_predictor = DurationPredictor(duration_predictor_hidden_size)
        decoder = SpeedySpeechDecoder(decoder_hidden_size, decoder_output_size,
                                      decoder_kernel_size, decoder_dilations)

        self.encoder = encoder
        self.duration_predictor = duration_predictor
        self.decoder = decoder

    def forward(self, text, tones, plens, durations):
        encodings = self.encoder(text, tones)
        pred_durations = self.duration_predictor(encodings.detach())  # (B, T)

        # expand encodings
        durations_to_expand = durations
        encodings = expand(encodings, durations_to_expand)

        # decode
        # remove positional encoding here
        _, t_dec, feature_size = encodings.shpae
        encodings += sinusoid_position_encoding(t_dec, feature_size)
        decoded = self.decoder(encodings)
        return decoded, pred_durations

    def inference(self, text, tones):
        # text: [T]
        # tones: [T]
        text = text.unsqueeze(0)
        if tones is not None:
            tones = tones.unsqueeze(0)

        encodings = self.encoder(text, tones)
        pred_durations = self.duration_predictor(encodings)  # (1, T)
        durations_to_expand = paddle.round(pred_durations.exp())
        durations_to_expand = (durations_to_expand).astype(paddle.int64)
        encodings = expand(encodings, durations_to_expand)

        shape = paddle.shape(encodings)
        t_dec, feature_size = shape[1], shape[2]
        encodings += sinusoid_position_encoding(t_dec, feature_size)
        decoded = self.decoder(encodings)
        return decoded, pred_durations