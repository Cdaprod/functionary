from typing import List, Optional, Tuple, Generator, Union, Any, Dict
import json
import re
import torch
import gc
from transformers import LlamaForCausalLM, LlamaTokenizer

from transformers.generation.logits_process import (
    LogitsProcessorList,
    RepetitionPenaltyLogitsProcessor,
    TemperatureLogitsWarper,
    TopKLogitsWarper,
    TopPLogitsWarper,
)

from functionary.openai_types import ChatMessage, Function, FunctionCall
from functionary.schema import generate_schema_from_functions

SYSTEM_MESSAGE = """A chat between a curious user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user's questions. The assistant calls functions with appropriate input when necessary"""


def tokenize(message: ChatMessage, tokenizer: LlamaTokenizer):
    text = str(message)
    return tokenizer(text, add_special_tokens=False, return_tensors="pt").input_ids.to("cuda:0")


def prepare_logits_processor(
    temperature: float, repetition_penalty: float, top_p: float, top_k: int
) -> LogitsProcessorList:
    processor_list = LogitsProcessorList()
    # TemperatureLogitsWarper doesn't accept 0.0, 1.0 makes it a no-op so we skip two cases.
    if temperature >= 1e-5 and temperature != 1.0:
        processor_list.append(TemperatureLogitsWarper(temperature))
    if repetition_penalty > 1.0:
        processor_list.append(RepetitionPenaltyLogitsProcessor(repetition_penalty))
    if 1e-8 <= top_p < 1.0:
        processor_list.append(TopPLogitsWarper(top_p))
    if top_k > 0:
        processor_list.append(TopKLogitsWarper(top_k))
    return processor_list


def prepare_messages_for_inference(
    tokenizer: LlamaTokenizer, messages: List[ChatMessage], functions=None
) -> torch.Tensor:
    all_messages = []
    if functions is not None:
        all_messages.append(ChatMessage(role="system", content=generate_schema_from_functions(functions)))

    all_messages.append(ChatMessage(role="system", content=SYSTEM_MESSAGE))

    for message in messages:
        if message.role == "assistant":
            if message:
                all_messages.append(ChatMessage(role="assistant", content=message.content))
            if message.function_call:
                fc = message.function_call
                all_messages.append(
                    ChatMessage(
                        role="assistant",
                        _to=f"functions.{fc.name}",
                        content=fc.arguments,
                    )
                )
        elif message.role == "function":
            all_messages.append(
                ChatMessage(
                    role="function",
                    name=f"functions.{message.name}",
                    content=message.content,
                )
            )
        else:
            all_messages.append(message)

    all_messages.append(ChatMessage(role="assistant", content=None))

    # ! should this be done as concatting strings and then tokenizing?
    # ! >>> text = "".join([str(msg) for msg in all_messages]
    # ! >>> return tokenizer(text, add_special_tokens=False, return_tensors="pt").input_ids.to("cuda:0")
    all_input_ids = [tokenize(tokenizer=tokenizer, message=message) for message in all_messages]
    return torch.cat(all_input_ids, dim=-1)


def generate_message(
    model: LlamaForCausalLM,
    tokenizer: LlamaTokenizer,
    messages: List[ChatMessage],
    functions: Optional[List[Function]] = None,
    temperature: float = 0.7,
    max_new_tokens=256,
) -> ChatMessage:
    inputs = prepare_messages_for_inference(tokenizer=tokenizer, messages=messages, functions=functions)
    print("input shape: ", inputs.shape)
    generate_ids = model.generate(inputs, max_new_tokens=max_new_tokens, temperature=temperature)
    generated_content = tokenizer.batch_decode(
        generate_ids[:, inputs.shape[1] :],
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0]

    # If it's a function call:
    if generated_content.startswith("to=functions."):
        function_call_content = generated_content[len("to=functions.") :]
        function_name, arguments = function_call_content.split(":\n")
        return ChatMessage(
            role="assistant",
            function_call=FunctionCall(name=function_name, arguments=arguments),
        )
    return ChatMessage(
        role="assistant",
        content=generated_content.lstrip("assistant:\n").rstrip("\n user:\n"),
    )


def generate_text_stream(
    model: LlamaForCausalLM,
    tokenizer: LlamaTokenizer,
    messages: List[ChatMessage],
    functions: Optional[List[Function]] = None,
    temperature: float = 0.7,
    max_new_tokens=256,
    **kwargs,
):
    if hasattr(model, "device"):
        device = model.device
    else:
        device = "cuda:0"
    repetition_penalty = float(kwargs.get("repetition_penalty", 1.0))
    top_p = float(kwargs.get("top_p", 1.0))
    top_k = int(kwargs.get("top_k", -1))  # -1 means disable
    stop_token_ids = []
    if tokenizer.eos_token_id not in stop_token_ids:
        stop_token_ids.append(tokenizer.eos_token_id)

    logits_processor = prepare_logits_processor(temperature, repetition_penalty, top_p, top_k)
    input_ids = prepare_messages_for_inference(tokenizer=tokenizer, messages=messages, functions=functions)
    input_ids = input_ids.to(device)
    output_ids = input_ids.clone().detach()
    past_key_values = None # KV cached
    token_ts = None # next token
    finish_reason = None
    reach_stop_token = False
    words = ""
    for i in range(max_new_tokens):
        if i == 0:  # prefill
            out = model(input_ids, use_cache=True)
        else:  # decoding
            out = model(
                    input_ids=token_ts,
                    use_cache=True,
                    past_key_values=past_key_values,
                )
        logits = out.logits
        past_key_values = out.past_key_values

        if logits_processor:
            if repetition_penalty > 1.0:
                tmp_output_ids = torch.as_tensor([output_ids], device=logits.device)
            else:
                tmp_output_ids = None
            last_token_logits = logits_processor(tmp_output_ids, logits[:, -1, :])[0]
        else:
            last_token_logits = logits[0, -1, :]

        if temperature < 1e-5 or top_p < 1e-8:  # greedy
            _, indices = torch.topk(last_token_logits, 2)
            tokens = [int(index) for index in indices.tolist()]
        else:
            probs = torch.softmax(last_token_logits, dim=-1)
            indices = torch.multinomial(probs, num_samples=2)
            tokens = [int(token) for token in indices.tolist()]
        token_int = tokens[0]
        token_ts = torch.as_tensor([[token_int]], device=device)
        current_output_text = tokenizer.decode(
            output_ids[0].tolist(),
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        output_ids = torch.cat((output_ids, token_ts), 1)
        next_output_text = tokenizer.decode(
            output_ids[0].tolist(),
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )
        output = next_output_text[len(current_output_text) :]
        words += output
        if token_int in stop_token_ids:
            reach_stop_token = True
            break
        yield (output, finish_reason)

    # Finish stream event, which contains finish reason
    if reach_stop_token:
        finish_reason = "stop"
    else:
        finish_reason = "lenghth"

    yield ("", finish_reason)

    # Clean
    del past_key_values, out
    gc.collect()
    torch.cuda.empty_cache()


def generate_stream(
    model: LlamaForCausalLM,
    tokenizer: LlamaTokenizer,
    messages: List[ChatMessage],
    functions: Optional[List[Function]] = None,
    temperature: float = 0.7,
    max_new_tokens=256,
    **kwargs,
):
    generator = generate_text_stream(
        model=model,
        tokenizer=tokenizer,
        messages=messages,
        functions=functions,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
        **kwargs,
    )
    cur_text = ""
    func_name = None
    response_type = None  # = function if it is function call; = text if it is chit-chat
    response: Dict[str, Any] = {}
    for item, finish_reason in generator:
        # print(f"item:{item}, finish_reason: {finish_reason}; response_type: {response_type}")
        cur_text += item
        if response_type is None:
            if cur_text.lstrip() == ":\n":
                response_type = "text"
                response = {"delta": {"content": "", "role": "assistant"}, "finish_reason": None}
                yield response
            else:
                match = re.search(r"to=functions\.(?P<f>.+?):", cur_text.strip())
                if match is not None:
                    response_type = "function"
                    func_name = match.group("f").strip()
                    response = {
                        "delta": {
                            "role": "assistant",
                            "content": None,
                            "function_call": {"arguments": "", "name": func_name},
                        },
                        "finish_reason": None,
                    }
                    yield response
        elif response_type == "function":
            if finish_reason is None:
                response = {
                    "delta": {"role": "assistant", "function_call": {"arguments": item, "name": func_name}},
                    "finish_reason": None,
                }
            else:
                response = {"delta": {"role": "assistant"}, "finish_reason": "function_call"}
            yield response
        elif response_type == "text":
            if finish_reason is None:
                response = {"delta": {"content": item, "role": "assistant"}, "finish_reason": None}
            else:
                response = {"delta": {"role": "assistant"}, "finish_reason": finish_reason}
            yield response
