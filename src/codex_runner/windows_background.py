"""pythonw entry point for source/venv installs; native ZIPs use the tiny GUI launcher."""
import os
import sys


def main():
    # pythonw has no standard streams. CLI errors and imports must still be safe;
    # service run replaces output streams with the component's private log.
    for name, mode in [('stdin', 'r'), ('stdout', 'w'), ('stderr', 'w')]:
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding='utf-8'))
    from .cli import main as cli_main
    return cli_main()


if __name__ == '__main__':
    raise SystemExit(main())
