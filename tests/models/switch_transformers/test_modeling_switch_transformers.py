# coding=utf-8
# Copyright 2022 Google SwitchTransformers Authors and HuggingFace Inc. team.
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


import copy
import tempfile
import unittest

from transformers import SwitchTransformersConfig, is_torch_available
from transformers.testing_utils import require_tokenizers, require_torch, slow, torch_device

from ...generation.test_generation_utils import GenerationTesterMixin
from ...test_configuration_common import ConfigTester
from ...test_modeling_common import ModelTesterMixin, ids_tensor


if is_torch_available():
    import torch

    from transformers import (
        SwitchTransformersEncoderModel,
        SwitchTransformersForConditionalGeneration,
        SwitchTransformersModel,
    )
    from transformers.models.switch_transformers.modeling_switch_transformers import (
        SWITCH_TRANSFORMERS_PRETRAINED_MODEL_ARCHIVE_LIST,
    )
    from transformers.models.switch_transformers.router import (
        ExpertsChooseMaskedRouter,
        TokensChooseMaskedRouter,
        load_balancing_loss_func,
        router_z_loss_func,
    )


class SwitchTransformersModelTester:
    def __init__(
        self,
        parent,
        vocab_size=99,
        batch_size=13,
        encoder_seq_length=7,
        decoder_seq_length=9,
        # For common tests
        is_training=True,
        use_attention_mask=True,
        use_labels=True,
        hidden_size=32,
        num_hidden_layers=5,
        num_attention_heads=4,
        d_ff=37,
        relative_attention_num_buckets=8,
        dropout_rate=0.1,
        initializer_factor=0.002,
        eos_token_id=1,
        pad_token_id=0,
        decoder_start_token_id=0,
        scope=None,
        decoder_layers=None,
        sparse_step=1,
    ):

        self.parent = parent
        self.batch_size = batch_size
        self.encoder_seq_length = encoder_seq_length
        self.decoder_seq_length = decoder_seq_length
        # For common tests
        self.seq_length = self.decoder_seq_length
        self.is_training = is_training
        self.use_attention_mask = use_attention_mask
        self.use_labels = use_labels
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.d_ff = d_ff
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.dropout_rate = dropout_rate
        self.initializer_factor = initializer_factor
        self.eos_token_id = eos_token_id
        self.pad_token_id = pad_token_id
        self.decoder_start_token_id = decoder_start_token_id
        self.scope = None
        self.decoder_layers = decoder_layers
        self.sparse_step = sparse_step

    def get_large_model_config(self):
        return SwitchTransformersConfig.from_pretrained("switch_transformers-base")

    def prepare_config_and_inputs(self):
        input_ids = ids_tensor([self.batch_size, self.encoder_seq_length], self.vocab_size)
        decoder_input_ids = ids_tensor([self.batch_size, self.decoder_seq_length], self.vocab_size)

        attention_mask = None
        decoder_attention_mask = None
        if self.use_attention_mask:
            attention_mask = ids_tensor([self.batch_size, self.encoder_seq_length], vocab_size=2)
            decoder_attention_mask = ids_tensor([self.batch_size, self.decoder_seq_length], vocab_size=2)

        lm_labels = None
        if self.use_labels:
            lm_labels = ids_tensor([self.batch_size, self.decoder_seq_length], self.vocab_size)

        config = self.get_config()

        return (
            config,
            input_ids,
            decoder_input_ids,
            attention_mask,
            decoder_attention_mask,
            lm_labels,
        )

    def get_pipeline_config(self):
        return SwitchTransformersConfig(
            vocab_size=166,  # switch_transformers forces 100 extra tokens
            d_model=self.hidden_size,
            d_ff=self.d_ff,
            d_kv=self.hidden_size // self.num_attention_heads,
            num_layers=self.num_hidden_layers,
            num_decoder_layers=self.decoder_layers,
            num_heads=self.num_attention_heads,
            relative_attention_num_buckets=self.relative_attention_num_buckets,
            dropout_rate=self.dropout_rate,
            initializer_factor=self.initializer_factor,
            eos_token_id=self.eos_token_id,
            bos_token_id=self.pad_token_id,
            pad_token_id=self.pad_token_id,
            decoder_start_token_id=self.decoder_start_token_id,
        )

    def get_config(self):
        return SwitchTransformersConfig(
            vocab_size=self.vocab_size,
            d_model=self.hidden_size,
            d_ff=self.d_ff,
            d_kv=self.hidden_size // self.num_attention_heads,
            num_layers=self.num_hidden_layers,
            num_decoder_layers=self.decoder_layers,
            num_heads=self.num_attention_heads,
            relative_attention_num_buckets=self.relative_attention_num_buckets,
            dropout_rate=self.dropout_rate,
            initializer_factor=self.initializer_factor,
            eos_token_id=self.eos_token_id,
            bos_token_id=self.pad_token_id,
            pad_token_id=self.pad_token_id,
            decoder_start_token_id=self.decoder_start_token_id,
            sparse_step=self.sparse_step,
        )

    def check_prepare_lm_labels_via_shift_left(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config)
        model.to(torch_device)
        model.eval()

        # make sure that lm_labels are correctly padded from the right
        lm_labels.masked_fill_((lm_labels == self.decoder_start_token_id), self.eos_token_id)

        # add casaul pad token mask
        triangular_mask = torch.tril(lm_labels.new_ones(lm_labels.shape)).logical_not()
        lm_labels.masked_fill_(triangular_mask, self.pad_token_id)
        decoder_input_ids = model._shift_right(lm_labels)

        for i, (decoder_input_ids_slice, lm_labels_slice) in enumerate(zip(decoder_input_ids, lm_labels)):
            # first item
            self.parent.assertEqual(decoder_input_ids_slice[0].item(), self.decoder_start_token_id)
            if i < decoder_input_ids_slice.shape[-1]:
                if i < decoder_input_ids.shape[-1] - 1:
                    # items before diagonal
                    self.parent.assertListEqual(
                        decoder_input_ids_slice[1 : i + 1].tolist(), lm_labels_slice[:i].tolist()
                    )
                # pad items after diagonal
                if i < decoder_input_ids.shape[-1] - 2:
                    self.parent.assertListEqual(
                        decoder_input_ids_slice[i + 2 :].tolist(), lm_labels_slice[i + 1 : -1].tolist()
                    )
            else:
                # all items after square
                self.parent.assertListEqual(decoder_input_ids_slice[1:].tolist(), lm_labels_slice[:-1].tolist())

    def create_and_check_model(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config)
        model.to(torch_device)
        model.eval()
        result = model(
            input_ids=input_ids,
            decoder_input_ids=decoder_input_ids,
            attention_mask=attention_mask,
            decoder_attention_mask=decoder_attention_mask,
        )
        result = model(input_ids=input_ids, decoder_input_ids=decoder_input_ids)
        decoder_output = result.last_hidden_state
        decoder_past = result.past_key_values
        encoder_output = result.encoder_last_hidden_state

        self.parent.assertEqual(encoder_output.size(), (self.batch_size, self.encoder_seq_length, self.hidden_size))
        self.parent.assertEqual(decoder_output.size(), (self.batch_size, self.decoder_seq_length, self.hidden_size))
        # There should be `num_layers` key value embeddings stored in decoder_past
        self.parent.assertEqual(len(decoder_past), config.num_layers)
        # There should be a self attn key, a self attn value, a cross attn key and a cross attn value stored in each decoder_past tuple
        self.parent.assertEqual(len(decoder_past[0]), 4)

    def create_and_check_with_lm_head(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersForConditionalGeneration(config=config).to(torch_device).eval()
        outputs = model(
            input_ids=input_ids,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            labels=lm_labels,
        )
        self.parent.assertEqual(len(outputs), 4)
        self.parent.assertEqual(outputs["logits"].size(), (self.batch_size, self.decoder_seq_length, self.vocab_size))
        self.parent.assertEqual(outputs["loss"].size(), ())

    def create_and_check_decoder_model_past(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config).get_decoder().to(torch_device).eval()
        # first forward pass
        outputs = model(input_ids, use_cache=True)
        outputs_use_cache_conf = model(input_ids)
        outputs_no_past = model(input_ids, use_cache=False)

        self.parent.assertTrue(len(outputs) == len(outputs_use_cache_conf))
        self.parent.assertTrue(len(outputs) == len(outputs_no_past) + 1)

        output, past_key_values = outputs.to_tuple()

        # create hypothetical next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size)

        # append to next input_ids and
        next_input_ids = torch.cat([input_ids, next_tokens], dim=-1)

        output_from_no_past = model(next_input_ids)["last_hidden_state"]
        output_from_past = model(next_tokens, past_key_values=past_key_values)["last_hidden_state"]

        # select random slice
        random_slice_idx = ids_tensor((1,), output_from_past.shape[-1]).item()
        output_from_no_past_slice = output_from_no_past[:, -1, random_slice_idx].detach()
        output_from_past_slice = output_from_past[:, 0, random_slice_idx].detach()

        # test that outputs are equal for slice
        self.parent.assertTrue(torch.allclose(output_from_past_slice, output_from_no_past_slice, atol=1e-3))

    def create_and_check_decoder_model_attention_mask_past(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config).get_decoder()
        model.to(torch_device)
        model.eval()

        # create attention mask
        attn_mask = torch.ones(input_ids.shape, dtype=torch.long, device=torch_device)

        half_seq_length = input_ids.shape[-1] // 2
        attn_mask[:, half_seq_length:] = 0

        # first forward pass
        output, past_key_values = model(input_ids, attention_mask=attn_mask, use_cache=True).to_tuple()

        # create hypothetical next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size)

        # change a random masked slice from input_ids
        random_seq_idx_to_change = ids_tensor((1,), half_seq_length).item() + 1
        random_other_next_tokens = ids_tensor((self.batch_size, 1), config.vocab_size).squeeze(-1)
        input_ids[:, -random_seq_idx_to_change] = random_other_next_tokens

        # append to next input_ids and attn_mask
        next_input_ids = torch.cat([input_ids, next_tokens], dim=-1)
        attn_mask = torch.cat(
            [attn_mask, torch.ones((attn_mask.shape[0], 1), dtype=torch.long, device=torch_device)],
            dim=1,
        )

        # get two different outputs
        output_from_no_past = model(next_input_ids, attention_mask=attn_mask)["last_hidden_state"]
        output_from_past = model(next_tokens, past_key_values=past_key_values, attention_mask=attn_mask)[
            "last_hidden_state"
        ]

        # select random slice
        random_slice_idx = ids_tensor((1,), output_from_past.shape[-1]).item()
        output_from_no_past_slice = output_from_no_past[:, -1, random_slice_idx].detach()
        output_from_past_slice = output_from_past[:, 0, random_slice_idx].detach()

        # test that outputs are equal for slice
        self.parent.assertTrue(torch.allclose(output_from_past_slice, output_from_no_past_slice, atol=1e-3))

    def create_and_check_decoder_model_past_large_inputs(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config).get_decoder().to(torch_device).eval()
        # first forward pass
        outputs = model(input_ids, attention_mask=attention_mask, use_cache=True)

        output, past_key_values = outputs.to_tuple()

        # create hypothetical multiple next token and extent to next_input_ids
        next_tokens = ids_tensor((self.batch_size, 3), config.vocab_size)
        next_mask = ids_tensor((self.batch_size, 3), vocab_size=2)

        # append to next input_ids and
        next_input_ids = torch.cat([input_ids, next_tokens], dim=-1)
        next_attention_mask = torch.cat([attention_mask, next_mask], dim=-1)

        output_from_no_past = model(next_input_ids, attention_mask=next_attention_mask)["last_hidden_state"]
        output_from_past = model(next_tokens, attention_mask=next_attention_mask, past_key_values=past_key_values)[
            "last_hidden_state"
        ]

        # select random slice
        random_slice_idx = ids_tensor((1,), output_from_past.shape[-1]).item()
        output_from_no_past_slice = output_from_no_past[:, -3:, random_slice_idx].detach()
        output_from_past_slice = output_from_past[:, :, random_slice_idx].detach()

        self.parent.assertTrue(output_from_past_slice.shape[1] == next_tokens.shape[1])

        # test that outputs are equal for slice
        self.parent.assertTrue(torch.allclose(output_from_past_slice, output_from_no_past_slice, atol=1e-3))

    def create_and_check_generate_with_past_key_values(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersForConditionalGeneration(config=config).to(torch_device).eval()
        torch.manual_seed(0)
        output_without_past_cache = model.generate(
            input_ids[:1], num_beams=2, max_length=5, do_sample=True, use_cache=False
        )
        torch.manual_seed(0)
        output_with_past_cache = model.generate(input_ids[:1], num_beams=2, max_length=5, do_sample=True)
        self.parent.assertTrue(torch.all(output_with_past_cache == output_without_past_cache))

    def create_and_check_model_fp16_forward(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        model = SwitchTransformersModel(config=config).to(torch_device).half().eval()
        output = model(input_ids, decoder_input_ids=input_ids, attention_mask=attention_mask)["last_hidden_state"]
        self.parent.assertFalse(torch.isnan(output).any().item())

    def create_and_check_encoder_decoder_shared_weights(
        self,
        config,
        input_ids,
        decoder_input_ids,
        attention_mask,
        decoder_attention_mask,
        lm_labels,
    ):
        for model_class in [SwitchTransformersModel, SwitchTransformersForConditionalGeneration]:
            torch.manual_seed(0)
            model = model_class(config=config).to(torch_device).eval()
            # load state dict copies weights but does not tie them
            model.encoder.load_state_dict(model.decoder.state_dict(), strict=False)

            torch.manual_seed(0)
            tied_config = copy.deepcopy(config)
            tied_config.tie_encoder_decoder = True
            tied_model = model_class(config=tied_config).to(torch_device).eval()

            model_result = model(
                input_ids=input_ids,
                decoder_input_ids=decoder_input_ids,
                attention_mask=attention_mask,
                decoder_attention_mask=decoder_attention_mask,
            )

            tied_model_result = tied_model(
                input_ids=input_ids,
                decoder_input_ids=decoder_input_ids,
                attention_mask=attention_mask,
                decoder_attention_mask=decoder_attention_mask,
            )

            # check that models has less parameters
            self.parent.assertLess(
                sum(p.numel() for p in tied_model.parameters()), sum(p.numel() for p in model.parameters())
            )
            random_slice_idx = ids_tensor((1,), model_result[0].shape[-1]).item()

            # check that outputs are equal
            self.parent.assertTrue(
                torch.allclose(
                    model_result[0][0, :, random_slice_idx], tied_model_result[0][0, :, random_slice_idx], atol=1e-4
                )
            )

            # check that outputs after saving and loading are equal
            with tempfile.TemporaryDirectory() as tmpdirname:
                tied_model.save_pretrained(tmpdirname)
                tied_model = model_class.from_pretrained(tmpdirname)
                tied_model.to(torch_device)
                tied_model.eval()

                # check that models has less parameters
                self.parent.assertLess(
                    sum(p.numel() for p in tied_model.parameters()), sum(p.numel() for p in model.parameters())
                )
                random_slice_idx = ids_tensor((1,), model_result[0].shape[-1]).item()

                tied_model_result = tied_model(
                    input_ids=input_ids,
                    decoder_input_ids=decoder_input_ids,
                    attention_mask=attention_mask,
                    decoder_attention_mask=decoder_attention_mask,
                )

                # check that outputs are equal
                self.parent.assertTrue(
                    torch.allclose(
                        model_result[0][0, :, random_slice_idx],
                        tied_model_result[0][0, :, random_slice_idx],
                        atol=1e-4,
                    )
                )

    def check_resize_embeddings_switch_transformers_v1_1(
        self,
        config,
    ):
        prev_vocab_size = config.vocab_size

        config.tie_word_embeddings = False
        model = SwitchTransformersForConditionalGeneration(config=config).to(torch_device).eval()
        model.resize_token_embeddings(prev_vocab_size - 10)

        self.parent.assertEqual(model.get_input_embeddings().weight.shape[0], prev_vocab_size - 10)
        self.parent.assertEqual(model.get_output_embeddings().weight.shape[0], prev_vocab_size - 10)
        self.parent.assertEqual(model.config.vocab_size, prev_vocab_size - 10)

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        (
            config,
            input_ids,
            decoder_input_ids,
            attention_mask,
            decoder_attention_mask,
            lm_labels,
        ) = config_and_inputs

        inputs_dict = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "decoder_input_ids": decoder_input_ids,
            "decoder_attention_mask": decoder_attention_mask,
            "use_cache": False,
        }
        return config, inputs_dict


@require_torch
class SwitchTransformersModelTest(ModelTesterMixin, GenerationTesterMixin, unittest.TestCase):

    all_model_classes = (
        (SwitchTransformersModel, SwitchTransformersForConditionalGeneration) if is_torch_available() else ()
    )
    all_generative_model_classes = (SwitchTransformersForConditionalGeneration,) if is_torch_available() else ()
    all_parallelizable_model_classes = (
        (SwitchTransformersModel, SwitchTransformersForConditionalGeneration) if is_torch_available() else ()
    )
    fx_compatible = False
    test_pruning = False
    test_resize_embeddings = True
    test_model_parallel = True
    is_encoder_decoder = True
    # The small SWITCH_TRANSFORMERS model needs higher percentages for CPU/MP tests
    model_split_percents = [0.8, 0.9]

    def setUp(self):
        self.model_tester = SwitchTransformersModelTester(self)
        self.config_tester = ConfigTester(self, config_class=SwitchTransformersConfig, d_model=37)

    def test_config(self):
        self.config_tester.run_common_tests()

    def test_shift_right(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.check_prepare_lm_labels_via_shift_left(*config_and_inputs)

    def test_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model(*config_and_inputs)

    def test_model_v1_1(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        # check that gated gelu feed forward and different word embeddings work
        config = config_and_inputs[0]
        config.tie_word_embeddings = False
        config.feed_forward_proj = "gated-gelu"
        self.model_tester.create_and_check_model(config, *config_and_inputs[1:])

    def test_config_and_model_silu_gated(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        config = config_and_inputs[0]
        config.feed_forward_proj = "gated-silu"
        self.model_tester.create_and_check_model(*config_and_inputs)

    def test_with_lm_head(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_with_lm_head(*config_and_inputs)

    def test_decoder_model_past(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_decoder_model_past(*config_and_inputs)

    def test_decoder_model_past_with_attn_mask(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_decoder_model_attention_mask_past(*config_and_inputs)

    def test_decoder_model_past_with_3d_attn_mask(self):
        (
            config,
            input_ids,
            decoder_input_ids,
            attention_mask,
            decoder_attention_mask,
            lm_labels,
        ) = self.model_tester.prepare_config_and_inputs()

        attention_mask = ids_tensor(
            [self.model_tester.batch_size, self.model_tester.encoder_seq_length, self.model_tester.encoder_seq_length],
            vocab_size=2,
        )
        decoder_attention_mask = ids_tensor(
            [self.model_tester.batch_size, self.model_tester.decoder_seq_length, self.model_tester.decoder_seq_length],
            vocab_size=2,
        )

        self.model_tester.create_and_check_decoder_model_attention_mask_past(
            config,
            input_ids,
            decoder_input_ids,
            attention_mask,
            decoder_attention_mask,
            lm_labels,
        )

    def test_decoder_model_past_with_large_inputs(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_decoder_model_past_large_inputs(*config_and_inputs)

    def test_generate_with_past_key_values(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_generate_with_past_key_values(*config_and_inputs)

    def test_encoder_decoder_shared_weights(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_encoder_decoder_shared_weights(*config_and_inputs)

    @unittest.skipIf(torch_device == "cpu", "Cant do half precision")
    def test_model_fp16_forward(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model_fp16_forward(*config_and_inputs)

    def test_v1_1_resize_embeddings(self):
        config = self.model_tester.prepare_config_and_inputs()[0]
        self.model_tester.check_resize_embeddings_switch_transformers_v1_1(config)

    @slow
    def test_model_from_pretrained(self):
        for model_name in SWITCH_TRANSFORMERS_PRETRAINED_MODEL_ARCHIVE_LIST[:1]:
            model = SwitchTransformersModel.from_pretrained(model_name)
            self.assertIsNotNone(model)

    @unittest.skip("Test has a segmentation fault on torch 1.8.0")
    def test_export_to_onnx(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        model = SwitchTransformersModel(config_and_inputs[0]).to(torch_device)
        with tempfile.TemporaryDirectory() as tmpdirname:
            torch.onnx.export(
                model,
                (config_and_inputs[1], config_and_inputs[3], config_and_inputs[2]),
                f"{tmpdirname}/switch_transformers_test.onnx",
                export_params=True,
                opset_version=9,
                input_names=["input_ids", "decoder_input_ids"],
            )

    def test_generate_with_head_masking(self):
        attention_names = ["encoder_attentions", "decoder_attentions", "cross_attentions"]
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        config = config_and_inputs[0]
        max_length = config_and_inputs[1].shape[-1] + 3
        model = SwitchTransformersForConditionalGeneration(config).eval()
        model.to(torch_device)

        head_masking = {
            "head_mask": torch.zeros(config.num_layers, config.num_heads, device=torch_device),
            "decoder_head_mask": torch.zeros(config.num_decoder_layers, config.num_heads, device=torch_device),
            "cross_attn_head_mask": torch.zeros(config.num_decoder_layers, config.num_heads, device=torch_device),
        }

        for attn_name, (name, mask) in zip(attention_names, head_masking.items()):
            head_masks = {name: mask}
            # Explicitly pass decoder_head_mask as it is required from SWITCH_TRANSFORMERS model when head_mask specified
            if name == "head_mask":
                head_masks["decoder_head_mask"] = torch.ones(
                    config.num_decoder_layers, config.num_heads, device=torch_device
                )

            out = model.generate(
                config_and_inputs[1],
                num_beams=1,
                max_length=max_length,
                output_attentions=True,
                return_dict_in_generate=True,
                **head_masks,
            )
            # We check the state of decoder_attentions and cross_attentions just from the last step
            attn_weights = out[attn_name] if attn_name == attention_names[0] else out[attn_name][-1]
            self.assertEqual(sum([w.sum().item() for w in attn_weights]), 0.0)

    @unittest.skip("Does not work on the tiny model as we keep hitting edge cases.")
    def test_disk_offload(self):
        pass


class SwitchTransformersEncoderOnlyModelTester:
    def __init__(
        self,
        parent,
        vocab_size=99,
        batch_size=13,
        encoder_seq_length=7,
        # For common tests
        use_attention_mask=True,
        hidden_size=32,
        num_hidden_layers=5,
        num_attention_heads=4,
        d_ff=37,
        relative_attention_num_buckets=8,
        is_training=False,
        dropout_rate=0.1,
        initializer_factor=0.002,
        is_encoder_decoder=False,
        eos_token_id=1,
        pad_token_id=0,
        scope=None,
    ):

        self.parent = parent
        self.batch_size = batch_size
        self.encoder_seq_length = encoder_seq_length
        # For common tests
        self.seq_length = self.encoder_seq_length
        self.use_attention_mask = use_attention_mask
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.d_ff = d_ff
        self.relative_attention_num_buckets = relative_attention_num_buckets
        self.dropout_rate = dropout_rate
        self.initializer_factor = initializer_factor
        self.eos_token_id = eos_token_id
        self.pad_token_id = pad_token_id
        self.is_encoder_decoder = is_encoder_decoder
        self.scope = None
        self.is_training = is_training

    def get_large_model_config(self):
        return SwitchTransformersConfig.from_pretrained("switch_transformers-base")

    def prepare_config_and_inputs(self):
        input_ids = ids_tensor([self.batch_size, self.encoder_seq_length], self.vocab_size)

        attention_mask = None
        if self.use_attention_mask:
            attention_mask = ids_tensor([self.batch_size, self.encoder_seq_length], vocab_size=2)

        config = SwitchTransformersConfig(
            vocab_size=self.vocab_size,
            d_model=self.hidden_size,
            d_ff=self.d_ff,
            d_kv=self.hidden_size // self.num_attention_heads,
            num_layers=self.num_hidden_layers,
            num_heads=self.num_attention_heads,
            relative_attention_num_buckets=self.relative_attention_num_buckets,
            dropout_rate=self.dropout_rate,
            initializer_factor=self.initializer_factor,
            eos_token_id=self.eos_token_id,
            bos_token_id=self.pad_token_id,
            pad_token_id=self.pad_token_id,
            is_encoder_decoder=self.is_encoder_decoder,
        )

        return (
            config,
            input_ids,
            attention_mask,
        )

    def create_and_check_model(
        self,
        config,
        input_ids,
        attention_mask,
    ):
        model = SwitchTransformersEncoderModel(config=config)
        model.to(torch_device)
        model.eval()
        result = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
        )
        result = model(input_ids=input_ids)
        encoder_output = result.last_hidden_state

        self.parent.assertEqual(encoder_output.size(), (self.batch_size, self.encoder_seq_length, self.hidden_size))

    def create_and_check_model_fp16_forward(
        self,
        config,
        input_ids,
        attention_mask,
    ):
        model = SwitchTransformersEncoderModel(config=config).to(torch_device).half().eval()
        output = model(input_ids, attention_mask=attention_mask)["last_hidden_state"]
        self.parent.assertFalse(torch.isnan(output).any().item())

    def prepare_config_and_inputs_for_common(self):
        config_and_inputs = self.prepare_config_and_inputs()
        (
            config,
            input_ids,
            attention_mask,
        ) = config_and_inputs

        inputs_dict = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        return config, inputs_dict


class SwitchTransformersEncoderOnlyModelTest(ModelTesterMixin, unittest.TestCase):
    all_model_classes = (SwitchTransformersEncoderModel,) if is_torch_available() else ()
    test_pruning = False
    test_resize_embeddings = False
    test_model_parallel = True
    all_parallelizable_model_classes = (SwitchTransformersEncoderModel,) if is_torch_available() else ()

    def setUp(self):
        self.model_tester = SwitchTransformersEncoderOnlyModelTester(self)
        self.config_tester = ConfigTester(self, config_class=SwitchTransformersConfig, d_model=37)

    def test_config(self):
        self.config_tester.run_common_tests()

    def test_model(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model(*config_and_inputs)

    @unittest.skipIf(torch_device == "cpu", "Cant do half precision")
    def test_model_fp16_forward(self):
        config_and_inputs = self.model_tester.prepare_config_and_inputs()
        self.model_tester.create_and_check_model_fp16_forward(*config_and_inputs)


def use_task_specific_params(model, task):
    model.config.update(model.config.task_specific_params[task])


@require_torch
class TestAsymmetricSwitchTransformers(unittest.TestCase):
    def build_model_and_check_forward_pass(self, **kwargs):
        tester = SwitchTransformersModelTester(self, **kwargs)
        config, *inputs = tester.prepare_config_and_inputs()
        (
            input_ids,
            decoder_input_ids,
            attention_mask,
            decoder_attention_mask,
            lm_labels,
        ) = inputs
        model = SwitchTransformersForConditionalGeneration(config=config).to(torch_device).eval()
        outputs = model(
            input_ids=input_ids,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            labels=lm_labels,
        )
        # outputs = model(*inputs)
        assert len(outputs) == 4
        assert outputs["logits"].size() == (tester.batch_size, tester.decoder_seq_length, tester.vocab_size)
        assert outputs["loss"].size() == ()
        return model

    def test_small_decoder(self):
        # num_hidden_layers is passed to SwitchTransformersConfig as num_layers
        model = self.build_model_and_check_forward_pass(decoder_layers=1, num_hidden_layers=2)
        assert len(model.encoder.block) == 2
        assert len(model.decoder.block) == 1

    def test_defaulting_to_symmetry(self):
        # num_hidden_layers is passed to SwitchTransformersConfig as num_layers
        model = self.build_model_and_check_forward_pass(num_hidden_layers=2)
        assert len(model.decoder.block) == len(model.encoder.block) == 2


class SwitchTransformerRouterTest(unittest.TestCase):
    r"""
    Switch Transformers has different blocks from classic transformer based models.
    The Swift MLP contains a Router class, that has to be tested to check if it is correctly implemented

    Original implementation of the routers here:

    """

    def test_equivalency_balancy_loss(self):
        r"""
        This test checks if the balancy loss is correctly implemented
        as in the original implementation of the Switch Transformer .
        """
        router_probs = torch.Tensor(
            [
                [0.35490513, 0.60419905],
                [0.4275843, 0.23061597],
                [0.32985854, 0.43953657],
                [0.25099766, 0.27730572],
                [0.7678207, 0.71474564],
            ]
        )

        expert_indices = torch.Tensor([[0], [1], [1], [0], [0]]).to(torch.int32)

        loss = load_balancing_loss_func(router_probs, expert_indices)
        self.assertAlmostEqual(loss.item(), 0.8741045, places=5)

    def test_equivalency_router_z_loss(self):
        r"""
        This test checks if the router z loss is correctly implemented
        as in the original implementation of the Switch Transformer .
        """
        logits = torch.Tensor(
            [
                [
                    [-4.2124424, 3.891939, -3.6481273, 1.8849981],
                    [0.32625437, 2.918651, 0.84758997, -4.556842],
                    [-3.32062, 4.6977115, -0.15439987, 0.44086337],
                    [3.4467149, 4.3436565, -4.7224274, -4.264637],
                    [-2.224406, -2.5318158, -1.3832569, 1.1891162],
                    [-2.320062, -0.44705987, 4.289819, -0.00662684],
                ],
                [
                    [0.99470854, -0.6992364, 0.25503993, 4.2952085],
                    [3.5937333, -3.2408535, -4.298278, 4.426601],
                    [0.7669008, 2.6588762, 2.4505413, 4.6051874],
                    [0.23330331, -3.0845237, 0.6262374, -2.9865491],
                    [0.7595146, -2.1099675, -4.155346, -2.8326452],
                    [2.3771453, 1.004138, -3.1781673, 0.7581556],
                ],
            ]
        )

        loss = router_z_loss_func(logits)
        self.assertAlmostEqual(loss.item(), 13.786719, places=5)

    def test_equivalency_token_chose_masked_router(self):
        r"""
        This test tests the equivalency between the `TokensChooseMaskedRouter`
        originally implemented from here: TODO: provide link
        """
        hidden_dim = 4
        num_experts = 2
        num_selected_experts = 1  # Switch routing case
        expert_capacity = 1  # Total capacity = 2*2*1 = 4 < num_tokens
        jitter_noise = 0.0

        input_tokens = torch.Tensor(
            [
                [
                    [0.6433916, 0.18188512, 0.02240455, 0.563781],
                    [0.5526401, 0.0958724, 0.34253013, 0.03644359],
                    [0.08744538, 0.7909105, 0.35205448, 0.53364205],
                ],
                [
                    [0.02900076, 0.4168595, 0.5802449, 0.91486526],
                    [0.27414513, 0.14991808, 0.9383501, 0.5209162],
                    [0.51207185, 0.90618336, 0.7309413, 0.95533276],
                ],
            ]
        )

        config = SwitchTransformersConfig(
            num_experts=num_experts,
            hidden_size=hidden_dim,
            num_selected_experts=num_selected_experts,
            router_jitter_noise=jitter_noise,
            expert_capacity=expert_capacity,
            batch_prioritized_routing=False,
        )
        model = TokensChooseMaskedRouter(config)

        model.router_weights.weight = torch.nn.Parameter(
            torch.Tensor(
                [
                    [0.02008116, 0.00620062],
                    [-0.00811031, -0.00031623],
                    [-0.03542127, 0.02703803],
                    [0.02335377, -0.02971946],
                ],
            ).t()
        )

        output = model(input_tokens, expert_capacity=expert_capacity)

        expected_dispatch_mask = torch.Tensor(
            [
                [[[True], [False]], [[False], [True]], [[False], [False]]],
                [[[True], [False]], [[False], [True]], [[False], [False]]],
            ]
        )

        expected_combine_array = torch.Tensor(
            [
                [[[0.5090], [0.0000]], [[0.0000], [0.5031]], [[0.0000], [0.0000]]],
                [[[0.5024], [0.0000]], [[0.0000], [0.5071]], [[0.0000], [0.0000]]],
            ]
        )

        self.assertAlmostEqual(output.auxiliary_loss.item(), 1.000308, places=5)
        self.assertAlmostEqual(output.router_z_loss.item(), 0.4789799, places=5)

        self.assertTrue(torch.allclose(output.dispatch_mask, expected_dispatch_mask))
        self.assertTrue(torch.allclose(output.combine_array, expected_combine_array, atol=1e-4))

    def test_equivalency_experts_chose_masked_router(self):
        r"""
        This test tests the equivalency between the `ExpertsChooseMaskedRouter`
        originally implemented from here: TODO: provide link
        """
        hidden_dim = 3
        num_experts = 2
        expert_capacity = 2  # Total capacity = 2*2*1 = 4 < num_tokens
        jitter_noise = 0.0

        input_tokens = torch.Tensor(
            [
                [
                    [0.6433916, 0.18188512, 0.02240455],
                    [0.563781, 0.5526401, 0.0958724],
                    [0.34253013, 0.03644359, 0.08744538],
                    [0.7909105, 0.35205448, 0.53364205],
                ],
                [
                    [0.02900076, 0.4168595, 0.5802449],
                    [0.91486526, 0.27414513, 0.14991808],
                    [0.9383501, 0.5209162, 0.51207185],
                    [0.90618336, 0.7309413, 0.95533276],
                ],
            ]
        )

        config = SwitchTransformersConfig(
            num_experts=num_experts,
            hidden_size=hidden_dim,
            router_jitter_noise=jitter_noise,
            expert_capacity=expert_capacity,
            batch_prioritized_routing=False,
        )

        model = ExpertsChooseMaskedRouter(config)

        model.router_weights.weight = torch.nn.Parameter(
            torch.Tensor([[-0.00107201, 0.01544739], [-0.0087319, 0.01314363], [0.03530733, 0.03709853]]).t()
        )

        output = model(input_tokens, expert_capacity=expert_capacity)

        self.assertEqual(output.auxiliary_loss, 0.0)
        self.assertAlmostEqual(output.router_z_loss.item(), 0.507016, places=5)

        expected_dispatch_mask = torch.Tensor(
            [
                [[[0, 1], [0, 0]], [[0, 0], [0, 1]], [[1, 0], [0, 0]], [[0, 0], [1, 0]]],
                [[[1, 0], [0, 0]], [[0, 1], [0, 0]], [[0, 0], [0, 1]], [[0, 0], [1, 0]]],
            ]
        )

        self.assertTrue(torch.allclose(output.dispatch_mask, expected_dispatch_mask))

        expected_combined_array = torch.Tensor(
            [
                [
                    [[0.0000, 0.4963], [0.0000, 0.0000]],
                    [[0.0000, 0.0000], [0.0000, 0.5054]],
                    [[0.4983, 0.0000], [0.0000, 0.0000]],
                    [[0.0000, 0.0000], [0.5054, 0.0000]],
                ],
                [
                    [[0.4973, 0.0000], [0.0000, 0.0000]],
                    [[0.0000, 0.4947], [0.0000, 0.0000]],
                    [[0.0000, 0.0000], [0.0000, 0.5070]],
                    [[0.0000, 0.0000], [0.5082, 0.0000]],
                ],
            ]
        )

        self.assertTrue(torch.allclose(output.combine_array, expected_combined_array, atol=1e-4))


@require_torch
@require_tokenizers
class SwitchTransformerModelIntegrationTests(unittest.TestCase):
    def test_small_logits(self):
        pass

    def test_large_logits(self):
        pass

    def test_small_logits_bf16(self):
        pass

    def test_small_batch_generate(self):
        pass

    def test_large_batch_generate(self):
        pass

    @slow
    def test_summarization(self):
        pass

    @slow
    def test_translation_en_to_de(self):
        pass