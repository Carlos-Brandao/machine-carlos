"""Bot EASYCONSIG.

O portal usa o mesmo fluxo de autenticação e consulta do RF1, mas mantém
configurações independentes pelo prefixo ``EASYCONSIG_`` no arquivo ``.env``.
"""

from rf1.rf1 import main


__all__ = ["main"]
