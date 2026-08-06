"""Leitor FAT12/16/32 somente leitura, para inspecionar a particao EFI.

A particao de sistema EFI normalmente nao tem letra de unidade, entao nao da
para checar os arquivos de boot com os.path. Aqui lemos o sistema de arquivos
direto do dispositivo, sem montar nada e sem escrever nada.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .winio import DispositivoBruto

ATTR_LFN = 0x0F
ATTR_DIRETORIO = 0x10
ATTR_VOLUME_ID = 0x08


class ErroFAT(ValueError):
    pass


@dataclass
class InfoFAT:
    bytes_por_setor: int
    setores_por_cluster: int
    setores_reservados: int
    num_fats: int
    entradas_raiz: int
    setores_por_fat: int
    total_setores: int
    cluster_raiz: int
    tipo: str  # FAT12 / FAT16 / FAT32

    @property
    def bytes_por_cluster(self) -> int:
        return self.bytes_por_setor * self.setores_por_cluster

    @property
    def inicio_fat(self) -> int:
        return self.setores_reservados * self.bytes_por_setor

    @property
    def setores_raiz(self) -> int:
        if self.tipo == "FAT32":
            return 0
        return (self.entradas_raiz * 32 + self.bytes_por_setor - 1) // self.bytes_por_setor

    @property
    def primeiro_setor_dados(self) -> int:
        return self.setores_reservados + self.num_fats * self.setores_por_fat + self.setores_raiz


def analisar_bpb(setor: bytes) -> InfoFAT:
    if len(setor) < 512:
        raise ErroFAT("Setor de boot curto demais.")
    if setor[510:512] != b"\x55\xAA":
        raise ErroFAT("Assinatura 0x55AA ausente: nao parece um volume FAT.")
    bytes_por_setor = struct.unpack_from("<H", setor, 11)[0]
    setores_por_cluster = setor[13]
    if bytes_por_setor not in (512, 1024, 2048, 4096) or setores_por_cluster == 0:
        raise ErroFAT("BPB invalido: nao e um volume FAT.")
    setores_reservados = struct.unpack_from("<H", setor, 14)[0]
    num_fats = setor[16]
    entradas_raiz = struct.unpack_from("<H", setor, 17)[0]
    total_16 = struct.unpack_from("<H", setor, 19)[0]
    fat_16 = struct.unpack_from("<H", setor, 22)[0]
    total_32 = struct.unpack_from("<I", setor, 32)[0]
    fat_32 = struct.unpack_from("<I", setor, 36)[0]
    cluster_raiz = struct.unpack_from("<I", setor, 44)[0] if fat_16 == 0 else 0

    setores_por_fat = fat_16 or fat_32
    total_setores = total_16 or total_32
    if not setores_por_fat or not total_setores or not num_fats:
        raise ErroFAT("BPB invalido: campos de tamanho zerados.")

    setores_raiz = (entradas_raiz * 32 + bytes_por_setor - 1) // bytes_por_setor
    setores_dados = total_setores - (setores_reservados + num_fats * setores_por_fat + setores_raiz)
    clusters = setores_dados // setores_por_cluster
    tipo = "FAT12" if clusters < 4085 else ("FAT16" if clusters < 65525 else "FAT32")

    return InfoFAT(
        bytes_por_setor=bytes_por_setor,
        setores_por_cluster=setores_por_cluster,
        setores_reservados=setores_reservados,
        num_fats=num_fats,
        entradas_raiz=entradas_raiz,
        setores_por_fat=setores_por_fat,
        total_setores=total_setores,
        cluster_raiz=cluster_raiz,
        tipo=tipo,
    )


class VolumeFAT:
    """Acesso somente leitura a um volume FAT em um deslocamento do dispositivo."""

    def __init__(self, dispositivo: DispositivoBruto, offset: int, setor_dispositivo: int = 512):
        self.dispositivo = dispositivo
        self.offset = offset
        self.setor_dispositivo = setor_dispositivo
        self.info = analisar_bpb(self._ler(0, max(512, setor_dispositivo)))

    def _ler(self, deslocamento: int, tamanho: int) -> bytes:
        """Le respeitando o alinhamento exigido pelo dispositivo."""
        inicio = self.offset + deslocamento
        base = (inicio // self.setor_dispositivo) * self.setor_dispositivo
        sobra = inicio - base
        total = sobra + tamanho
        total = ((total + self.setor_dispositivo - 1) // self.setor_dispositivo) * self.setor_dispositivo
        bloco = self.dispositivo.ler(base, total)
        return bloco[sobra : sobra + tamanho]

    def _proximo_cluster(self, cluster: int) -> int:
        info = self.info
        if info.tipo == "FAT32":
            pos = info.inicio_fat + cluster * 4
            valor = struct.unpack_from("<I", self._ler(pos, 4))[0] & 0x0FFFFFFF
            return 0x0FFFFFF8 if valor >= 0x0FFFFFF8 else valor
        if info.tipo == "FAT16":
            pos = info.inicio_fat + cluster * 2
            valor = struct.unpack_from("<H", self._ler(pos, 2))[0]
            return 0x0FFFFFF8 if valor >= 0xFFF8 else valor
        pos = info.inicio_fat + (cluster * 3) // 2
        par = struct.unpack_from("<H", self._ler(pos, 2))[0]
        valor = (par & 0x0FFF) if cluster % 2 == 0 else (par >> 4)
        return 0x0FFFFFF8 if valor >= 0xFF8 else valor

    def _offset_cluster(self, cluster: int) -> int:
        info = self.info
        setor = info.primeiro_setor_dados + (cluster - 2) * info.setores_por_cluster
        return setor * info.bytes_por_setor

    def _bytes_do_diretorio(self, cluster: int, limite_clusters: int = 4096) -> bytes:
        """Concatena os clusters de um diretorio (ou a raiz fixa em FAT12/16)."""
        info = self.info
        if cluster == 0 and info.tipo != "FAT32":
            inicio = (info.setores_reservados + info.num_fats * info.setores_por_fat) * info.bytes_por_setor
            return self._ler(inicio, info.entradas_raiz * 32)
        if cluster == 0:
            cluster = info.cluster_raiz
        partes = bytearray()
        vistos = set()
        while 2 <= cluster < 0x0FFFFFF8 and len(vistos) < limite_clusters:
            if cluster in vistos:  # cadeia circular por corrupcao
                break
            vistos.add(cluster)
            partes.extend(self._ler(self._offset_cluster(cluster), info.bytes_por_cluster))
            cluster = self._proximo_cluster(cluster)
        return bytes(partes)

    def listar(self, cluster: int = 0) -> list[dict]:
        """Lista um diretorio, remontando os nomes longos (LFN)."""
        dados = self._bytes_do_diretorio(cluster)
        itens: list[dict] = []
        partes_lfn: list[tuple[int, str]] = []
        for i in range(0, len(dados) - 31, 32):
            entrada = dados[i : i + 32]
            if entrada[0] == 0x00:
                break
            if entrada[0] == 0xE5:  # apagada
                partes_lfn.clear()
                continue
            atributos = entrada[11]
            if atributos == ATTR_LFN:
                sequencia = entrada[0] & 0x1F
                bruto = entrada[1:11] + entrada[14:26] + entrada[28:32]
                texto = bruto.decode("utf-16-le", "ignore")
                fim = texto.find("￿")
                if fim >= 0:
                    texto = texto[:fim]
                partes_lfn.append((sequencia, texto.split("\x00")[0]))
                continue
            if atributos & ATTR_VOLUME_ID:
                partes_lfn.clear()
                continue

            nome_longo = "".join(t for _, t in sorted(partes_lfn, key=lambda x: x[0]))
            partes_lfn.clear()
            base = entrada[0:8].decode("cp437", "ignore").rstrip()
            ext = entrada[8:11].decode("cp437", "ignore").rstrip()
            nome_curto = f"{base}.{ext}" if ext else base
            itens.append(
                {
                    "nome": nome_longo or nome_curto,
                    "nome_curto": nome_curto,
                    "diretorio": bool(atributos & ATTR_DIRETORIO),
                    "cluster": (
                        struct.unpack_from("<H", entrada, 20)[0] << 16
                    )
                    | struct.unpack_from("<H", entrada, 26)[0],
                    "tamanho": struct.unpack_from("<I", entrada, 28)[0],
                }
            )
        return itens

    def procurar(self, caminho: str) -> dict | None:
        """Resolve um caminho como 'EFI/Microsoft/Boot/bootmgfw.efi'."""
        atual = 0
        item: dict | None = None
        for parte in [p for p in caminho.replace("\\", "/").split("/") if p]:
            alvo = parte.lower()
            item = next(
                (
                    e
                    for e in self.listar(atual)
                    if e["nome"].lower() == alvo or e["nome_curto"].lower() == alvo
                ),
                None,
            )
            if item is None:
                return None
            atual = item["cluster"]
        return item

    def existe(self, caminho: str) -> bool:
        return self.procurar(caminho) is not None
