"""Inventario de discos, particoes e volumes usando os cmdlets Storage."""

from __future__ import annotations

import ctypes
import json
import subprocess
from dataclasses import dataclass, field


class ErroInventario(RuntimeError):
    pass


def eh_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _powershell(script: str, timeout: int = 120) -> str:
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ErroInventario(
            f"PowerShell retornou {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def _json_powershell(script: str, timeout: int = 120):
    # o pipe precisa ficar na mesma linha do fim do script, senao o PowerShell
    # entende um elemento de pipeline vazio
    saida = _powershell(f"{script.strip()} | ConvertTo-Json -Depth 4 -Compress", timeout)
    saida = saida.strip()
    if not saida:
        return []
    dados = json.loads(saida)
    return dados if isinstance(dados, list) else [dados]


def formatar_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    unidades = ["B", "KB", "MB", "GB", "TB", "PB"]
    valor = float(n)
    for unidade in unidades:
        if valor < 1024 or unidade == unidades[-1]:
            return f"{valor:,.1f} {unidade}".replace(",", ".")
        valor /= 1024
    return f"{n} B"


@dataclass
class Particao:
    numero: int
    offset: int
    tamanho: int
    tipo: str
    letra: str | None
    rotulo: str | None
    sistema_arquivos: str | None
    espaco_livre: int | None
    usado: int | None

    def resumo(self) -> str:
        letra = f"{self.letra}:" if self.letra else "--"
        rotulo = self.rotulo or self.tipo or ""
        return (
            f"    #{self.numero} {letra:<3} {formatar_bytes(self.tamanho):>10}  "
            f"{(self.sistema_arquivos or '-'):<6} {rotulo}"
        )


@dataclass
class Disco:
    numero: int
    modelo: str
    serial: str | None
    tamanho: int
    barramento: str
    estilo_particao: str
    status: str
    somente_leitura: bool
    tem_boot: bool
    tem_sistema: bool
    particoes: list[Particao] = field(default_factory=list)

    @property
    def espaco_usado(self) -> int:
        """Soma o espaco realmente ocupado pelos dados dos volumes."""
        total = 0
        for p in self.particoes:
            total += p.usado if p.usado is not None else p.tamanho
        return total

    @property
    def fim_ultima_particao(self) -> int:
        if not self.particoes:
            return 0
        return max(p.offset + p.tamanho for p in self.particoes)

    def resumo(self) -> str:
        marcas = []
        if self.tem_sistema:
            marcas.append("SISTEMA")
        if self.tem_boot:
            marcas.append("BOOT")
        if self.somente_leitura:
            marcas.append("SOMENTE-LEITURA")
        sufixo = f"  [{', '.join(marcas)}]" if marcas else ""
        return (
            f"  Disco {self.numero}: {self.modelo} - {formatar_bytes(self.tamanho)} "
            f"({self.barramento}, {self.estilo_particao}, {self.status}){sufixo}"
        )

    def detalhe(self) -> str:
        linhas = [self.resumo()]
        linhas.extend(p.resumo() for p in self.particoes)
        return "\n".join(linhas)


_SCRIPT_DISCOS = r"""
Get-Disk | ForEach-Object {
  $d = $_
  $parts = @()
  try {
    $parts = Get-Partition -DiskNumber $d.Number -ErrorAction Stop | ForEach-Object {
      $p = $_
      $vol = $null
      try { $vol = Get-Volume -Partition $p -ErrorAction Stop } catch {}
      [pscustomobject]@{
        Numero      = $p.PartitionNumber
        Offset      = [int64]$p.Offset
        Tamanho     = [int64]$p.Size
        Tipo        = [string]$p.Type
        Letra       = if ($p.DriveLetter -and "$($p.DriveLetter)" -ne "`0") { "$($p.DriveLetter)" } else { $null }
        Rotulo      = if ($vol) { $vol.FileSystemLabel } else { $null }
        FS          = if ($vol) { $vol.FileSystemType } else { $null }
        Livre       = if ($vol -and $vol.SizeRemaining -ne $null) { [int64]$vol.SizeRemaining } else { $null }
        VolTamanho  = if ($vol -and $vol.Size -ne $null) { [int64]$vol.Size } else { $null }
      }
    }
  } catch {}
  [pscustomobject]@{
    Numero     = $d.Number
    Modelo     = "$($d.FriendlyName)".Trim()
    Serial     = "$($d.SerialNumber)".Trim()
    Tamanho    = [int64]$d.Size
    Barramento = [string]$d.BusType
    Estilo     = [string]$d.PartitionStyle
    Status     = [string]$d.OperationalStatus
    ReadOnly   = [bool]$d.IsReadOnly
    Boot       = [bool]$d.IsBoot
    Sistema    = [bool]$d.IsSystem
    Particoes  = @($parts)
  }
}
"""


def listar_discos() -> list[Disco]:
    discos: list[Disco] = []
    for item in _json_powershell(_SCRIPT_DISCOS):
        particoes = []
        for p in item.get("Particoes") or []:
            livre = p.get("Livre")
            vol_tamanho = p.get("VolTamanho")
            usado = None
            if livre is not None and vol_tamanho:
                usado = max(0, vol_tamanho - livre)
            particoes.append(
                Particao(
                    numero=p.get("Numero") or 0,
                    offset=p.get("Offset") or 0,
                    tamanho=p.get("Tamanho") or 0,
                    tipo=p.get("Tipo") or "",
                    letra=p.get("Letra"),
                    rotulo=p.get("Rotulo"),
                    sistema_arquivos=p.get("FS"),
                    espaco_livre=livre,
                    usado=usado,
                )
            )
        discos.append(
            Disco(
                numero=item["Numero"],
                modelo=item.get("Modelo") or "(sem nome)",
                serial=item.get("Serial") or None,
                tamanho=item.get("Tamanho") or 0,
                barramento=item.get("Barramento") or "?",
                estilo_particao=item.get("Estilo") or "?",
                status=item.get("Status") or "?",
                somente_leitura=bool(item.get("ReadOnly")),
                tem_boot=bool(item.get("Boot")),
                tem_sistema=bool(item.get("Sistema")),
                particoes=sorted(particoes, key=lambda x: x.offset),
            )
        )
    return sorted(discos, key=lambda d: d.numero)


def obter_disco(numero: int) -> Disco:
    for d in listar_discos():
        if d.numero == numero:
            return d
    raise ErroInventario(f"Disco {numero} nao encontrado.")


def definir_estado_disco(numero: int, online: bool) -> None:
    estado = "Online" if online else "Offline"
    _powershell(
        f"Set-Disk -Number {numero} -IsOffline ${'false' if online else 'true'} "
        f"-ErrorAction Stop; Write-Output 'Disco {numero} -> {estado}'"
    )


def letras_do_disco(numero: int) -> list[str]:
    disco = obter_disco(numero)
    return [p.letra for p in disco.particoes if p.letra]
