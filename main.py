import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BOTS = ["rf1", "fenix", "facil", "grid"]
ROOT = Path(__file__).parent


def pick_file() -> str:
    root = tk.Tk()
    root.withdraw()
    root.lift()
    path = filedialog.askopenfilename(
        title="Selecione o arquivo de entrada",
        filetypes=[("Planilhas", "*.xlsx *.csv"), ("Todos", "*.*")],
    )
    root.destroy()
    return path


def get_convenios(bot: str) -> list[str]:
    val = os.getenv(f"{bot.upper()}_CONVENIOS", "")
    return [c.strip().lower() for c in val.split(",") if c.strip()]


def get_config(bot: str, convenio: str) -> dict:
    prefix = f"{bot.upper()}_{convenio.upper()}_"
    config = {
        key[len(prefix):].lower(): val
        for key, val in os.environ.items()
        if key.startswith(prefix)
    }
    if not config:
        print(f"Erro: nenhuma configuração encontrada para [{bot}/{convenio}]. Verifique o .env.")
        sys.exit(1)
    return config


def find_resume(bot: str, convenio: str) -> tuple[Path, Path, Path] | None:
    temp_dir = ROOT / "temp"
    pattern = f"{bot}_{convenio}_*.csv" if bot == "facil" else f"{bot}_{convenio}_*.xlsx"
    candidates = sorted(temp_dir.glob(pattern))
    if not candidates:
        return None
    slug = candidates[-1].stem
    data_files = list((ROOT / "data").glob(f"{slug}.*"))
    if not data_files:
        return None
    temp_file = ROOT / "temp" / f"{slug}.xlsx"
    return data_files[0], temp_file, ROOT / "completed" / f"{slug}.xlsx"


def _count_temp(temp_file: Path, bot: str) -> int:
    try:
        f = temp_file.with_suffix(".csv") if bot == "facil" else temp_file
        if not f.exists():
            return 0
        return len(pd.read_csv(f, dtype=str) if bot == "facil" else pd.read_excel(f, dtype=str))
    except Exception:
        return 0


def run_bot(bot: str, config: dict, input_file: Path, temp_file: Path, output_file: Path):
    sys.path.insert(0, str(ROOT))
    if bot == "rf1":
        from rf1.rf1 import main
        main(config=config, input_file=input_file, temp_file=temp_file, output_file=output_file)
    elif bot == "fenix":
        from fenix.fenix import main
        main(config=config, input_file=input_file, temp_file=temp_file, output_file=output_file)
    elif bot == "facil":
        from facil.facil import main
        main(config=config, input_file=input_file, temp_file=temp_file, output_file=output_file)
    elif bot == "grid":
        from grid.grid import main
        main(config=config, input_file=input_file, temp_file=temp_file, output_file=output_file)


def main():
    parser = argparse.ArgumentParser(description="Dispatcher de bots de consulta de margem")
    parser.add_argument("bot", choices=BOTS, nargs="?", default="facil", help="Bot a executar (padrão: facil)")
    parser.add_argument("convenio", nargs="?", help="Convênio a consultar")
    parser.add_argument("--list", action="store_true", help="Lista convênios disponíveis")
    parser.add_argument("-y", "--yes", action="store_true", help="Aceita retomadas automaticamente (útil para automações/cron)")
    parser.add_argument("-f", "--file", help="Caminho do arquivo de entrada (se omitido, abre a interface gráfica)")
    parser.add_argument("--cron", action="store_true", help="Executa no modo cron (valida intervalo de 15 dias e retomadas automáticas)")
    args = parser.parse_args()

    if args.cron:
        args.yes = True
        if not args.file:
            args.file = str(ROOT / "data" / "base_paulista_prev.xlsx")

    convenios = get_convenios(args.bot)

    if args.list:
        if convenios:
            print(f"Convênios disponíveis para {args.bot}: {', '.join(convenios)}")
        else:
            print(f"Nenhum convênio configurado para {args.bot}.")
        return

    if not convenios:
        print(f"Erro: configure {args.bot.upper()}_CONVENIOS no .env.")
        sys.exit(1)

    if len(convenios) == 1 and not args.convenio:
        convenio = convenios[0]
    elif not args.convenio:
        print(f"Especifique o convênio. Disponíveis: {', '.join(convenios)}")
        sys.exit(1)
    else:
        convenio = args.convenio.lower()
        if convenio not in convenios:
            print(f"Convênio '{convenio}' não encontrado. Disponíveis: {', '.join(convenios)}")
            sys.exit(1)

    config = get_config(args.bot, convenio)
    config["convenio"] = convenio

    for folder in ["data", "temp", "completed"]:
        (ROOT / folder).mkdir(exist_ok=True)

    if args.cron:
        # Se não houver arquivo de retomada, validar se já se passaram 15 dias
        resume = find_resume(args.bot, convenio)
        if not resume:
            completed_dir = ROOT / "completed"
            pattern = f"{args.bot}_{convenio}_*.xlsx"
            completed_files = sorted(completed_dir.glob(pattern))
            if completed_files:
                last_file = completed_files[-1]
                try:
                    ts_str = last_file.stem.replace(f"{args.bot}_{convenio}_", "")
                    last_run_dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                    days_elapsed = (datetime.now() - last_run_dt).days
                    if days_elapsed < 15:
                        print(f"[CRON] Última execução concluída em {last_run_dt.strftime('%d/%m/%Y %H:%M:%S')}.")
                        print(f"[CRON] Apenas {days_elapsed} dias se passaram (mínimo de 15 dias). Encerrando sem nova execução.")
                        sys.exit(0)
                except Exception as e:
                    print(f"[AVISO] Não foi possível ler data do último arquivo: {e}")

    resume = find_resume(args.bot, convenio)
    if resume:
        r_input, r_temp, r_output = resume
        n = _count_temp(r_temp, args.bot)
        print(f"\nRetomada disponível: {n} registros já processados")
        print(f"  Arquivo: {r_input.name}")
        if args.yes:
            ans = "s"
        else:
            ans = input("Retomar de onde parou? [S/n]: ").strip().lower()
        if ans in ("", "s"):
            print(f"\nRetomando {r_input.relative_to(ROOT)}")
            print(f"Salvamento parcial:  {r_temp.relative_to(ROOT)}")
            print(f"Saída final:         {r_output.relative_to(ROOT)}\n")
            run_bot(args.bot, config, r_input, r_temp, r_output)
            return
        print()

    if args.file:
        src = args.file
        if not Path(src).exists():
            print(f"Erro: arquivo não encontrado em '{src}'")
            sys.exit(1)
    else:
        print("Selecione o arquivo de entrada...")
        src = pick_file()
        if not src:
            print("Nenhum arquivo selecionado.")
            sys.exit(0)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = f"{args.bot}_{convenio}_{ts}"
    ext = Path(src).suffix

    input_file = ROOT / "data" / f"{slug}{ext}"
    temp_file = ROOT / "temp" / f"{slug}.xlsx"
    output_file = ROOT / "completed" / f"{slug}.xlsx"

    shutil.copy(src, input_file)
    print(f"Arquivo copiado para: {input_file.relative_to(ROOT)}")
    print(f"Salvamento parcial:   {temp_file.relative_to(ROOT)}")
    print(f"Saída final:          {output_file.relative_to(ROOT)}")
    print()

    run_bot(args.bot, config, input_file, temp_file, output_file)


if __name__ == "__main__":
    main()
