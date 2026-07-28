import os
import sys

def aguardar_enter(msg: str = "\nPressione ENTER para fechar o navegador...") -> None:
    if os.environ.get('HEADLESS', 'False').lower() == 'true' or not sys.stdin.isatty():
        return
    print(msg, flush=True)
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        pass
    try:
        input()
    except Exception:
        pass
