import argparse
from transformers import AutoTokenizer, AutoConfig, AutoModelForSeq2SeqLM
from transformers.models.bigscience176b import BigScience176BLMHeadModel
from transformers.deepspeed import HfDeepSpeedConfig
import deepspeed
import os
import torch

parser = argparse.ArgumentParser(description='Run some evaluation on a pretrained model')

parser.add_argument('--nvme_path', type=str, 
                    help='nvme path', required=True)

args = parser.parse_args()

jobscratch_path = args.nvme_path
os.environ["TOKENIZERS_PARALLELISM"] = "false"  # To avoid warnings about parallelism in tokenizers

# distributed setup
local_rank = int(os.getenv("LOCAL_RANK", "0"))
world_size = int(os.getenv("WORLD_SIZE", "1"))
torch.cuda.set_device(local_rank)
deepspeed.init_distributed()

model_name = "/gpfswork/rech/six/uan68tv/model-conversion/tr11e-350M-transformers-sharded"
# jobscratch_path = "/gpfsssd/jobscratch/"
    #model_name = "bigscience/T0_3B"

config = AutoConfig.from_pretrained(model_name)
model_hidden_size = config.hidden_size

    # batch size has to be divisible by world_size, but can be bigger than world_size
train_batch_size = 1 * world_size

print("jobscratch path:", jobscratch_path)
# ds_config notes
#
# - enable bf16 if you use Ampere or higher GPU - this will run in mixed precision and will be
# faster.
#
# - for older GPUs you can enable fp16, but it'll only work for non-bf16 pretrained models - e.g.
# all official t5 models are bf16-pretrained
#
# - set offload_param.device to "none" or completely remove the `offload_param` section if you don't
# - want CPU offload
#
# - if using `offload_param` you can manually finetune stage3_param_persistence_threshold to control
# - which params should remain on gpus - the larger the value the smaller the offload size
#
# For indepth info on Deepspeed config see
# https://huggingface.co/docs/transformers/main/main_classes/deepspeed

# XXX: modified this script to use nvme offload so need to explain the new configs, but the key is
# to change the path to `nvme_path`

# keeping the same format as json for consistency, except it uses lower case for true/false
# fmt: off
ds_config = {
    "fp16": {
        "enabled": True
    },
    "bf16": {
        "enabled": False
    },
    "zero_optimization": {
        "stage": 3,
        "offload_param": {
            "device": "nvme",
            "nvme_path": jobscratch_path,
            "pin_memory": True,
            "buffer_count": 6,
            "buffer_size": 1e8,
            "max_in_cpu": 1e9
        },
        "aio": {
            "block_size": 262144,
            "queue_depth": 32,
            "thread_count": 1,
            "single_submit": False,
            "overlap_events": True
        },
        "overlap_comm": True,
        "contiguous_gradients": True,
        "reduce_bucket_size": model_hidden_size * model_hidden_size,
        "stage3_prefetch_bucket_size": 0.1 * model_hidden_size * model_hidden_size,
        "stage3_max_live_parameters": 1e8,
        "stage3_max_reuse_distance": 1e8,
        "stage3_param_persistence_threshold": 10 * model_hidden_size
    },
    "steps_per_print": 2000,
    "train_batch_size": train_batch_size,
    "train_micro_batch_size_per_gpu": 1,
    "wall_clock_breakdown": False
}
# # fmt: on

# # next line instructs transformers to partition the model directly over multiple gpus using
# # deepspeed.zero.Init when model's `from_pretrained` method is called.
# #
# # **it has to be run before loading the model AutoModelForSeq2SeqLM.from_pretrained(model_name)**
# #
# # otherwise the model will first be loaded normally and only partitioned at forward time which is
# # less efficient and when there is little CPU RAM may fail
dschf = HfDeepSpeedConfig(ds_config)  # keep this object alive

# # now a model can be loaded.
model = BigScience176BLMHeadModel.from_pretrained(model_name)#, low_cpu_mem_usage=True)

# initialise Deepspeed ZeRO and store only the engine object
ds_engine = deepspeed.initialize(model=model, config_params=ds_config)[0]
ds_engine.module.eval()  # inference

# # Deepspeed ZeRO can process unrelated inputs on each GPU. So for 2 gpus you process 2 inputs at once.
# # If you use more GPUs adjust for more.
# # And of course if you have just one input to process you then need to pass the same string to both gpus
# # If you use only one GPU, then you will have only rank 0.
# rank = torch.distributed.get_rank()
# if rank == 0:
#     text_in = "Is this review positive or negative? Review: this is the best cast iron skillet you will ever buy"
# elif rank == 1:
#     text_in = "Is this review positive or negative? Review: this is the worst restaurant ever"

# tokenizer = AutoTokenizer.from_pretrained(model_name)
# inputs = tokenizer.encode(text_in, return_tensors="pt").to(device=local_rank)
# from transformers.deepspeed import is_deepspeed_zero3_enabled
# print(f"Deepspeed 3 is enabled: {is_deepspeed_zero3_enabled()}")

EXAMPLE_IDS = [[2175,  23714,  73173, 144252, 2, 77, 132619, 3478, 368, 109586, 35433, 2, 2175,  23714,  73173, 144252, 2, 2175, 23714, 73173]]
ATTN_MASK = torch.triu(torch.ones(1, 1, 20, 20), diagonal=1).to(model.dtype)

with torch.no_grad():
    input_tensor = torch.LongTensor(EXAMPLE_IDS)
    logits = ds_engine.module.forward(input_tensor, attention_mask=ATTN_MASK).logits

print(logits)