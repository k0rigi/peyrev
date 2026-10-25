"""Yer tutucu: ortak model (LLM) uçları uygulanınca create_blueprint gerçek uçları döndürür."""

from flask import Blueprint


def create_blueprint(core) -> Blueprint:
    return Blueprint("studio_llm", __name__)
