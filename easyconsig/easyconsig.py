"""Variação EASYCONSIG do bot RF1.

Mantém o fluxo, seletores e tratamento de CAPTCHA do RF1, mas é executada
com configurações independentes iniciadas por ``EASYCONSIG_`` no arquivo .env.
"""

from rf1.rf1 import main


__all__ = ["main"]
