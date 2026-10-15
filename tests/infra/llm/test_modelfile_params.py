"""Ollama /api/show `parameters`: recorded in RunMeta as the effective Modelfile parameters."""

from __future__ import annotations

from escalator.llm.providers.ollama import parse_modelfile_parameters


def test_parameters_text_is_parsed() -> None:
    text = 'temperature                    1\ntop_k                          64\ntop_p                          0.95\n' \
           'stop                           "<end_of_turn>"\nstop                           "<eos>"\n'
    assert parse_modelfile_parameters(text) == {
        "temperature": "1", "top_k": "64", "top_p": "0.95", "stop": ["<end_of_turn>", "<eos>"]}


def test_missing_parameters_give_an_empty_mapping() -> None:
    assert parse_modelfile_parameters(None) == {}
    assert parse_modelfile_parameters("") == {}
