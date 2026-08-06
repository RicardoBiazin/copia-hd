"""Clone setor a setor de um disco fisico para outro.

Copiar o disco inteiro (setor 0 ate o fim) preserva a tabela de particoes, a
particao de sistema EFI, a particao reservada, o Windows e a particao de
recuperacao. E isso que faz o HD novo dar boot sozinho.
"""

from __future__ import annotations

import ctypes
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .disks import Disco, formatar_bytes
from .winio import (
    DispositivoBruto,
    WinIOError,
    buffer_alinhado,
    caminho_disco,
    caminho_volume,
)

BLOCO_PADRAO = 4 * 1024 * 1024
TENTATIVAS_LEITURA = 3


class ErroClone(RuntimeError):
    pass


@dataclass
class Progresso:
    total: int
    copiados: int = 0
    inicio: float = field(default_factory=time.monotonic)
    setores_ruins: int = 0
    bytes_zerados: int = 0

    @property
    def percentual(self) -> float:
        return (self.copiados / self.total * 100) if self.total else 0.0

    @property
    def decorrido(self) -> float:
        return max(0.001, time.monotonic() - self.inicio)

    @property
    def taxa(self) -> float:
        return self.copiados / self.decorrido

    @property
    def restante_segundos(self) -> float:
        falta = self.total - self.copiados
        return falta / self.taxa if self.taxa > 0 else float("inf")

    def linha(self) -> str:
        eta = self.restante_segundos
        eta_txt = "--:--:--" if eta == float("inf") else _hms(eta)
        barra_cheia = int(self.percentual / 2.5)
        barra = "#" * barra_cheia + "-" * (40 - barra_cheia)
        extra = f" ruins:{self.setores_ruins}" if self.setores_ruins else ""
        return (
            f"[{barra}] {self.percentual:5.1f}%  "
            f"{formatar_bytes(self.copiados)}/{formatar_bytes(self.total)}  "
            f"{formatar_bytes(int(self.taxa))}/s  restante {eta_txt}{extra}"
        )


def _hms(segundos: float) -> str:
    segundos = int(segundos)
    return f"{segundos // 3600:02d}:{segundos % 3600 // 60:02d}:{segundos % 60:02d}"


@dataclass
class PlanoClone:
    origem: Disco
    destino: Disco
    bytes_a_copiar: int
    setor: int
    bloco: int
    modo: str  # "completo" ou "ate-ultima-particao"
    avisos: list[str] = field(default_factory=list)


def montar_plano(
    origem: Disco,
    destino: Disco,
    bloco: int = BLOCO_PADRAO,
    copiar_disco_inteiro: bool = True,
) -> PlanoClone:
    """Valida a combinacao origem/destino e decide quantos bytes copiar."""
    avisos: list[str] = []
    if origem.numero == destino.numero:
        raise ErroClone("Origem e destino sao o mesmo disco.")
    if destino.tem_sistema or destino.tem_boot:
        raise ErroClone(
            f"O disco {destino.numero} e o disco de sistema/boot em uso. "
            "Nunca use o disco em que o Windows esta rodando como destino."
        )
    if destino.somente_leitura:
        raise ErroClone(f"O disco {destino.numero} esta marcado como somente leitura.")

    with DispositivoBruto(caminho_disco(origem.numero)) as d:
        setor_origem = d.tamanho_setor()
        bytes_origem = d.tamanho_bytes()
    with DispositivoBruto(caminho_disco(destino.numero)) as d:
        setor_destino = d.tamanho_setor()
        bytes_destino = d.tamanho_bytes()

    if setor_origem != setor_destino:
        raise ErroClone(
            f"Tamanho de setor diferente: origem {setor_origem} B, destino "
            f"{setor_destino} B. Um clone setor a setor entre discos 512e e 4Kn "
            "gera um sistema que nao inicializa."
        )

    modo = "completo"
    a_copiar = bytes_origem
    if bytes_destino < bytes_origem:
        fim = origem.fim_ultima_particao
        if not copiar_disco_inteiro and fim and fim <= bytes_destino:
            modo = "ate-ultima-particao"
            a_copiar = _arredondar_para_cima(fim, setor_origem)
            avisos.append(
                "Destino menor que a origem: copiando apenas ate o fim da ultima "
                f"particao ({formatar_bytes(a_copiar)}). Nenhuma particao sera cortada."
            )
        else:
            raise ErroClone(
                f"O destino ({formatar_bytes(bytes_destino)}) e menor que a origem "
                f"({formatar_bytes(bytes_origem)}) e as particoes ocupam ate "
                f"{formatar_bytes(fim)}. Use o modo de copia por arquivos "
                "(--modo arquivos) para um destino menor."
            )
    elif bytes_destino > bytes_origem:
        avisos.append(
            f"Destino tem {formatar_bytes(bytes_destino - bytes_origem)} a mais. "
            "Esse espaco fica sem uso ate a ultima particao ser expandida."
        )

    if origem.tem_sistema or origem.tem_boot:
        avisos.append(
            "A origem e o disco do Windows em execucao. A copia sera feita a quente "
            "e pode ficar inconsistente. Prefira executar de um Windows PE / outro "
            "sistema para uma copia confiavel."
        )

    return PlanoClone(
        origem=origem,
        destino=destino,
        bytes_a_copiar=a_copiar,
        setor=setor_origem,
        bloco=_arredondar_para_cima(bloco, setor_origem),
        modo=modo,
        avisos=avisos,
    )


def _arredondar_para_cima(valor: int, multiplo: int) -> int:
    return ((valor + multiplo - 1) // multiplo) * multiplo


class EstadoRetomada:
    """Guarda o offset ja copiado para permitir continuar um clone interrompido."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.dados: dict = {}

    def carregar(self, assinatura: dict) -> int:
        if not self.caminho.exists():
            return 0
        try:
            dados = json.loads(self.caminho.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if dados.get("assinatura") != assinatura:
            return 0
        self.dados = dados
        return int(dados.get("offset", 0))

    def salvar(self, assinatura: dict, offset: int, setores_ruins: list[int]) -> None:
        self.dados = {
            "assinatura": assinatura,
            "offset": offset,
            "setores_ruins": setores_ruins[-500:],
        }
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.dados, indent=2), encoding="utf-8")
        tmp.replace(self.caminho)

    def concluir(self) -> None:
        self.caminho.unlink(missing_ok=True)


def preparar_destino(numero_disco: int, registrar=print) -> None:
    """Desmonta e tranca os volumes do destino antes de sobrescrever."""
    from .disks import letras_do_disco

    for letra in letras_do_disco(numero_disco):
        try:
            with DispositivoBruto(caminho_volume(letra), escrita=True, sem_buffer=False) as vol:
                travado = vol.travar_volume()
                desmontado = vol.desmontar_volume()
                registrar(
                    f"  Volume {letra}: travado={travado} desmontado={desmontado}"
                )
        except WinIOError as exc:
            registrar(f"  Aviso: nao foi possivel preparar {letra}: - {exc}")


def executar_clone(
    plano: PlanoClone,
    registrar=print,
    aoprogresso=None,
    arquivo_estado: Path | None = None,
    retomar: bool = True,
) -> Progresso:
    """Copia os bytes da origem para o destino, bloco por bloco."""
    assinatura = {
        "origem": plano.origem.serial or plano.origem.modelo,
        "destino": plano.destino.serial or plano.destino.modelo,
        "bytes": plano.bytes_a_copiar,
    }
    estado = EstadoRetomada(arquivo_estado) if arquivo_estado else None
    inicio_offset = estado.carregar(assinatura) if (estado and retomar) else 0
    if inicio_offset:
        registrar(
            f"Retomando clone a partir de {formatar_bytes(inicio_offset)} "
            f"({inicio_offset / plano.bytes_a_copiar * 100:.1f}%)."
        )

    setores_ruins: list[int] = []
    progresso = Progresso(total=plano.bytes_a_copiar, copiados=inicio_offset)
    buffer = buffer_alinhado(plano.bloco)
    ultimo_relato = 0.0
    ultimo_estado = time.monotonic()

    with DispositivoBruto(caminho_disco(plano.origem.numero)) as origem, DispositivoBruto(
        caminho_disco(plano.destino.numero), escrita=True
    ) as destino:
        offset = inicio_offset
        while offset < plano.bytes_a_copiar:
            tamanho = _arredondar_para_cima(
                min(plano.bloco, plano.bytes_a_copiar - offset), plano.setor
            )
            zerados = _ler_bloco(
                origem, offset, tamanho, plano.setor, buffer, setores_ruins, registrar
            )
            progresso.bytes_zerados += zerados
            destino.posicionar(offset)
            destino.escrever_de(buffer, tamanho)

            offset += tamanho
            progresso.copiados = min(offset, plano.bytes_a_copiar)
            progresso.setores_ruins = len(setores_ruins)

            agora = time.monotonic()
            if agora - ultimo_relato >= 0.5:
                if aoprogresso:
                    aoprogresso(progresso)
                ultimo_relato = agora
            if estado and agora - ultimo_estado >= 5:
                estado.salvar(assinatura, offset, setores_ruins)
                ultimo_estado = agora

    if aoprogresso:
        aoprogresso(progresso)
    if estado:
        estado.concluir()
    if setores_ruins:
        registrar(
            f"\nATENCAO: {len(setores_ruins)} setor(es) ilegivel(is) na origem foram "
            "gravados como zeros no destino. O HD de origem esta com defeito fisico; "
            "arquivos nessas areas podem estar corrompidos na copia."
        )
    return progresso


def _ler_bloco(
    origem: DispositivoBruto,
    offset: int,
    tamanho: int,
    setor: int,
    buffer,
    setores_ruins: list[int],
    registrar,
) -> int:
    """Preenche `buffer` com `tamanho` bytes lidos de `offset`.

    Tenta o bloco inteiro; se houver erro de I/O, cai para leitura setor a
    setor e preenche com zeros apenas os setores realmente ilegiveis.
    Retorna quantos bytes foram zerados por defeito de leitura.
    """
    for tentativa in range(TENTATIVAS_LEITURA):
        try:
            origem.posicionar(offset)
            if origem.ler_em(buffer, tamanho) == tamanho:
                return 0
            break
        except WinIOError:
            if tentativa < TENTATIVAS_LEITURA - 1:
                time.sleep(0.05)

    zerados = 0
    for deslocamento in range(0, tamanho, setor):
        pos = offset + deslocamento
        alvo = ctypes.byref(buffer, deslocamento)
        for tentativa in range(TENTATIVAS_LEITURA):
            try:
                origem.posicionar(pos)
                if origem.ler_em(alvo, setor) == setor:
                    break
            except WinIOError:
                if tentativa < TENTATIVAS_LEITURA - 1:
                    time.sleep(0.05)
        else:
            ctypes.memset(alvo, 0, setor)
            setores_ruins.append(pos // setor)
            zerados += setor
            registrar(f"\n  Setor ilegivel na LBA {pos // setor} - gravando zeros.")
    return zerados


def verificar_copia(
    plano: PlanoClone,
    amostras: int = 64,
    registrar=print,
) -> tuple[bool, list[int]]:
    """Compara blocos por amostragem entre origem e destino."""
    import hashlib
    import random

    divergentes: list[int] = []
    tamanho_amostra = _arredondar_para_cima(1 * 1024 * 1024, plano.setor)
    posicoes = [0]
    if plano.bytes_a_copiar > tamanho_amostra:
        aleatorio = random.Random(1234)
        for _ in range(max(0, amostras - 2)):
            limite = plano.bytes_a_copiar - tamanho_amostra
            posicoes.append(_arredondar_para_cima(aleatorio.randrange(limite), plano.setor))
        posicoes.append(
            _arredondar_para_cima(plano.bytes_a_copiar - tamanho_amostra, plano.setor)
        )

    with DispositivoBruto(caminho_disco(plano.origem.numero)) as origem, DispositivoBruto(
        caminho_disco(plano.destino.numero)
    ) as destino:
        for i, pos in enumerate(sorted(set(posicoes)), 1):
            try:
                a = hashlib.sha256(origem.ler(pos, tamanho_amostra)).hexdigest()
                b = hashlib.sha256(destino.ler(pos, tamanho_amostra)).hexdigest()
            except WinIOError as exc:
                registrar(f"  Amostra em {formatar_bytes(pos)}: erro de leitura - {exc}")
                divergentes.append(pos)
                continue
            if a != b:
                divergentes.append(pos)
                registrar(f"  DIVERGENCIA em {formatar_bytes(pos)}")
            registrar(f"  Amostra {i}/{len(set(posicoes))} em {formatar_bytes(pos)}: "
                      f"{'OK' if a == b else 'FALHOU'}")
    return (not divergentes, divergentes)
