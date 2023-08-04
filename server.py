from fastapi import FastAPI
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import uuid
import time
from transformers import LlamaTokenizer, LlamaForCausalLM
import torch
from inference import generate
import uvicorn
import argparse

app = FastAPI()


class ChatInput(BaseModel):
    messages: List[Dict[str, Any]]
    functions: Optional[List[Dict[str, Any]]]
    temperature: float = 0.7  # set a default value


@app.post("/v1/chat/completions")
async def chat_endpoint(chat_input: ChatInput):
    generated_message = generate(model, tokenizer, chat_input.messages, chat_input.functions, chat_input.temperature)

    return {
        'id': str(uuid.uuid4()),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': args.model,
        'choices': [
            {
                'message': generated_message,
                'finish_reason': 'stop',
                'index': 0
            }
        ]
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Functionary API Server")
    parser.add_argument('--model', type=str, default='musabgultekin/functionary-7b-v1', help='Model name')
    parser.add_argument("--load_in_8bit", type=bool, default=False)
    args = parser.parse_args()

    model = LlamaForCausalLM.from_pretrained(args.model, low_cpu_mem_usage=True, device_map='auto', torch_dtype=torch.float16, load_in_8bit=args.load_in_8bit)
    tokenizer = LlamaTokenizer.from_pretrained(args.model, use_fast=False)

    uvicorn.run(app, host="0.0.0.0", port=8000)
