"""Testes do Copia_hd usando imagens de disco sinteticas (sem hardware real).

Rode com: python -m tests.test_copiahd   (a partir de C:\\DEV\\Copia_hd)
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import uuid
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copiahd import gpt
from copiahd.disks import formatar_bytes
from copiahd.rawclone import _arredondar_para_cima, _ler_bloco
from copiahd.winio import DispositivoBruto, buffer_alinhado

SETOR = 512
TIPO_MS_BASIC = uuid.UUID("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7")
TIPO_EFI = uuid.UUID("C12A7328-F81F-11D2-BA4B-00A0C93EC93B")

falhas: list[str] = []
total = 0


def checar(condicao: bool, descricao: str) -> None:
    global total
    total += 1
    if condicao:
        print(f"  OK   {descricao}")
    else:
        print(f"  FALHA {descricao}")
        falhas.append(descricao)


def _crc(dados: bytes) -> int:
    return zlib.crc32(dados) & 0xFFFFFFFF


def montar_entrada(tipo: uuid.UUID, primeira: int, ultima: int, nome: str) -> bytes:
    registro = bytearray(128)
    registro[0:16] = tipo.bytes_le
    registro[16:32] = uuid.uuid4().bytes_le
    struct.pack_into("<QQ", registro, 32, primeira, ultima)
    rotulo = nome.encode("utf-16-le")[:72]
    registro[56 : 56 + len(rotulo)] = rotulo
    return bytes(registro)


def criar_imagem_gpt(caminho: Path, total_setores: int, particoes: list[tuple]) -> None:
    """Escreve uma imagem com GPT valida (MBR protetiva + cabecalhos + entradas)."""
    num_entradas, tamanho_entrada = 128, 128
    bytes_entradas = num_entradas * tamanho_entrada
    setores_entradas = bytes_entradas // SETOR
    ultima_lba = total_setores - 1
    lba_entradas_backup = ultima_lba - setores_entradas
    primeira_usavel = 2 + setores_entradas
    ultima_usavel = lba_entradas_backup - 1
    guid_disco = uuid.uuid4().bytes_le

    entradas = bytearray(bytes_entradas)
    for i, (tipo, primeira, ultima, nome) in enumerate(particoes):
        entradas[i * 128 : (i + 1) * 128] = montar_entrada(tipo, primeira, ultima, nome)
    crc_entradas = _crc(bytes(entradas))

    def cabecalho(minha, alternativa, lba_ent) -> bytes:
        corpo = bytearray(92)
        struct.pack_into(
            "<8sIIII QQQQ 16s QIII",
            corpo,
            0,
            b"EFI PART",
            0x00010000,
            92,
            0,
            0,
            minha,
            alternativa,
            primeira_usavel,
            ultima_usavel,
            guid_disco,
            lba_ent,
            num_entradas,
            tamanho_entrada,
            crc_entradas,
        )
        struct.pack_into("<I", corpo, 16, _crc(bytes(corpo)))
        return bytes(corpo).ljust(SETOR, b"\x00")

    with open(caminho, "wb") as f:
        f.truncate(total_setores * SETOR)
        mbr = bytearray(SETOR)
        mbr[0x1B8:0x1BC] = b"\x11\x22\x33\x44"
        mbr[0x1BE] = 0x00
        mbr[0x1C2] = 0xEE  # tipo GPT protetivo
        struct.pack_into("<I", mbr, 0x1C6, 1)
        struct.pack_into("<I", mbr, 0x1CA, min(total_setores - 1, 0xFFFFFFFF))
        mbr[0x1FE:0x200] = b"\x55\xAA"
        f.write(bytes(mbr))
        f.write(cabecalho(1, ultima_lba, 2))
        f.write(bytes(entradas))
        # dados reconheciveis dentro da primeira particao de dados
        f.seek(particoes[-1][1] * SETOR)
        f.write(b"CONTEUDO-DE-TESTE" * 100)
        f.seek(lba_entradas_backup * SETOR)
        f.write(bytes(entradas))
        f.seek(ultima_lba * SETOR)
        f.write(cabecalho(ultima_lba, 1, lba_entradas_backup))


def clonar_arquivo(origem: Path, destino: Path, bloco: int = 1 << 20) -> None:
    """Reproduz o loop de copia setor a setor entre duas imagens.

    Copia no maximo o tamanho do destino: escrever alem do fim de um arquivo o
    faria crescer, o que um disco fisico nunca permite.
    """
    tamanho = min(origem.stat().st_size, destino.stat().st_size)
    with DispositivoBruto(str(origem)) as src, DispositivoBruto(str(destino), escrita=True) as dst:
        buffer = buffer_alinhado(bloco)
        offset = 0
        ruins: list[int] = []
        while offset < tamanho:
            n = _arredondar_para_cima(min(bloco, tamanho - offset), SETOR)
            _ler_bloco(src, offset, n, SETOR, buffer, ruins, lambda *_: None)
            dst.posicionar(offset)
            dst.escrever_de(buffer, n)
            offset += n
        assert not ruins, ruins


# --------------------------------------------------------------------------
def teste_leitura_escrita_bruta(tmp: Path) -> None:
    print("\n1. I/O bruto alinhado sobre imagem")
    img = tmp / "io.img"
    with open(img, "wb") as f:
        f.truncate(4 * 1024 * 1024)
    marca = (b"BLOCO-A" * 70)[:SETOR].ljust(SETOR, b"\x00")
    with DispositivoBruto(str(img), escrita=True) as d:
        checar(d.tamanho_setor() == 512, "tamanho de setor de imagem = 512 B")
        checar(d.tamanho_bytes() == 4 * 1024 * 1024, "tamanho da imagem lido corretamente")
        d.escrever(2 * SETOR, marca)
    with DispositivoBruto(str(img)) as d:
        checar(d.ler(2 * SETOR, SETOR) == marca, "dado gravado le identico")
        checar(d.ler(3 * SETOR, SETOR) == bytes(SETOR), "setor vizinho intacto")


def teste_clone_e_correcao_gpt(tmp: Path) -> None:
    print("\n2. Clone para disco MAIOR + correcao de GPT")
    origem = tmp / "origem.img"
    destino = tmp / "destino.img"
    setores_origem = 40 * 1024  # 20 MB
    setores_destino = 100 * 1024  # 50 MB
    fim_dados = setores_origem - 40
    criar_imagem_gpt(
        origem,
        setores_origem,
        [
            (TIPO_EFI, 34, 2081, "SISTEMA"),
            (TIPO_MS_BASIC, 2082, fim_dados, "Windows"),
        ],
    )
    with open(destino, "wb") as f:
        f.truncate(setores_destino * SETOR)

    clonar_arquivo(origem, destino)
    checar(
        origem.read_bytes() == destino.read_bytes()[: setores_origem * SETOR],
        "clone byte a byte identico a origem",
    )

    with DispositivoBruto(str(destino)) as d:
        antes = gpt.analisar_cabecalho(d.ler(SETOR, SETOR))
        backup_no_lugar_errado = gpt.analisar_cabecalho(
            d.ler((setores_destino - 1) * SETOR, SETOR)
        )
    checar(antes is not None, "GPT primaria copiada tem CRC valido")
    checar(
        backup_no_lugar_errado is None,
        "antes do ajuste, o fim do disco novo NAO tem GPT de backup (problema real)",
    )
    checar(
        antes.lba_alternativa == setores_origem - 1,
        "GPT copiada aponta o backup para o fim do disco ANTIGO",
    )

    guid_antes = antes.guid_texto
    relatorio = gpt.corrigir_gpt(str(destino), novo_guid=True)
    checar(relatorio["tipo"] == "GPT", "corrigir_gpt identificou GPT")

    with DispositivoBruto(str(destino)) as d:
        primario = gpt.analisar_cabecalho(d.ler(SETOR, SETOR))
        secundario = gpt.analisar_cabecalho(d.ler((setores_destino - 1) * SETOR, SETOR))
    checar(primario is not None, "cabecalho primario reescrito com CRC valido")
    checar(secundario is not None, "cabecalho secundario gravado no fim com CRC valido")
    checar(
        primario.lba_alternativa == setores_destino - 1,
        "primario passa a apontar para o fim do disco NOVO",
    )
    checar(secundario.minha_lba == setores_destino - 1, "secundario esta na ultima LBA")
    checar(secundario.lba_alternativa == 1, "secundario aponta de volta para a LBA 1")
    checar(
        primario.ultima_lba_usavel == secundario.ultima_lba_usavel > antes.ultima_lba_usavel,
        "ultima LBA usavel cresceu e e igual nos dois cabecalhos",
    )
    checar(primario.crc_entradas == antes.crc_entradas, "entradas de particao preservadas")
    checar(primario.guid_texto == secundario.guid_texto, "os dois cabecalhos tem o mesmo GUID")
    checar(primario.guid_texto != guid_antes, "GUID do disco trocado (sem conflito com a origem)")

    with DispositivoBruto(str(destino)) as d:
        setores_ent = (primario.num_entradas * primario.tamanho_entrada) // SETOR
        copia = d.ler(secundario.lba_entradas * SETOR, setores_ent * SETOR)
        principal = d.ler(2 * SETOR, setores_ent * SETOR)
        mbr = d.ler(0, SETOR)
    checar(copia == principal, "copia das entradas no fim do disco identica a principal")
    checar(
        struct.unpack_from("<I", mbr, 0x1CA)[0] == setores_destino - 1,
        "MBR protetiva cobre o disco novo inteiro",
    )

    particoes = gpt.descrever_particoes(str(destino))
    checar(len(particoes) == 2, "duas particoes visiveis no disco novo")
    checar(
        [p["nome"] for p in particoes] == ["SISTEMA", "Windows"],
        "nomes e ordem das particoes preservados",
    )
    livre = (primario.ultima_lba_usavel - fim_dados) * SETOR
    checar(livre > 25 * 1024 * 1024, f"sobrou espaco livre expansivel ({formatar_bytes(livre)})")


def teste_destino_menor(tmp: Path) -> None:
    print("\n3. Recusa quando as particoes nao cabem no destino")
    origem = tmp / "grande.img"
    destino = tmp / "pequeno.img"
    setores = 40 * 1024
    criar_imagem_gpt(origem, setores, [(TIPO_MS_BASIC, 34, setores - 40, "Windows")])
    with open(destino, "wb") as f:
        f.truncate(20 * 1024 * SETOR)
    clonar_arquivo(origem, destino, bloco=1 << 20)
    try:
        gpt.corrigir_gpt(str(destino), novo_guid=True)
        checar(False, "corrigir_gpt deveria recusar destino pequeno demais")
    except ValueError as exc:
        checar("pequeno demais" in str(exc), f"recusa explicada ao usuario: {exc}")


def teste_gpt_ausente_mbr(tmp: Path) -> None:
    print("\n4. Disco MBR: troca de assinatura, sem tocar na GPT")
    img = tmp / "mbr.img"
    with open(img, "wb") as f:
        f.truncate(8 * 1024 * 1024)
        mbr = bytearray(SETOR)
        mbr[0x1B8:0x1BC] = b"\xAA\xBB\xCC\xDD"
        mbr[0x1FE:0x200] = b"\x55\xAA"
        f.write(bytes(mbr))
    relatorio = gpt.corrigir_gpt(str(img), novo_guid=True)
    checar(relatorio["tipo"] == "MBR", "disco sem GPT identificado como MBR")
    with DispositivoBruto(str(img)) as d:
        assinatura = d.ler(0, SETOR)[0x1B8:0x1BC]
    checar(assinatura != b"\xAA\xBB\xCC\xDD", "assinatura MBR trocada")
    checar(assinatura != b"\x00\x00\x00\x00", "assinatura MBR nao ficou nula")


def teste_deteccao_corrupcao(tmp: Path) -> None:
    print("\n5. Copia incompleta e detectada pelo CRC das entradas")
    origem = tmp / "corromper.img"
    setores = 40 * 1024
    criar_imagem_gpt(origem, setores, [(TIPO_MS_BASIC, 34, setores - 40, "Windows")])
    with DispositivoBruto(str(origem), escrita=True) as d:
        bloco = bytearray(d.ler(3 * SETOR, SETOR))
        bloco[0:4] = b"\xFF\xFF\xFF\xFF"  # estraga uma entrada de particao
        d.escrever(3 * SETOR, bytes(bloco))
    try:
        gpt.corrigir_gpt(str(origem), novo_guid=False)
        checar(False, "deveria detectar CRC de entradas invalido")
    except ValueError as exc:
        checar("CRC" in str(exc), f"corrupcao detectada antes de gravar: {exc}")


def teste_simulacao_nao_escreve(tmp: Path) -> None:
    print("\n6. Modo simulacao nao altera o disco")
    img = tmp / "simular.img"
    setores = 40 * 1024
    criar_imagem_gpt(img, setores, [(TIPO_MS_BASIC, 34, setores - 40, "Windows")])
    antes = img.read_bytes()
    relatorio = gpt.corrigir_gpt(str(img), novo_guid=True, simular=True)
    checar(relatorio["simulado"] is True, "relatorio marcado como simulacao")
    checar(len(relatorio["acoes"]) > 0, "simulacao descreve as acoes que faria")
    checar(img.read_bytes() == antes, "nenhum byte alterado em modo simulacao")


def teste_setor_ilegivel(tmp: Path) -> None:
    print("\n7. Setor ilegivel: bloco copiado com zeros no lugar")
    img = tmp / "ruim.img"
    with open(img, "wb") as f:
        f.truncate(1 << 20)
        f.seek(0)
        f.write(b"X" * (1 << 20))

    class OrigemComDefeito(DispositivoBruto):
        """Simula um setor fisicamente ilegivel (erro 23, CRC de dados).

        Qualquer leitura que abranja a LBA defeituosa falha, como acontece de
        verdade: o bloco grande falha e so a leitura setor a setor consegue
        recuperar os setores vizinhos.
        """

        lba_ruim = 2

        def ler_em(self, buffer, tamanho):
            inicio, fim = self._pos, self._pos + tamanho
            if inicio <= self.lba_ruim * SETOR < fim:
                from copiahd.winio import WinIOError

                raise WinIOError("ReadFile simulado", 23)
            return super().ler_em(buffer, tamanho)

        def posicionar(self, offset):
            self._pos = offset
            return super().posicionar(offset)

    ruins: list[int] = []
    buffer = buffer_alinhado(4 * SETOR)
    with OrigemComDefeito(str(img)) as d:
        d._pos = 0
        zerados = _ler_bloco(d, 0, 4 * SETOR, SETOR, buffer, ruins, lambda *_: None)
    dados = bytes(bytearray(buffer))
    checar(zerados == SETOR, "exatamente um setor contabilizado como zerado")
    checar(ruins == [2], "LBA defeituosa registrada para o relatorio")
    checar(dados[0 : 2 * SETOR] == b"X" * (2 * SETOR), "setores anteriores copiados corretos")
    checar(dados[2 * SETOR : 3 * SETOR] == bytes(SETOR), "setor defeituoso virou zeros")
    checar(dados[3 * SETOR : 4 * SETOR] == b"X" * SETOR, "setor seguinte copiado correto")


def _nome_curto_83(nome: str) -> bytes:
    base, _, ext = nome.partition(".")
    return (base.upper()[:8].ljust(8) + ext.upper()[:3].ljust(3)).encode("cp437")


def _checksum_83(curto: bytes) -> int:
    soma = 0
    for b in curto:
        soma = (((soma & 1) << 7) + (soma >> 1) + b) & 0xFF
    return soma


def _entradas_lfn(nome: str, curto: bytes) -> bytes:
    """Gera as entradas de nome longo que precedem a entrada 8.3."""
    codificado = nome.encode("utf-16-le") + b"\x00\x00"
    pedacos = [codificado[i : i + 26] for i in range(0, len(codificado), 26)]
    chk = _checksum_83(curto)
    saida = bytearray()
    for indice in range(len(pedacos), 0, -1):  # ordem reversa no disco
        pedaco = pedacos[indice - 1].ljust(26, b"\xFF")
        entrada = bytearray(32)
        entrada[0] = indice | (0x40 if indice == len(pedacos) else 0)
        entrada[1:11] = pedaco[0:10]
        entrada[11] = 0x0F
        entrada[13] = chk
        entrada[14:26] = pedaco[10:22]
        entrada[28:32] = pedaco[22:26]
        saida.extend(bytes(entrada))
    return bytes(saida)


def _entrada_dir(nome: str, cluster: int, tamanho: int, diretorio: bool) -> bytes:
    curto = _nome_curto_83(nome)
    entrada = bytearray(32)
    entrada[0:11] = curto
    entrada[11] = 0x10 if diretorio else 0x20
    struct.pack_into("<H", entrada, 20, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", entrada, 26, cluster & 0xFFFF)
    struct.pack_into("<I", entrada, 28, tamanho)
    prefixo = _entradas_lfn(nome, curto) if len(nome.partition(".")[0]) > 8 else b""
    return prefixo + bytes(entrada)


def criar_imagem_fat32(caminho: Path, arquivos: list[str]) -> None:
    """Cria um volume FAT32 minimo contendo a arvore de diretorios pedida."""
    setor, spc = 512, 1
    reservados, num_fats, setores_fat = 32, 2, 512
    total_setores = 70 * 1024  # 35 MB: acima do minimo de clusters do FAT32
    inicio_dados = reservados + num_fats * setores_fat

    # constroi a arvore: {caminho_dir: {nome: (eh_dir, tamanho)}}
    arvore: dict[str, dict[str, tuple[bool, int]]] = {"": {}}
    for arquivo in arquivos:
        partes = arquivo.split("/")
        atual = ""
        for parte in partes[:-1]:
            arvore.setdefault(atual, {})[parte] = (True, 0)
            atual = f"{atual}/{parte}" if atual else parte
            arvore.setdefault(atual, {})
        arvore.setdefault(atual, {})[partes[-1]] = (False, 4096)

    clusters: dict[str, int] = {"": 2}
    proximo = 3
    for caminho_dir in sorted(arvore):
        if caminho_dir and caminho_dir not in clusters:
            clusters[caminho_dir] = proximo
            proximo += 1
    conteudo_cluster = proximo  # clusters de dados dos arquivos comecam aqui

    fat = bytearray(setores_fat * setor)
    struct.pack_into("<III", fat, 0, 0x0FFFFFF8, 0x0FFFFFFF, 0x0FFFFFF8)
    dados: dict[int, bytes] = {}

    for caminho_dir, filhos in arvore.items():
        bloco = bytearray()
        if caminho_dir:
            bloco.extend(_entrada_dir(".", clusters[caminho_dir], 0, True))
            pai = caminho_dir.rpartition("/")[0]
            bloco.extend(_entrada_dir("..", clusters.get(pai, 0), 0, True))
        for nome, (eh_dir, tamanho) in filhos.items():
            filho = f"{caminho_dir}/{nome}" if caminho_dir else nome
            if eh_dir:
                bloco.extend(_entrada_dir(nome, clusters[filho], 0, True))
            else:
                bloco.extend(_entrada_dir(nome, conteudo_cluster, tamanho, False))
                struct.pack_into("<I", fat, conteudo_cluster * 4, 0x0FFFFFFF)
                dados[conteudo_cluster] = b"MZ" + b"\x00" * (setor - 2)
                conteudo_cluster += 1
        cluster = clusters[caminho_dir]
        struct.pack_into("<I", fat, cluster * 4, 0x0FFFFFFF)
        dados[cluster] = bytes(bloco).ljust(setor * spc, b"\x00")

    with open(caminho, "wb") as f:
        f.truncate(total_setores * setor)
        bpb = bytearray(setor)
        bpb[0:3] = b"\xEB\x58\x90"
        bpb[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", bpb, 11, setor)
        bpb[13] = spc
        struct.pack_into("<H", bpb, 14, reservados)
        bpb[16] = num_fats
        struct.pack_into("<H", bpb, 17, 0)  # FAT32: raiz e cadeia de clusters
        struct.pack_into("<H", bpb, 19, 0)
        bpb[21] = 0xF8
        struct.pack_into("<H", bpb, 22, 0)  # FATSz16 = 0 marca FAT32
        struct.pack_into("<I", bpb, 32, total_setores)
        struct.pack_into("<I", bpb, 36, setores_fat)
        struct.pack_into("<I", bpb, 44, 2)  # cluster raiz
        bpb[510:512] = b"\x55\xAA"
        f.write(bytes(bpb))
        for copia in range(num_fats):
            f.seek((reservados + copia * setores_fat) * setor)
            f.write(bytes(fat))
        for cluster, conteudo in dados.items():
            f.seek((inicio_dados + (cluster - 2) * spc) * setor)
            f.write(conteudo)


def teste_leitor_fat_particao_efi(tmp: Path) -> None:
    print("\n9. Leitor FAT: confere a particao EFI sem letra de unidade")
    from copiahd.fat import ErroFAT, VolumeFAT

    img = tmp / "esp.img"
    criar_imagem_fat32(
        img,
        [
            "EFI/Microsoft/Boot/bootmgfw.efi",
            "EFI/Microsoft/Boot/BCD",
            "EFI/Boot/bootx64.efi",
        ],
    )
    with DispositivoBruto(str(img)) as d:
        volume = VolumeFAT(d, 0, 512)
        checar(volume.info.tipo == "FAT32", f"volume reconhecido como FAT32 (veio {volume.info.tipo})")
        raiz = [e["nome"] for e in volume.listar()]
        checar(raiz == ["EFI"], f"raiz contem apenas EFI (veio {raiz})")
        checar(
            volume.existe("EFI/Microsoft/Boot/bootmgfw.efi"),
            "bootmgfw.efi encontrado via nome longo (Microsoft > 8 caracteres)",
        )
        checar(volume.existe("EFI/Boot/bootx64.efi"), "bootx64.efi encontrado")
        checar(volume.existe("efi/microsoft/boot/BOOTMGFW.EFI"), "busca insensivel a maiusculas")
        checar(not volume.existe("EFI/Microsoft/Boot/inexistente.efi"), "arquivo ausente retorna falso")
        checar(not volume.existe("Windows/System32"), "caminho invalido retorna falso")

    # mesmo volume gravado dentro de um "disco", em um deslocamento de particao,
    # e lido com setor de dispositivo de 4096 B (exercita o realinhamento)
    disco_img = tmp / "disco_com_esp.img"
    offset_esp = 1 << 20
    with open(disco_img, "wb") as f:
        f.truncate(offset_esp + img.stat().st_size)
        f.seek(offset_esp)
        f.write(img.read_bytes())
    with DispositivoBruto(str(disco_img)) as d:
        volume = VolumeFAT(d, offset_esp, 4096)
        checar(
            volume.existe("EFI/Microsoft/Boot/bootmgfw.efi"),
            "particao EFI lida em deslocamento, com setor de dispositivo 4096 B",
        )
        entrada = volume.procurar("EFI/Microsoft/Boot/BCD")
        checar(entrada is not None and entrada["tamanho"] == 4096, "metadados do arquivo lidos")

    vazio = tmp / "naofat.img"
    with open(vazio, "wb") as f:
        f.truncate(1 << 20)
    try:
        with DispositivoBruto(str(vazio)) as d:
            VolumeFAT(d, 0, 512)
        checar(False, "deveria recusar volume que nao e FAT")
    except ErroFAT as exc:
        checar(True, f"volume nao-FAT recusado com mensagem clara: {exc}")


def teste_guid_preservado_por_padrao(tmp: Path) -> None:
    print("\n10. Por padrao os identificadores do disco sao preservados")
    img = tmp / "guid.img"
    setores = 40 * 1024
    criar_imagem_gpt(img, setores, [(TIPO_MS_BASIC, 34, setores - 400, "Windows")])
    with DispositivoBruto(str(img)) as d:
        antes = gpt.analisar_cabecalho(d.ler(SETOR, SETOR)).guid_texto
    gpt.corrigir_gpt(str(img))  # sem argumentos: comportamento padrao
    with DispositivoBruto(str(img)) as d:
        primario = gpt.analisar_cabecalho(d.ler(SETOR, SETOR))
        secundario = gpt.analisar_cabecalho(d.ler((setores - 1) * SETOR, SETOR))
    checar(primario.guid_texto == antes, "GUID do disco preservado por padrao (BCD continua valida)")
    checar(secundario is not None, "GPT de backup ainda foi corrigida")
    checar(secundario.guid_texto == antes, "backup tambem mantem o GUID original")


def teste_validacoes_do_plano(_tmp: Path) -> None:
    print("\n11. Guardas de seguranca antes de escrever no destino")
    from copiahd import rawclone
    from copiahd.disks import Disco, Particao

    def disco(numero, tamanho, sistema=False, boot=False, ro=False, fim_particao=None):
        fim = fim_particao if fim_particao is not None else tamanho - (1 << 20)
        return Disco(
            numero=numero,
            modelo=f"DISCO{numero}",
            serial=f"SN{numero}",
            tamanho=tamanho,
            barramento="SATA",
            estilo_particao="GPT",
            status="Online",
            somente_leitura=ro,
            tem_boot=boot,
            tem_sistema=sistema,
            particoes=[
                Particao(1, 1 << 20, fim - (1 << 20), "Basic", "X", "Dados", "NTFS", 0, fim)
            ],
        )

    geometria: dict[int, tuple[int, int]] = {}

    class DispositivoFalso:
        """Substitui o acesso bruto: devolve a geometria declarada no teste."""

        def __init__(self, caminho, escrita=False, sem_buffer=True):
            self.numero = int(caminho.rsplit("PhysicalDrive", 1)[1])

        def tamanho_setor(self):
            return geometria[self.numero][0]

        def tamanho_bytes(self):
            return geometria[self.numero][1]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    original = rawclone.DispositivoBruto
    rawclone.DispositivoBruto = DispositivoFalso
    try:
        GB = 1 << 30

        def erro_de(origem, destino, **kw):
            try:
                rawclone.montar_plano(origem, destino, **kw)
                return None
            except rawclone.ErroClone as exc:
                return str(exc)

        geometria = {0: (512, 100 * GB), 1: (512, 100 * GB)}
        d0, d1 = disco(0, 100 * GB), disco(1, 100 * GB)
        checar(erro_de(d0, d0) is not None, "recusa origem igual ao destino")

        erro = erro_de(d0, disco(1, 100 * GB, sistema=True))
        checar(erro is not None and "sistema" in erro, f"recusa destino que e o disco de sistema")

        erro = erro_de(d0, disco(1, 100 * GB, ro=True))
        checar(erro is not None and "somente leitura" in erro, "recusa destino somente leitura")

        geometria = {0: (512, 100 * GB), 1: (4096, 100 * GB)}
        erro = erro_de(d0, d1)
        checar(erro is not None and "setor" in erro, "recusa setor logico diferente (512e vs 4Kn)")

        geometria = {0: (512, 100 * GB), 1: (512, 50 * GB)}
        erro = erro_de(d0, disco(1, 50 * GB))
        checar(erro is not None and "menor" in erro, "recusa destino menor sem --recortar")

        # destino menor, mas as particoes terminam antes do limite: --recortar resolve
        origem_enxuta = disco(0, 100 * GB, fim_particao=40 * GB)
        plano = rawclone.montar_plano(origem_enxuta, disco(1, 50 * GB), copiar_disco_inteiro=False)
        checar(plano.modo == "ate-ultima-particao", "com --recortar copia so ate a ultima particao")
        checar(plano.bytes_a_copiar == 40 * GB, "volume copiado limitado ao fim da ultima particao")
        checar(any("menor" in a for a in plano.avisos), "avisa que o destino e menor")

        geometria = {0: (512, 100 * GB), 1: (512, 200 * GB)}
        plano = rawclone.montar_plano(d0, disco(1, 200 * GB))
        checar(plano.modo == "completo" and plano.bytes_a_copiar == 100 * GB, "destino maior: copia tudo")
        checar(any("a mais" in a for a in plano.avisos), "avisa sobre o espaco extra do destino")
        checar(plano.bloco % plano.setor == 0, "bloco de copia multiplo do setor")

        plano = rawclone.montar_plano(disco(0, 100 * GB, sistema=True, boot=True), disco(1, 200 * GB))
        checar(
            any("quente" in a for a in plano.avisos),
            "avisa sobre copia a quente quando a origem e o sistema em execucao",
        )
    finally:
        rawclone.DispositivoBruto = original


def teste_arredondamento(_tmp: Path) -> None:
    print("\n8. Arredondamento de tamanhos para multiplo de setor")
    checar(_arredondar_para_cima(1, 512) == 512, "1 B -> 512 B")
    checar(_arredondar_para_cima(512, 512) == 512, "512 B permanece 512 B")
    checar(_arredondar_para_cima(513, 512) == 1024, "513 B -> 1024 B")
    checar(_arredondar_para_cima(4096, 4096) == 4096, "alinhamento 4Kn preservado")


def main() -> int:
    print("=" * 70)
    print("Testes do Copia_hd (imagens sinteticas, nenhum disco real tocado)")
    print("=" * 70)
    with tempfile.TemporaryDirectory(prefix="copiahd_teste_") as pasta:
        tmp = Path(pasta)
        for teste in (
            teste_leitura_escrita_bruta,
            teste_clone_e_correcao_gpt,
            teste_destino_menor,
            teste_gpt_ausente_mbr,
            teste_deteccao_corrupcao,
            teste_simulacao_nao_escreve,
            teste_setor_ilegivel,
            teste_leitor_fat_particao_efi,
            teste_guid_preservado_por_padrao,
            teste_validacoes_do_plano,
            teste_arredondamento,
        ):
            teste(tmp)
    print("\n" + "=" * 70)
    if falhas:
        print(f"{len(falhas)} de {total} verificacoes FALHARAM:")
        for f in falhas:
            print(f"  - {f}")
        return 1
    print(f"Todas as {total} verificacoes passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
