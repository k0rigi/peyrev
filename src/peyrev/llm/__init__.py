"""Ortak model (LLM): Studio'nun /api/studio/ekip/llm/* uçları ve ayrı çalıştırıcı (python -m peyrev.llm.worker).

Çalıştırıcı yalnız standart kütüphane ve psycopg yükler; Flask uçları ancak create_blueprint çağrılınca içe aktarılır.
"""


def create_blueprint(core):
    from .routes import create_blueprint as build

    return build(core)
