import json
from typing import Any, Dict, List, Optional, Tuple, Union
from abc import ABC, abstractmethod

import torch
import transformers
from torch.utils.data import Dataset
import datetime
import os

from functionary.prompt import (
    EndToken,
    get_number_of_tokens_of_prefix_assistant,
    get_prompt_from_messages,
    get_end_token_to_token_id,
)


def get_batch_indices(size: int, batch_size: int) -> List[Tuple[int, int]]:
    result = []
    for i in range(size // batch_size + 1):
        start = i * batch_size
        end = i * batch_size + batch_size
        if end > size:
            end = size
        if end > start:
            result.append((start, end))
    return result


def get_prefix_assistant_token_ids(tokenizer: Any):
    result = []
    for e in EndToken:
        prefix = f"{e.value}\nassistant:"
        token_ids = tokenizer.encode(prefix, add_special_tokens=False)
        if token_ids[0] == 29871:
            token_ids = token_ids[1:]
        result.append(token_ids)
    return result


def get_matching_prefix(prefix_tokens, sequence_ids):
    for prefix in prefix_tokens:
        if len(sequence_ids) >= len(prefix):
            if sequence_ids[: len(prefix)] == prefix:
                return prefix
    return None


def prepare_training_inputs(
    messages: Dict[str, List],
    tokenizer: Any,
    padding: Optional[str] = "max_length",
    max_length: Optional[int] = None,
    return_tensor: bool = True,
    verbose=False,
) -> Dict[str, Union[str, Dict]]:
    batch_result = prepare_training_inputs_batch([messages], tokenizer, padding, max_length, return_tensor, verbose)
    return dict(final_prompt=batch_result["batch_prompts"][0], inputs=batch_result["batch_inputs"][0])


def get_masked_labels(input_token_ids: List[int], tokenizer: Any, endtoken_2_id: Dict, verbose: bool = False):
    # first we initialize labels with all positions as -100,
    # then we will fill in positions where role=assistant as we only include these in computing the loss
    labels = [-100 for _ in range(len(input_token_ids))]
    start = 0
    # now we will unmask labels by positions that was from assistant
    # we will find the chunks: "<endtoken>assistant ...(<end_of_function>|<end_of_assistant>) from input_token_ids
    # and unmask: this part: "...(<end_of_function>|<end_of_assistant>"
    # find token_ids of: "<endtoken>assistant"
    prefix_token_ids = get_prefix_assistant_token_ids(tokenizer)
    if verbose:
        print("prefix_token_ids: ", prefix_token_ids)
    index = 0
    total_input_leng = len(input_token_ids)
    while index < total_input_leng:
        # finding the index that start with: "<endtoken>assistant" --> we will unmask labels from this position
        matched_prefix = get_matching_prefix(prefix_token_ids, input_token_ids[index:])
        if matched_prefix is not None:
            end_index = -1
            # unmask until reach <end_of_function> or <end_of_assistant>
            for i in range(index + len(matched_prefix), total_input_leng):
                tok_id = input_token_ids[i]
                if tok_id in [
                    endtoken_2_id[EndToken.assistant],
                    endtoken_2_id[EndToken.function_call],
                ]:  # check if this is end of turn
                    labels[i] = input_token_ids[i]  # unmask labels at this position
                    end_index = i
                    break
                else:
                    labels[i] = input_token_ids[i]  # unmask labels at this position
            if verbose:
                print("------------------------")
                start = index + len(matched_prefix)
                chunk_ids = input_token_ids[start : end_index + 1] if end_index > -1 else input_token_ids[start:]
                print("chunk_ids: ", chunk_ids)
                print(
                    "longer chunk: ",
                    input_token_ids[index : end_index + 1] if end_index > 1 else input_token_ids[index:],
                )
                print(f"chunk:{tokenizer.decode(chunk_ids)}")
                print("-------------------")
            if (
                end_index == -1
            ):  # if at the end, cannot find EndToken.assistant or EndToken.function_call --> this data point was truncated
                break
            index = end_index
        else:
            index += 1
    return labels


def prepare_training_inputs_batch(
    batch_messages: Dict[str, List],
    tokenizer: Any,
    padding: Optional[str] = "max_length",
    max_length: Optional[int] = None,
    return_tensor: bool = True,
    verbose=False,
) -> List[Dict[str, Union[str, Dict]]]:
    """This function is used for when you want to get a dictionary input for the model.forward.
    The dictionary will contain: input_ids, attention_maks, labels.
    labels is like input_ids except that content from user, system, function will be set as -100, only content from assistant remains

    Args:
        messages (List[Dict]): List of messages in openAI format (containing: role, content and function_call (optional))
        tokenizer (Any): tokenizer from transformers
        padding (str, optional): type of padding (longest, max_length), this is passed to tokenizer(). Defaults to "max_length".
        max_length (Optional[int], optional): maximum number of tokens allowed in prompt. Defaults to None.
        return_tensor (bool, optional): if true, the input_dic will be dictionary[str, Tensor] else dictionary[str, List[int]]. Defaults to True.
        verbose (bool, optional): to print some useful information or not. Defaults to False.

    Returns:
        Dict[str, Union[str, Dict]]: {"final_prompt": str, "inputs": Dict}
            final_prompt: the final prompt to be used,
            inputs: a dictionary containing: input_ids, attention_mask, labels. This will be used in model.forward(**inputs)
    """
    # a dictionary mapping from token_id --> end_token
    endtoken_2_id = get_end_token_to_token_id(tokenizer)
    prompt_str_list = []
    for messages in batch_messages:
        prompt_str = get_prompt_from_messages(
            messages["messages"], messages["functions"]
        )  # prompt_str is the concatenation of all prompts from messages
        prompt_str_list.append(prompt_str)
    max_length = max_length if max_length is not None else tokenizer.model_max_length

    input_dic = tokenizer(prompt_str_list, padding=padding, max_length=max_length, truncation=True)
    #input_token_ids = input_dic["input_ids"]
    batch_labels = []
    for input_token_ids in input_dic["input_ids"]:
        labels = get_masked_labels(input_token_ids, tokenizer, endtoken_2_id, verbose=verbose)
        batch_labels.append(labels)
        assert len(labels) == len(input_token_ids)

    input_dic["labels"] = batch_labels
    assert len(input_dic["labels"]) == len(input_dic["input_ids"]) == len(input_dic["attention_mask"]) == len(batch_messages)
    
    batch_inputs = []
    for i in range(len(input_dic["input_ids"])):
        inputs = {}
        for key in ["labels", "input_ids", "attention_mask"]:
            inputs[key] = input_dic[key][i]
            if return_tensor:
                inputs[key] = torch.tensor(inputs[key])
        batch_inputs.append(inputs)

    return dict(batch_prompts=prompt_str_list, batch_inputs=batch_inputs)


class CachedDataset(Dataset):
    def __init__(self, tokenizer: Any, cached_folder: str, ignore_cached: bool=False) -> None:
        super().__init__()
        self.tokenizer = tokenizer
        self.data_points = []
        if cached_folder is not None and os.path.exists(os.path.join(cached_folder, "inputs.jsonl")) and not ignore_cached:
            print(f"cached found, load from cached: {cached_folder}")
            self.load(cached_folder)
    
    def __len__(self):
        return len(self.data_points)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        return self.data_points[i]
    
    def post_process_loaded_item(self, item: Dict) -> Dict:
        return item
    
    def post_process_before_dump(self, item: Dict) -> Dict:
        return item
    
    def create_meta_info(self):
        return {"max_length": self.tokenizer.model_max_length, "size": len(self.data_points)}
    
    def load(self, folder: str):
        count = 0
        with open(os.path.join(folder, "inputs.jsonl"), "r") as f:
            for line in f:
                if line:
                    item = json.loads(line)
                    item = self.post_process_loaded_item(item)
                    self.data_points.append(item)
                    count += 1
        print(f"load: {count} from cached: {folder}")
    
    def dump(self, folder: str):
        if not os.path.exists(folder):
            os.mkdir(folder)
        print(f"dump: {len(self.data_points)} datapoints to: {folder}")
        with open(os.path.join(folder, "inputs.jsonl"), "w") as f:
            for item in self.data_points:
                n_item = self.post_process_before_dump(item)
                f.write(json.dumps(n_item) + "\n")
                
        with open(os.path.join(folder, "meta_info.jsonl"), "w") as f:
            f.write(json.dumps(self.create_meta_info()))          
    
    def stat(self):
        print(json.dumps(self.create_meta_info()))
    

class CustomDataset(CachedDataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self, raw_data, tokenizer: transformers.PreTrainedTokenizer, cached_folder: Optional[str] = None, ignore_cached: bool = False, batch_size: int = 5000):
        super(self).__init__(tokenizer, cached_folder, ignore_cached)
        
        if len(self.data_points) == 0: # if loaded from cached
            self.processed_data = []
            data_size = len(raw_data)
            t1 = datetime.datetime.now()
            invalid_count = 0
            for start, end in get_batch_indices(data_size, batch_size):
                batch_result = prepare_training_inputs_batch(raw_data[start: end], tokenizer, return_tensor=True)
                assert len(batch_result["batch_inputs"]) == len(raw_data[start: end])
                for item in batch_result["batch_inputs"]:
                    if is_valid_labels(item["labels"]):
                        self.processed_data.append(item)
                    else: 
                        invalid_count += 1
                t2 = datetime.datetime.now()
                avg_time = (t2 - t1).total_seconds() / len(self.processed_data)
                remaining_time = avg_time * (data_size - len(self.processed_data))
                print(f"{len(self.processed_data)}/{data_size}, avg_time per 1000 data points: {avg_time * 1000}, remaining time: {remaining_time}")
            assert len(self.processed_data) == data_size - invalid_count
            print("number of invalid data points where labels=-100 all the times: ", invalid_count)
            if cached_path is not None:
                self.dump(cached_path)

    def __len__(self):
        return len(self.processed_data)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        return self.processed_data[i]
    
    def load(self, folder: str):
        self.processed_data = []
        with open(os.path.join(folder, "inputs.jsonl"), "r") as f:
            for line in f:
                if line:
                    item = json.loads(line)
                    for key in item:
                        item[key] = torch.tensor(item[key])
                    self.processed_data.append(item)
        print(f"load: {len(self.processed_data)} from cached: {folder}")
    
    def dump(self, folder: str):
        if not os.path.exists(folder):
            os.mkdir(folder)
        print(f"dump: {len(self.processed_data)} datapoints to: {folder}")
        with open(os.path.join(folder, "inputs.jsonl"), "w") as f:
            for item in self.processed_data:
                n_item = {}
                for key in item:
                    n_item[key] = item[key].tolist()
                f.write(json.dumps(n_item) + "\n")
        meta = {"max_length": self.tokenizer.model_max_length}
        with open(os.path.join(folder, "meta_info.jsonl"), "w") as f:
            f.write(json.dumps(meta))          
    
    def stat(self):
        print("number of data points: ", len(self.processed_data))


def merge_data_points_by_length(lengths: List[int], max_length: int) -> List[List[int]]:
    """given lengths of data points, we merge them into groups such that the sum of lengths
    in each group is less than max_length. This is known as: https://en.wikipedia.org/wiki/Bin_packing_problem
    Here is the greedy algorithm
    Args:
        lengths (List[int]): _description_
        max_length (int): _description_

    Returns:
        _type_: groups of indices: [[index1, index2, ...], [], ...]
    """
    items = [{"length": length, "index": i} for i, length in enumerate(lengths)]
    items = sorted(items, key=lambda x: x["index"])
    merges = []
    current_sum = 0
    current_list = []
    for i in range(len(items)):
        cur_length = items[i]["length"]
        if cur_length + current_sum <= max_length:
            current_sum += items[i]["length"]
            current_list.append(i)
        else:
            merges.append(current_list)
            current_list = [i]
            current_sum = cur_length
    if len(current_list) > 0:
        merges.append(current_list)
    result = []
    for merge in merges:
        sub_items = [items[index]["index"] for index in merge]
        result.append(sub_items)
    return result


def get_causal_mask(length: int, m_value: float) -> torch.tensor:
    mask = torch.full((length, length), m_value)
    mask_cond = torch.arange(mask.size(-1))
    mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
    return mask

    
def create_mask_from_lengths(lengths: List[int], tokenizer: Any, m_value: float) -> torch.tensor:
    """create attention_mask: N x N where masked value = m_value
    Args:
        lengths (List[int]): length of data points
        tokenizer (Any): _description_
        m_value (float): _description_

    Returns:
        torch.tensor: _description_
    """
    max_length = tokenizer.model_max_length
    result = torch.full((max_length, max_length), m_value)
    acc_leng = 0
    for length in lengths:
        # mask for a data point with length
        x = get_causal_mask(length, m_value)
        result[acc_leng: acc_leng + length, acc_leng: acc_leng + length] = x
        acc_leng += length
    pad_length = max_length - sum(lengths)
    if pad_length > 0:
        result[-pad_length: , :] = 0
        result[:, -pad_length: ] = m_value
    return result


def merge_data_points(data_points: List[Dict], tokenizer: Any) -> Dict:
    input_ids = []
    lengths = []
    label_ids = []
    for item in data_points:
        input_ids += item["input_ids"]
        #assert item["labels"][0] == -100 # This is to make sure that the first token won't be included in computing loss
        labels = list(item["labels"])
        labels[0] = -100
        label_ids += labels
        lengths.append(len(item["input_ids"]))
    attention_mask = create_mask_from_lengths(lengths, tokenizer, float("-inf"))
    pad_leng = tokenizer.model_max_length - len(input_ids)  # padding to model_max_length
    if tokenizer.padding_side == "right":
        input_ids = input_ids + [tokenizer.pad_token_id for _ in range(pad_leng)]
        label_ids = label_ids + [-100 for _ in range(pad_leng)]
    else:
        input_ids = [tokenizer.pad_token_id for _ in range(pad_leng)] + input_ids
        label_ids = [-100 for _ in range(pad_leng)] + label_ids
    assert len(input_ids) == len(label_ids) == attention_mask.size(0)
    return {
        "input_ids": torch.tensor(input_ids), 
        "labels": torch.tensor(label_ids), 
        "attention_mask": torch.unsqueeze(attention_mask, 0)  # This is because the shape is: B x 1 x N x N
    }


def is_valid_labels(labels: Union[List[int], torch.Tensor]) -> bool:
    """by setting max_length, there might be the case that the labels are all -100 -> loss=nan
    Args:
        labels (Union[List[int], torch.Tensor]): _description_

    Returns:
        bool: _description_
    """
    if type(labels) is list:
        non_mask_count = 0
        for label in labels:
            if label != -100:
                non_mask_count += 1
        if non_mask_count == 0:
            return False
        return True
    else:
        if sum(labels + 100) == 0:
            return False
        return True


def remove_invalid_label_items(data_points: List[Dict]):
    """Remove data points where labels are all -100

    Args:
        data_points (List[Dict]): _description_

    Returns:
        _type_: _description_
    """
    result = []
    for dp in data_points:
        if is_valid_labels(dp["labels"]):
            result.append(dp)
    return result
    

class PackedDataset(Dataset):
    def __init__(self, tokenizer: Any, cached_path: Optional[str]=None, ignore_cached: bool = False) -> None:
        super().__init__()
        self.tokenizer = tokenizer
        self.data_points = []
        if cached_path is not None and os.path.exists(os.path.join(cached_path, "inputs.jsonl")) and not ignore_cached:
            print(f"cached found, load from cached: {cached_path}")
            if self.is_loadable_cached(cached_path):
                self.load(cached_path)
    
    def pack_data_points(self, data_points: List[Dict]):
        # remove data points where labels = -100 all the times
        valid_data_points = remove_invalid_label_items(data_points) 
        invalid_count = len(data_points) - len(valid_data_points)
        if invalid_count > 0:
            print(f"Remove {invalid_count} data points with invalid labels")
        self.lengths = [len(item["input_ids"]) for item in valid_data_points]
        self.groups = merge_data_points_by_length(self.lengths, self.tokenizer.model_max_length)
        print(f"pack: {len(data_points)} data points into: {len(self.groups)} data points")
        self.data_points = valid_data_points
        
    def __len__(self):
        return len(self.groups)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        group = self.groups[i]
        group_data_points = [self.data_points[index] for index in group]
        return merge_data_points(group_data_points, self.tokenizer)

    def dump(self, folder):
        if not os.path.exists(folder):
            os.mkdir(folder)
        t1 = datetime.datetime.now()
        with open(f"{folder}/inputs.jsonl", "w") as f:
            for item in self.data_points:
                f.write(json.dumps(item) + "\n")
        t2 = datetime.datetime.now()
        print("time of dumping data: ", (t2 - t1).total_seconds())
        meta_path = f"{folder}/meta_info.json"
        info = {"max_length": self.tokenizer.model_max_length}
        with open(meta_path, "w") as f:
            f.write(json.dumps(info))
    
    def read_max_length_from_cached(self, folder):
        meta_path = f"{folder}/meta_info.json"
        with open(meta_path, "r") as f:
            info = json.loads(f.read())
            return info["max_length"]
    
    def is_loadable_cached(self, folder):
        cached_max_length = self.read_max_length_from_cached(folder)
        if cached_max_length >= self.tokenizer.model_max_length:
            return True
        return False

    def load(self, folder):
        data_points = []
        t1 = datetime.datetime.now()
        cached_max_length = self.read_max_length_from_cached(folder)
        assert cached_max_length >= self.tokenizer.model_max_length
        with open(f"{folder}/inputs.jsonl", "r") as f:
            for line in f:
                temp = line.strip()
                if len(temp) > 0:
                    item = json.loads(temp)
                    for key in item:
                        item[key] = torch.tensor(item[key][: self.tokenizer.model_max_length])
                    data_points.append(item)
        t2 = datetime.datetime.now()
        print("time for loading data from cached:", (t2 - t1).total_seconds())
        self.pack_data_points(data_points)
    
    def stat(self):
        print(f"number of original data points:{len(self.data_points)}; packed to: {len(self.groups)}")
        original_avg_length = sum(self.lengths) / len(self.lengths)
        packed_lengths = []
        for group in self.groups:
            lengths = [self.lengths[index] for index in group]
            packed_lengths.append(sum(lengths))
        avg_packed_length = sum(packed_lengths) / len(packed_lengths)
        print(f"original avg length: {original_avg_length}; avg packed length: {avg_packed_length}")
        
        
class DirectPackedDataset(PackedDataset):
    def __init__(self, raw_data, tokenizer: transformers.PreTrainedTokenizer, cached_path: Optional[str] = None, ignore_cached: bool = False, batch_size: int = 5000):
        super().__init__(tokenizer, cached_path, ignore_cached)
        if len(self.data_points) == 0:
            original_datapoints = []
            data_size = len(raw_data)
            t1 = datetime.datetime.now()
            for start, end in get_batch_indices(data_size, batch_size):
                batch_result = prepare_training_inputs_batch(raw_data[start: end], tokenizer, padding="do_not_pad", return_tensor=False)
                assert len(batch_result["batch_inputs"]) == len(raw_data[start: end])
                for item in batch_result["batch_inputs"]:
                    original_datapoints.append(item)
                t2 = datetime.datetime.now()
                avg_time = (t2 - t1).total_seconds() / len(original_datapoints)
                remaining_time = avg_time * (data_size - len(original_datapoints))
                print(f"{len(original_datapoints)}/{data_size}, avg_time per 1000 data points: {avg_time * 1000}, remaining time: {remaining_time}")
            assert len(original_datapoints) == data_size
            self.pack_data_points(original_datapoints)
            if cached_path is not None: 
                if not os.path.exists(cached_path):
                    print(f"dump data to cached: {cached_path}")
                    self.dump(cached_path)

