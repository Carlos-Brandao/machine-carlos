import re
import sys


def digits_only(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def mask_cpf(value: object) -> str:
    """Retorna um identificador seguro para logs, mantendo só os 4 finais."""
    digits = digits_only(value)
    return f"***.***.***-{digits[-4:]}" if digits else "CPF-ausente"


def aguardar_enter(msg: str = "\nPressione ENTER para fechar o navegador...") -> None:
    if not sys.stdin.isatty():
        return
    print(msg, flush=True)
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        pass
    input()
