import json
import math
import pathlib
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

import torch
import torch.distributed
import transformers
from torch.nn import CrossEntropyLoss
from transformers import LlamaTokenizer, Trainer

from functionary.prompt import EndToken
from train.custom_datasets import CustomDataset, split_data
from train.llama_flash_attn_monkey_patch import replace_llama_attn_with_flash_attn

replace_llama_attn_with_flash_attn()


@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="meta-llama/Llama-2-7b-hf")


@dataclass
class DataArguments:
    data_path: str = field(
        default=None, metadata={"help": "Path to the training data."}
    )
    train_valid_split: float = field(
        default=0.9,
        metadata={
            "help": "Ratio to split overall data into train-validation. Must be between 0.0 and 1.0."
        },
    )


@dataclass
class TrainingArguments(transformers.TrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(
        default=4096,
        metadata={
            "help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )


def safe_save_model_for_hf_trainer(trainer: transformers.Trainer, output_dir: str):
    """Collects the state dict and dump to disk."""
    state_dict = trainer.model.state_dict()
    if trainer.args.should_save:
        cpu_state_dict = {key: value.cpu() for key, value in state_dict.items()}
        del state_dict
        trainer._save(output_dir, state_dict=cpu_state_dict)  # noqa


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.
    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


def train():
    argument_parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments)
    )
    model_args, data_args, training_args = argument_parser.parse_args_into_dataclasses()

    # # Set RoPE scaling factor
    # config = transformers.AutoConfig.from_pretrained(
    #     model_args.model_name_or_path,
    #     cache_dir=training_args.cache_dir,
    # )
    # orig_ctx_len = getattr(config, "max_position_embeddings", None)
    # if orig_ctx_len and training_args.model_max_length > orig_ctx_len:
    #     scaling_factor = float(math.ceil(training_args.model_max_length / orig_ctx_len))
    #     config.rope_scaling = {"type": "linear", "factor": scaling_factor}
    # config.use_cache = False

    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        # config=config,
        cache_dir=training_args.cache_dir,
    )
    model.config.use_cache = False
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
        model_max_length=training_args.model_max_length,
        padding_side="right",
        use_fast=False,
        # legacy=False,  # See: https://github.com/huggingface/transformers/pull/24565
    )
    tokenizer.pad_token = tokenizer.unk_token
    added_tokens = [e.value for e in EndToken]
    special_tokens_dict = {"additional_special_tokens": added_tokens}
    smart_tokenizer_and_embedding_resize(
        special_tokens_dict=special_tokens_dict, tokenizer=tokenizer, model=model
    )

    with open(data_args.data_path, "r") as file:
        raw_data = [json.loads(line) for line in file]

    if torch.distributed.get_rank() == 0:
        print(f"Data Loaded: #{len(raw_data)}")

    random.shuffle(raw_data)

    if training_args.do_eval:
        # Do train-validation split
        assert (
            0.0 < data_args.train_valid_split <= 1.0
        ), f"The `train_valid_split` argument of `{data_args.train_valid_split}` is not between 0.0 and 1.0."

        raw_train_data, raw_eval_data = split_data(
            raw_data, data_args.data_path, data_args.train_valid_split
        )
        train_dataset = CustomDataset(raw_train_data, tokenizer)
        eval_dataset = CustomDataset(raw_eval_data, tokenizer)
    else:
        train_dataset = CustomDataset(raw_data, tokenizer)

    def preprocess_logits_for_metrics(logits, labels):
        """Preprocesses the logits during evaluation by computing the greedy token predictions for
        accuracy calculation and loss values for perplexity calculation. Both pred_ids and loss are
        of shape (batch_size x seq_len)"""
        pred_ids = torch.argmax(logits, dim=-1)

        loss_fn = CrossEntropyLoss(reduction="none")
        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()
        shift_logits = shift_logits.view(-1, tokenizer.vocab_size)
        shift_labels = shift_labels.view(-1)
        loss = loss_fn(shift_logits, shift_labels)
        loss = torch.mean(loss.view(logits.shape[0], -1), dim=-1)

        return pred_ids, loss

    def compute_metrics(eval_preds):
        """Computes next-token accuracy and perplexity metrics for evaluation"""
        predictions = eval_preds.predictions[0][:, :-1]
        labels = eval_preds.label_ids[:, 1:]

        # Calculate accuracy
        acc_count = 0
        total_num = 0
        for pred, label in zip(
            predictions.flatten().tolist(), labels.flatten().tolist()
        ):
            if label != -100:
                if label == pred:
                    acc_count += 1
                total_num += 1

        # Calculate perplexity
        loss = eval_preds.predictions[1].tolist()
        loss = sum(loss) / len(loss)
        perplexity = math.exp(loss)

        return {"accuracy": acc_count / total_num, "perplexity": perplexity}

    if training_args.do_eval:
        trainer = Trainer(
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            compute_metrics=compute_metrics,
            preprocess_logits_for_metrics=preprocess_logits_for_metrics,
        )
    else:
        trainer = Trainer(
            model=model,
            tokenizer=tokenizer,
            args=training_args,
            train_dataset=train_dataset,
        )

    if list(pathlib.Path(training_args.output_dir).glob("checkpoint-*")):
        trainer.train(resume_from_checkpoint=True)
    else:
        trainer.train()
    trainer.save_state()
    safe_save_model_for_hf_trainer(trainer=trainer, output_dir=training_args.output_dir)


if __name__ == "__main__":
    train()
