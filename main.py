"""CloudMount — облачные хранилища как диски Windows.

Запуск:
    python main.py                 — окно управления (и значок в трее)
    python main.py --minimized     — сразу в трей
    python main.py --mount <id>    — служебный режим: процесс одного диска
"""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()
    args = sys.argv[1:]
    if "--mount" in args:
        from cloudmount.mount import run
        sys.exit(run(args[args.index("--mount") + 1]))
    from cloudmount.gui import run_gui
    run_gui(minimized="--minimized" in args)


if __name__ == "__main__":
    main()
