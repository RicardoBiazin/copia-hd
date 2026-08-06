"""Leitura e correcao de tabelas de particao GPT e MBR no disco destino.

Apos um clone setor a setor para um disco de tamanho diferente, a GPT fica
inconsistente: o cabecalho secundario aponta para o ultimo setor do disco de
ORIGEM. Este modulo reescreve os cabecalhos primario e secundario para o
tamanho real do destino e, opcionalmente, troca o identificador do disco para
que origem e destino possam ficar ligadas ao mesmo tempo sem conflito.
"""

from __future__ import annotations

import os
import struct
import uuid
import zlib
from dataclasses import dataclass

from .winio import DispositivoBruto

ASSINATURA_GPT = b"EFI PART"
TAMANHO_CABECALHO = 92


@dataclass
class CabecalhoGPT:
    revisao: int
    tamanho_cabecalho: int
    minha_lba: int
    lba_alternativa: int
    primeira_lba_usavel: int
    ultima_lba_usavel: int
    guid_disco: bytes
    lba_entradas: int
    num_entradas: int
    tamanho_entrada: int
    crc_entradas: int
    bruto: bytes

    @property
    def bytes_entradas(self) -> int:
        return self.num_entradas * self.tamanho_entrada

    @property
    def guid_texto(self) -> str:
        return str(uuid.UUID(bytes_le=self.guid_disco)).upper()


def _crc32(dados: bytes) -> int:
    return zlib.crc32(dados) & 0xFFFFFFFF


def analisar_cabecalho(bloco: bytes) -> CabecalhoGPT | None:
    if len(bloco) < TAMANHO_CABECALHO or bloco[:8] != ASSINATURA_GPT:
        return None
    (
        _assinatura,
        revisao,
        tamanho_cabecalho,
        crc_cabecalho,
        _reservado,
        minha_lba,
        lba_alternativa,
        primeira,
        ultima,
        guid,
        lba_entradas,
        num_entradas,
        tamanho_entrada,
        crc_entradas,
    ) = struct.unpack("<8sIIII QQQQ 16s QIII", bloco[:TAMANHO_CABECALHO])

    verificacao = bytearray(bloco[:tamanho_cabecalho])
    verificacao[16:20] = b"\x00\x00\x00\x00"
    if _crc32(bytes(verificacao)) != crc_cabecalho:
        return None

    return CabecalhoGPT(
        revisao=revisao,
        tamanho_cabecalho=tamanho_cabecalho,
        minha_lba=minha_lba,
        lba_alternativa=lba_alternativa,
        primeira_lba_usavel=primeira,
        ultima_lba_usavel=ultima,
        guid_disco=guid,
        lba_entradas=lba_entradas,
        num_entradas=num_entradas,
        tamanho_entrada=tamanho_entrada,
        crc_entradas=crc_entradas,
        bruto=bloco[:tamanho_cabecalho],
    )


def _montar_cabecalho(
    modelo: CabecalhoGPT,
    minha_lba: int,
    lba_alternativa: int,
    lba_entradas: int,
    primeira_usavel: int,
    ultima_usavel: int,
    guid_disco: bytes,
    crc_entradas: int,
) -> bytes:
    corpo = bytearray(modelo.bruto)
    struct.pack_into("<QQ", corpo, 24, minha_lba, lba_alternativa)
    struct.pack_into("<QQ", corpo, 40, primeira_usavel, ultima_usavel)
    corpo[56:72] = guid_disco
    struct.pack_into("<Q", corpo, 72, lba_entradas)
    struct.pack_into("<I", corpo, 88, crc_entradas)
    struct.pack_into("<I", corpo, 16, 0)
    struct.pack_into("<I", corpo, 16, _crc32(bytes(corpo)))
    return bytes(corpo)


def _setores_para(bytes_totais: int, setor: int) -> int:
    return (bytes_totais + setor - 1) // setor


def corrigir_gpt(
    caminho_disco: str,
    novo_guid: bool = False,
    simular: bool = False,
) -> dict:
    """Reescreve os cabecalhos GPT do disco para o seu tamanho real.

    `novo_guid` troca o identificador do disco. Por padrao ele NAO e trocado: a
    configuracao de boot (BCD) copiada da origem referencia o disco pelo GUID, e
    trocar sem reconstruir o boot com bcdboot faz o Windows nao inicializar. Use
    novo_guid apenas se precisar manter os dois HDs ligados ao mesmo tempo, e
    reconstrua o boot depois.

    Retorna um relatorio com o que foi (ou seria) alterado.
    """
    relatorio: dict = {"tipo": None, "acoes": [], "simulado": simular}
    with DispositivoBruto(caminho_disco, escrita=not simular) as disco:
        setor = disco.tamanho_setor()
        total_bytes = disco.tamanho_bytes()
        total_setores = total_bytes // setor
        ultima_lba = total_setores - 1
        relatorio.update(
            {"setor": setor, "total_bytes": total_bytes, "total_setores": total_setores}
        )

        mbr = disco.ler(0, setor)
        primario_bruto = disco.ler(setor, setor)
        cab = analisar_cabecalho(primario_bruto)

        if cab is None:
            relatorio["tipo"] = "MBR"
            relatorio["acoes"].append(
                "Disco em MBR (ou GPT primaria ilegivel): cabecalho GPT nao reescrito."
            )
            if novo_guid:
                nova_assinatura = os.urandom(4)
                relatorio["assinatura_mbr"] = nova_assinatura[::-1].hex().upper()
                if not simular:
                    novo_mbr = bytearray(mbr)
                    novo_mbr[0x1B8:0x1BC] = nova_assinatura
                    disco.escrever(0, bytes(novo_mbr))
                relatorio["acoes"].append(
                    "Assinatura de disco MBR trocada para evitar conflito com a origem."
                )
            return relatorio

        relatorio["tipo"] = "GPT"
        relatorio["guid_original"] = cab.guid_texto

        bytes_entradas = cab.bytes_entradas
        setores_entradas = _setores_para(bytes_entradas, setor)
        lba_entradas_backup = ultima_lba - setores_entradas
        nova_ultima_usavel = lba_entradas_backup - 1

        entradas = disco.ler(cab.lba_entradas * setor, setores_entradas * setor)
        crc_entradas = _crc32(entradas[:bytes_entradas])
        if crc_entradas != cab.crc_entradas:
            raise ValueError(
                "CRC das entradas de particao nao confere: a copia pode estar "
                "incompleta ou corrompida. Corrija a copia antes de ajustar a GPT."
            )

        fim_particoes = _fim_particoes(entradas, cab)
        if fim_particoes > nova_ultima_usavel:
            raise ValueError(
                f"As particoes copiadas terminam na LBA {fim_particoes}, alem do "
                f"limite utilizavel do destino ({nova_ultima_usavel}). O disco "
                "destino e pequeno demais para este clone setor a setor."
            )

        guid_disco = uuid.uuid4().bytes_le if novo_guid else cab.guid_disco
        relatorio["guid_novo"] = str(uuid.UUID(bytes_le=guid_disco)).upper()
        relatorio["ultima_lba_usavel_antes"] = cab.ultima_lba_usavel
        relatorio["ultima_lba_usavel_depois"] = nova_ultima_usavel
        relatorio["setores_livres_no_fim"] = max(
            0, nova_ultima_usavel - fim_particoes
        )

        primario = _montar_cabecalho(
            cab,
            minha_lba=1,
            lba_alternativa=ultima_lba,
            lba_entradas=2,
            primeira_usavel=cab.primeira_lba_usavel,
            ultima_usavel=nova_ultima_usavel,
            guid_disco=guid_disco,
            crc_entradas=crc_entradas,
        )
        secundario = _montar_cabecalho(
            cab,
            minha_lba=ultima_lba,
            lba_alternativa=1,
            lba_entradas=lba_entradas_backup,
            primeira_usavel=cab.primeira_lba_usavel,
            ultima_usavel=nova_ultima_usavel,
            guid_disco=guid_disco,
            crc_entradas=crc_entradas,
        )

        novo_mbr = bytearray(mbr)
        setores_protetivos = min(total_setores - 1, 0xFFFFFFFF)
        if novo_mbr[0x1C2] == 0xEE:
            struct.pack_into("<I", novo_mbr, 0x1CA, setores_protetivos)
            relatorio["acoes"].append(
                "MBR protetiva ajustada para cobrir o disco inteiro."
            )

        if not simular:
            disco.escrever(0, bytes(novo_mbr))
            disco.escrever(setor, primario.ljust(setor, b"\x00"))
            disco.escrever(lba_entradas_backup * setor, entradas)
            disco.escrever(ultima_lba * setor, secundario.ljust(setor, b"\x00"))
            disco.atualizar_propriedades()

        relatorio["acoes"].extend(
            [
                f"Cabecalho GPT primario reescrito (ultima LBA usavel {nova_ultima_usavel}).",
                f"Cabecalho GPT secundario e copia das entradas gravados no fim do disco (LBA {ultima_lba}).",
            ]
        )
        if novo_guid:
            relatorio["acoes"].append(
                "GUID do disco trocado para evitar conflito com o HD de origem."
            )
        return relatorio


def _fim_particoes(entradas: bytes, cab: CabecalhoGPT) -> int:
    """Ultima LBA ocupada por alguma particao valida."""
    fim = 0
    for i in range(cab.num_entradas):
        base = i * cab.tamanho_entrada
        registro = entradas[base : base + cab.tamanho_entrada]
        if len(registro) < 56 or registro[:16] == b"\x00" * 16:
            continue
        primeira, ultima = struct.unpack("<QQ", registro[32:48])
        if ultima > fim:
            fim = ultima
    return fim


def descrever_particoes(caminho_disco: str) -> list[dict]:
    """Le as entradas GPT diretamente do disco (util para conferir o destino)."""
    with DispositivoBruto(caminho_disco) as disco:
        setor = disco.tamanho_setor()
        cab = analisar_cabecalho(disco.ler(setor, setor))
        if cab is None:
            return []
        entradas = disco.ler(
            cab.lba_entradas * setor,
            _setores_para(cab.bytes_entradas, setor) * setor,
        )
        resultado = []
        for i in range(cab.num_entradas):
            base = i * cab.tamanho_entrada
            registro = entradas[base : base + cab.tamanho_entrada]
            if len(registro) < 128 or registro[:16] == b"\x00" * 16:
                continue
            tipo = str(uuid.UUID(bytes_le=registro[:16])).upper()
            guid = str(uuid.UUID(bytes_le=registro[16:32])).upper()
            primeira, ultima = struct.unpack("<QQ", registro[32:48])
            nome = registro[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
            resultado.append(
                {
                    "indice": i + 1,
                    "tipo": tipo,
                    "guid": guid,
                    "primeira_lba": primeira,
                    "ultima_lba": ultima,
                    "bytes": (ultima - primeira + 1) * setor,
                    "nome": nome,
                }
            )
        return resultado
