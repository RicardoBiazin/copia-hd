"""Copia por arquivos (robocopy) para quando o HD destino e menor que a origem.

Neste modo o destino e particionado do zero, os arquivos sao copiados com todos
os metadados (ACLs, atributos, timestamps, junctions) e o boot e reconstruido
com bcdboot. Exige que o Windows de origem NAO esteja em execucao (rode de um
Windows PE) para que os arquivos abertos possam ser copiados.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .disks import Disco, formatar_bytes

# arquivos e pastas que nao devem ser copiados: sao recriados pelo Windows
EXCLUIR_ARQUIVOS = [
    "pagefile.sys",
    "swapfile.sys",
    "hiberfil.sys",
    "DumpStack.log",
    "DumpStack.log.tmp",
]
EXCLUIR_PASTAS = [
    "$RECYCLE.BIN",
    "System Volume Information",
    "$WinREAgent",
    "Recovery\\WindowsRE",
]

SCRIPT_DISKPART_GPT = """select disk {disco}
clean
convert gpt
create partition efi size=512
format quick fs=fat32 label="SISTEMA"
assign letter={letra_efi}
create partition msr size=16
create partition primary
format quick fs=ntfs label="{rotulo}"
assign letter={letra_windows}
exit
"""


class ErroCopiaArquivos(RuntimeError):
    pass


@dataclass
class ResultadoRobocopy:
    codigo: int
    log: Path

    @property
    def ok(self) -> bool:
        # robocopy: 0-7 sao sucesso (com ou sem copias); >=8 indica falha real
        return self.codigo < 8

    def descricao(self) -> str:
        mapa = {
            0: "Nenhum arquivo diferente encontrado.",
            1: "Arquivos copiados com sucesso.",
            2: "Arquivos extras no destino.",
            3: "Arquivos copiados; havia extras no destino.",
        }
        if self.codigo >= 8:
            return f"FALHA (codigo {self.codigo}): houve arquivos que nao puderam ser copiados."
        return mapa.get(self.codigo, f"Concluido com codigo {self.codigo}.")


def particionar_destino(
    numero_disco: int,
    letra_windows: str = "W",
    letra_efi: str = "S",
    rotulo: str = "Windows",
    simular: bool = False,
) -> str:
    """APAGA o disco destino e cria o layout GPT padrao do Windows (EFI+MSR+NTFS)."""
    script = SCRIPT_DISKPART_GPT.format(
        disco=numero_disco,
        letra_efi=letra_efi.rstrip(":"),
        letra_windows=letra_windows.rstrip(":"),
        rotulo=rotulo,
    )
    if simular:
        return f"[SIMULACAO] diskpart receberia:\n{script}"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="ascii"
    ) as arq:
        arq.write(script)
        caminho = arq.name
    try:
        proc = subprocess.run(
            ["diskpart.exe", "/s", caminho], capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            raise ErroCopiaArquivos(
                f"diskpart falhou (codigo {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
        return proc.stdout
    finally:
        Path(caminho).unlink(missing_ok=True)


def copiar_volume(
    origem: str,
    destino: str,
    log: Path,
    threads: int = 16,
    simular: bool = False,
    registrar=print,
) -> ResultadoRobocopy:
    """Espelha origem em destino preservando metadados NTFS."""
    origem = origem.rstrip("\\") + "\\"
    destino = destino.rstrip("\\") + "\\"
    comando = [
        "robocopy.exe",
        origem,
        destino,
        "/MIR",  # espelha (inclui subpastas vazias e remove extras)
        "/COPYALL",  # dados, atributos, timestamps, ACL, owner, auditoria
        "/DCOPY:DAT",  # metadados das pastas
        "/SL",  # copia links simbolicos como links
        "/XJ",  # nao segue junctions (evita loop infinito)
        "/R:2",
        "/W:2",
        f"/MT:{threads}",
        "/NP",
        "/NFL",
        "/NDL",
        "/TEE",
        f"/LOG+:{log}",
        "/XF",
        *EXCLUIR_ARQUIVOS,
        "/XD",
        *[f"{origem}{p}" for p in EXCLUIR_PASTAS],
    ]
    if simular:
        comando.insert(3, "/L")  # apenas lista o que faria
        registrar("[SIMULACAO] robocopy em modo /L (nenhuma escrita).")
    registrar(f"  {' '.join(comando[:4])} ... (log em {log})")
    proc = subprocess.run(comando, capture_output=True, text=True)
    return ResultadoRobocopy(codigo=proc.returncode, log=log)


def reconstruir_boot(
    letra_windows: str,
    letra_efi: str,
    firmware: str = "UEFI",
    simular: bool = False,
) -> str:
    """Recria os arquivos de boot no destino com bcdboot."""
    windows = f"{letra_windows.rstrip(':')}:\\Windows"
    comando = [
        "bcdboot.exe",
        windows,
        "/s",
        f"{letra_efi.rstrip(':')}:",
        "/f",
        firmware,
        "/l",
        "pt-br",
    ]
    if simular:
        return f"[SIMULACAO] {' '.join(comando)}"
    proc = subprocess.run(comando, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise ErroCopiaArquivos(
            f"bcdboot falhou (codigo {proc.returncode}): {proc.stdout} {proc.stderr}"
        )
    return proc.stdout.strip()


def espaco_necessario(origem: Disco) -> int:
    return origem.espaco_usado


def cabe_no_destino(origem: Disco, destino: Disco, margem: float = 1.15) -> tuple[bool, str]:
    """Verifica se os dados da origem cabem no destino, com margem de folga."""
    preciso = int(espaco_necessario(origem) * margem)
    if destino.tamanho >= preciso:
        return True, (
            f"Dados em uso: {formatar_bytes(origem.espaco_usado)}; com folga de "
            f"{int((margem - 1) * 100)}%: {formatar_bytes(preciso)}; destino: "
            f"{formatar_bytes(destino.tamanho)}."
        )
    return False, (
        f"Destino insuficiente: precisa de ~{formatar_bytes(preciso)} e tem "
        f"{formatar_bytes(destino.tamanho)}."
    )
