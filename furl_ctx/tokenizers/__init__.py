"""Pluggable tokenizer system for universal LLM support.

This module provides a registry-based tokenizer system that supports
multiple backends:

1. tiktoken - OpenAI models (GPT-3.5, GPT-4, GPT-4o)
2. Anthropic - Claude models (via SDK or estimation)
3. Estimation - Fallback for unknown models

Usage:
    from furl_ctx.tokenizers import TokenizerRegistry, get_tokenizer

    # Auto-detect tokenizer from model name
    tokenizer = get_tokenizer("gpt-4o")
    tokens = tokenizer.count_text("Hello, world!")

    # Register custom tokenizer
    TokenizerRegistry.register("my-model", my_tokenizer)
"""

from .base import BaseTokenizer, TokenCounter
from .estimator import CharacterCounter, EstimatingTokenCounter
from .registry import (
    TokenizerRegistry,
    get_tokenizer,
    list_supported_models,
    register_tokenizer,
)
from .tiktoken_counter import TiktokenCounter

__all__ = [
    # Registry
    "TokenizerRegistry",
    "get_tokenizer",
    "register_tokenizer",
    "list_supported_models",
    # Base classes
    "TokenCounter",
    "BaseTokenizer",
    # Implementations
    "TiktokenCounter",
    "EstimatingTokenCounter",
    "CharacterCounter",
]
