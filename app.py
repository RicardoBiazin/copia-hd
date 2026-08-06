"""Copia_hd - ponto de entrada.

Uso:
    python app.py                    menu interativo
    python app.py listar             mostra discos e particoes
    python app.py clonar 0 1         clona o disco 0 no disco 1 (setor a setor)
    python app.py clonar 0 1 --simular
    python app.py arquivos 0 1       copia por arquivos (destino menor)
    python app.py ajustar 1          corrige GPT e expande particao de um clone
    python app.py verificar 0 1      confere a copia por amostragem

Precisa ser executado como administrador para acessar discos fisicos.
"""

from __future__ import annotations

import sys

if sys.platform != "win32":
    print("Este sistema funciona apenas no Windows.")
    raise SystemExit(3)

from copiahd.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
