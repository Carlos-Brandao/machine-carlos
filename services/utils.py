def aguardar_enter(msg: str = "\nPressione ENTER para fechar o navegador...") -> None:
    print(msg, flush=True)
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except ImportError:
        pass
    input()
