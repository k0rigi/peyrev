"""Yer tutucu: modül uygulanınca create_blueprint gerçek uçları döndürür."""

from flask import Blueprint


def create_blueprint(core) -> Blueprint:
    return Blueprint("accounts", __name__)
