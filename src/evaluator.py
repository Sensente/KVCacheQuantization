import os
import json
import math
import torch
from tqdm import tqdm
from question import Question
from typing import Optional, Any
from torch.nn import functional as F
from matplotlib import pyplot as plt
from functools import cached_property
from dataclasses import dataclass, asdict
from transformers import LlamaForCausalLM
from quantizer import Quantizer, AttentionType


@dataclass
class EvaluationResult:
    accuracy: float = 0.0
    accuracy_confidence: float = 0.0
    answer_log_probability: float = 0.0
    quantization_error: float = 0.0
    key_quantization_error: float = 0.0
    value_quantization_error: float = 0.0
    attention_error: float = 0.0
    logit_error: float = 0.0
    average_n_bits: float = 0.0
    key_average_n_bits: float = 0.0
    value_average_n_bits: float = 0.0
    average_size: float = 0.0
    key_average_size: float = 0.0
    value_average_size: float = 0.0


class Evaluator:
    def __init__(self, device: torch.device, version: str,
                 model: LlamaForCausalLM,
                 questions: list[Question],
                 key_quantizer: Quantizer,
                 value_quantizer: Quantizer,
                 draw_cache_insights: bool):
        self.device = device
        self.version = version
        self.model = model
        self.questions = questions
        self.key_quantizer = key_quantizer
        self.value_quantizer = value_quantizer
        self.enable_draw_cache_insights = draw_cache_insights
        if draw_cache_insights:
            assert self.key_quantizer.level == "token"
            assert self.value_quantizer.level == "token"

    @cached_property
    def params(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        res["version"] = self.version
        res["model_name"] = self.model.name_or_path
        res["question_count"] = len(self.questions)
        res["key_quantizer"] = self.key_quantizer.params
        res["value_quantizer"] = self.value_quantizer.params
        return res

    def _calc_tensor_error(self, tensor1: torch.Tensor, tensor2: torch.Tensor) -> float:
        return ((tensor1.to(self.device) - tensor2.to(self.device)) ** 2).mean().item()

    def _calc_attention_error(self, attention1: AttentionType, attention2: AttentionType) -> float:
        return sum(self._calc_tensor_error(attn1, attn2) for attn1, attn2 in zip(attention1, attention2)) / len(attention1)

    def _evaluate_single(self, idx: int, question: Question) -> EvaluationResult:
        question_len = question.question_length
        # Forward before quantization
        input_ids = question.input_ids.to(self.device)
        result = self.model.forward(input_ids, use_cache=True, output_attentions=True, return_dict=True)
        # Quantize key/value cache
        question_attentions = [attn[:,:,:question_len,:question_len].to(self.device) for attn in result.attentions]
        key_cache = torch.stack([key[:,:,:question_len,:].to(self.device) for key, _ in result.past_key_values])
        value_cache = torch.stack([value[:,:,:question_len,:].to(self.device) for _, value in result.past_key_values])
        quantized_key_cache, key_average_n_bits = self.key_quantizer.quantize(key_cache, question_attentions)
        quantized_value_cache, value_average_n_bits = self.value_quantizer.quantize(value_cache, question_attentions)
        quantized_kvcache = list(zip(quantized_key_cache, quantized_value_cache))
        if self.enable_draw_cache_insights and idx == 0:
            self.draw_cache_insights({
                "Key cache": key_cache,
                "Quantized key cache": quantized_key_cache,
                "Value cache": value_cache,
                "Quantized value cache": quantized_value_cache,
            })
        # Forward after quantization
        quantized_result = self.model.forward(input_ids[:,question_len:], past_key_values=quantized_kvcache, use_cache=True, output_attentions=True, return_dict=True)
        # Calculate log probabilities
        first_word_log_softmax = F.log_softmax(result.logits[:,question_len-1], dim=-1)
        quantized_log_softmax = F.log_softmax(quantized_result.logits, dim=-1)
        max_log_probability, max_choice_idx, answer_log_probability = None, None, None
        for choice_idx, choice_len in enumerate(question.choice_length):
            quantized_log_probability = first_word_log_softmax[choice_idx, input_ids[choice_idx, question_len]].item()
            quantized_log_probability += quantized_log_softmax[choice_idx, torch.arange(choice_len-1), input_ids[choice_idx,question_len+1:question_len+choice_len]].sum().item()
            quantized_log_probability /= choice_len
            if choice_idx == question.answer_idx:
                answer_log_probability = quantized_log_probability
            if max_log_probability is None or quantized_log_probability > max_log_probability:
                max_log_probability = quantized_log_probability
                max_choice_idx = choice_idx
        # Calculate quantization metrics
        key_quantization_error = self._calc_tensor_error(key_cache, quantized_key_cache)
        value_quantization_error = self._calc_tensor_error(value_cache, quantized_value_cache)
        attention_error = self._calc_attention_error(
            [attn[:,:,question_len:,:question_len].to(self.device) for attn in result.attentions],
            [attn[:,:,:,:question_len].to(self.device) for attn in quantized_result.attentions],
        )
        logit_error = self._calc_tensor_error(result.logits[:,question_len:,:], quantized_result.logits)
        key_average_size = self.key_quantizer.calc_quantized_cache_size_per_token(key_average_n_bits, self.model)
        value_average_size = self.value_quantizer.calc_quantized_cache_size_per_token(value_average_n_bits, self.model)
        return EvaluationResult(
            accuracy=1.0 if max_choice_idx == question.answer_idx else 0.0,
            answer_log_probability=answer_log_probability,
            quantization_error=(key_quantization_error + value_quantization_error) / 2,
            key_quantization_error=key_quantization_error,
            value_quantization_error=value_quantization_error,
            attention_error=attention_error,
            logit_error=logit_error,
            average_size=(key_average_size + value_average_size) / 2,
            key_average_size=key_average_size,
            value_average_size=value_average_size,
            average_n_bits=(key_average_n_bits + value_average_n_bits) / 2,
            key_average_n_bits=key_average_n_bits,
            value_average_n_bits=value_average_n_bits,
        )

    def evaluate(self, use_tqdm: bool) -> EvaluationResult:
        result = EvaluationResult()
        total_tokens = 0
        with torch.no_grad():
            for idx, question in enumerate(tqdm(self.questions) if use_tqdm else self.questions):
                single_result = self._evaluate_single(idx, question)
                n_tokens = question.question_length
                total_tokens += n_tokens
                result.accuracy += single_result.accuracy
                result.answer_log_probability += single_result.answer_log_probability
                result.quantization_error += single_result.quantization_error
                result.key_quantization_error += single_result.key_quantization_error
                result.value_quantization_error += single_result.value_quantization_error
                result.attention_error += single_result.attention_error
                result.logit_error += single_result.logit_error
                result.average_size += single_result.average_size * n_tokens
                result.key_average_size += single_result.key_average_size * n_tokens
                result.value_average_size += single_result.value_average_size * n_tokens
                result.average_n_bits += single_result.average_n_bits * n_tokens
                result.key_average_n_bits += single_result.key_average_n_bits * n_tokens
                result.value_average_n_bits += single_result.value_average_n_bits * n_tokens
        result.accuracy /= len(self.questions)
        # Calculate 95% confidence interval
        result.accuracy_confidence = 1.96 * math.sqrt(result.accuracy * (1.0 - result.accuracy) / len(self.questions))
        result.answer_log_probability /= len(self.questions)
        result.quantization_error /= len(self.questions)
        result.key_quantization_error /= len(self.questions)
        result.value_quantization_error /= len(self.questions)
        result.attention_error /= len(self.questions)
        result.logit_error /= len(self.questions)
        result.average_size /= total_tokens
        result.key_average_size /= total_tokens
        result.value_average_size /= total_tokens
        result.average_n_bits /= total_tokens
        result.key_average_n_bits /= total_tokens
        result.value_average_n_bits /= total_tokens
        return result

    def cached_evaluate(self, cache_file: Optional[str], use_tqdm: bool) -> EvaluationResult:
        if cache_file is not None and os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cached_results = json.load(f)
                for entry in cached_results:
                    if entry["params"] == self.params:
                        return EvaluationResult(**entry["results"])
        else:
            cached_results = []
        result = self.evaluate(use_tqdm)
        cached_results.append({
            "params": self.params,
            "results": asdict(result),
        })
        if cache_file is not None:
            with open(cache_file, "w") as f:
                json.dump(cached_results, f, indent=4, separators=(", ", ": "))
        return result

    def draw_cache_insights(self, caches: dict[str, torch.Tensor]) -> None:
        # cache.shape: (n_layer, n_batch, n_head, seq_len, embed_size_per_head)
        assert self.key_quantizer.level == "token"
        assert self.value_quantizer.level == "token"
        # Plot std of caches
        plt.figure(figsize=(10, 6))
        for name, cache in caches.items():
            cache_std = cache[:,0,:,:,:].to(torch.float64).std(dim=(0, 1, 3)).detach().cpu().numpy()
            plt.plot(cache_std, label=name)
        plt.legend()
        plt.title("Std of caches at different tokens")
        plt.xlabel("Token index")
        plt.ylabel("Std")
        plt.tight_layout()
        plt.savefig("figs/cache_std.png", dpi=400)
        # Plot distribution of caches
        print("(min/max/std/mean)")
        plt.figure(figsize=(10, 2*len(caches)))
        for idx, (name, cache) in enumerate(caches.items()):
            # Do not use the first token because it is <s>.
            cache = cache[:,0,:,1,:].reshape(-1).to(torch.float64).detach().cpu().numpy()
            print(f"{name:30} {cache.min().item():.4f} {cache.max().item():.4f} {cache.std().item():.4f} {cache.mean().item():.4f}")
            ax = plt.subplot(len(caches), 1, idx+1)
            ax.hist(cache, bins=1000, range=(-5, 5))
            ax.set_title(name)
        plt.tight_layout()
        plt.savefig("figs/cache_distribution.png", dpi=400)
