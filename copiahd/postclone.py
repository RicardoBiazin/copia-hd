"""Ajustes finais no HD novo: expandir particao, checar integridade e boot."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .disks import _powershell, formatar_bytes, obter_disco
from .gpt import corrigir_gpt
from .winio import caminho_disco


@dataclass
class ResultadoEtapa:
    nome: str
    ok: bool
    detalhe: str

    def linha(self) -> str:
        return f"  [{'OK ' if self.ok else 'ERRO'}] {self.nome}: {self.detalhe}"


def ajustar_gpt_destino(
    numero_disco: int, novo_guid: bool = False, simular: bool = False
) -> ResultadoEtapa:
    try:
        relatorio = corrigir_gpt(caminho_disco(numero_disco), novo_guid, simular)
    except Exception as exc:  # noqa: BLE001 - erro reportado ao usuario
        return ResultadoEtapa("Ajuste da tabela de particao", False, str(exc))
    return ResultadoEtapa(
        "Ajuste da tabela de particao",
        True,
        f"{relatorio['tipo']}: " + " ".join(relatorio["acoes"]),
    )


def expandir_particao_dados(numero_disco: int, simular: bool = False) -> ResultadoEtapa:
    """Estende a particao de dados (o Windows) para o espaco livre restante.

    Nao usa simplesmente "a ultima particao": em um disco de Windows a ultima
    normalmente e a de recuperacao, e expandi-la nao ajuda em nada. Escolhemos a
    maior particao de dados. Se a de recuperacao estiver depois dela, o espaco
    novo nao e contiguo e o Windows nao consegue estender - nesse caso a funcao
    explica o que fazer em vez de mexer na particao errada.
    """
    disco = obter_disco(numero_disco)
    candidatas = [
        p
        for p in disco.particoes
        if p.sistema_arquivos
        and p.sistema_arquivos.upper() in {"NTFS", "REFS"}
        and (p.tipo or "").lower() != "recovery"
    ]
    if not candidatas:
        return ResultadoEtapa(
            "Expansao da particao de dados", False, "Nenhuma particao NTFS de dados encontrada."
        )
    alvo = max(candidatas, key=lambda p: p.tamanho)
    depois = [p for p in disco.particoes if p.offset > alvo.offset]
    bloqueadoras = [p for p in depois if (p.tipo or "").lower() == "recovery"]

    script = (
        f"$tam = Get-PartitionSupportedSize -DiskNumber {numero_disco} "
        f"-PartitionNumber {alvo.numero} -ErrorAction Stop; "
        f"$atual = (Get-Partition -DiskNumber {numero_disco} "
        f"-PartitionNumber {alvo.numero}).Size; "
        "if ($tam.SizeMax -gt $atual + 1MB) { "
        + (
            "Write-Output \"SIMULACAO: expandiria de $atual para $($tam.SizeMax)\" }"
            if simular
            else f"Resize-Partition -DiskNumber {numero_disco} -PartitionNumber "
            f"{alvo.numero} -Size $tam.SizeMax -ErrorAction Stop; "
            "Write-Output \"Expandida de $atual para $($tam.SizeMax)\" }"
        )
        + " else { Write-Output 'Ja ocupa todo o espaco disponivel.' }"
    )
    nome = f"Expansao da particao #{alvo.numero} ({alvo.letra or alvo.rotulo or 'dados'})"
    try:
        saida = _powershell(script, timeout=300).strip()
    except Exception as exc:  # noqa: BLE001
        return ResultadoEtapa(nome, False, str(exc))

    nao_cresceu = "Ja ocupa" in saida
    if nao_cresceu and bloqueadoras and disco.tamanho - disco.fim_ultima_particao > 1 << 30:
        livres = formatar_bytes(disco.tamanho - disco.fim_ultima_particao)
        return ResultadoEtapa(
            nome,
            True,
            f"{saida} Ha {livres} livres no fim do disco, mas a particao de "
            f"recuperacao (#{bloqueadoras[0].numero}) esta entre eles e o Windows, "
            "entao a expansao automatica nao e possivel. O clone funciona e da boot "
            "assim mesmo. Para aproveitar o espaco depois, no Gerenciamento de Disco "
            "do sistema ja migrado: apague a particao de recuperacao, estenda o "
            "volume do Windows e recrie a recuperacao com 'reagentc /enable'.",
        )
    return ResultadoEtapa(nome, True, saida or "Sem alteracao.")


# nome anterior mantido para compatibilidade com chamadas existentes
expandir_ultima_particao = expandir_particao_dados


def verificar_sistemas_arquivos(numero_disco: int, simular: bool = False) -> list[ResultadoEtapa]:
    """Roda chkdsk somente-leitura em cada volume NTFS copiado."""
    disco = obter_disco(numero_disco)
    etapas: list[ResultadoEtapa] = []
    for p in disco.particoes:
        if not p.letra or not p.sistema_arquivos:
            continue
        if simular:
            etapas.append(
                ResultadoEtapa(f"chkdsk {p.letra}:", True, "[SIMULACAO] chkdsk somente leitura")
            )
            continue
        proc = subprocess.run(
            ["chkdsk.exe", f"{p.letra}:"], capture_output=True, text=True, timeout=3600
        )
        ok = proc.returncode == 0
        ultima = [l for l in proc.stdout.splitlines() if l.strip()]
        etapas.append(
            ResultadoEtapa(
                f"chkdsk {p.letra}:",
                ok,
                (ultima[-1].strip() if ultima else f"codigo {proc.returncode}"),
            )
        )
    return etapas


TIPO_GPT_EFI = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
ARQUIVOS_UEFI = ["EFI/Microsoft/Boot/bootmgfw.efi", "EFI/Boot/bootx64.efi"]


def conferir_arquivos_de_boot(numero_disco: int) -> ResultadoEtapa:
    """Confirma que o HD novo tem os arquivos que o firmware procura no boot.

    A particao de sistema EFI raramente tem letra de unidade, por isso ela e
    lida direto do dispositivo com o leitor FAT (somente leitura). A particao do
    Windows e checada pela letra quando existe.
    """
    import os

    from .fat import ErroFAT, VolumeFAT
    from .gpt import descrever_particoes
    from .winio import DispositivoBruto

    achados: list[str] = []
    faltando: list[str] = []

    try:
        esps = [
            p
            for p in descrever_particoes(caminho_disco(numero_disco))
            if p["tipo"] == TIPO_GPT_EFI
        ]
    except Exception as exc:  # noqa: BLE001 - sem elevacao ou disco offline
        esps = []
        faltando.append(f"tabela de particao ilegivel ({exc})")

    if esps:
        try:
            with DispositivoBruto(caminho_disco(numero_disco)) as dev:
                setor = dev.tamanho_setor()
                for esp in esps:
                    try:
                        volume = VolumeFAT(dev, esp["primeira_lba"] * setor, setor)
                    except ErroFAT as exc:
                        faltando.append(f"particao EFI #{esp['indice']} ilegivel ({exc})")
                        continue
                    for rel in ARQUIVOS_UEFI:
                        if volume.existe(rel):
                            achados.append(f"EFI#{esp['indice']}:/{rel}")
        except Exception as exc:  # noqa: BLE001
            faltando.append(f"nao foi possivel ler a particao EFI ({exc})")

    disco = obter_disco(numero_disco)
    windows_encontrado = False
    for p in disco.particoes:
        if not p.letra:
            continue
        raiz = f"{p.letra}:\\"
        if os.path.exists(os.path.join(raiz, "Windows", "System32", "winload.efi")):
            windows_encontrado = True
            achados.append(f"{p.letra}:\\Windows\\System32\\winload.efi")
        for rel in ("bootmgr", "Boot\\BCD"):  # boot legado (BIOS/MBR)
            if os.path.exists(os.path.join(raiz, rel)):
                achados.append(f"{p.letra}:\\{rel}")

    tem_gerenciador = any("bootmgfw.efi" in a or "bootmgr" in a for a in achados)
    if not tem_gerenciador:
        faltando.append("gerenciador de boot (bootmgfw.efi na particao EFI, ou bootmgr)")

    notas: list[str] = []
    if not windows_encontrado:
        # Quando os identificadores do disco sao preservados (o padrao), o Windows
        # costuma nao montar os volumes do clone porque sao duplicados. Isso e
        # esperado e nao indica falha: nao da para checar winload.efi por letra.
        particoes_dados = [
            p for p in disco.particoes if p.tamanho > 8 << 30 and (p.tipo or "").lower() != "recovery"
        ]
        if particoes_dados and not any(p.letra for p in particoes_dados):
            notas.append(
                "a particao do Windows no destino nao tem letra (normal em um clone "
                "com identificadores preservados), por isso winload.efi nao foi "
                "conferido por caminho"
            )
        else:
            faltando.append("pasta Windows com winload.efi")

    if faltando:
        return ResultadoEtapa(
            "Arquivos de boot no destino",
            False,
            "Nao encontrado: "
            + "; ".join(faltando)
            + ". Reconstrua com: bcdboot X:\\Windows /s Y: /f UEFI",
        )
    detalhe = f"{len(achados)} arquivo(s) presentes: " + ", ".join(achados)
    if notas:
        detalhe += ". Observacao: " + "; ".join(notas) + "."
    return ResultadoEtapa("Arquivos de boot no destino", True, detalhe)


def relatorio_final(numero_disco: int) -> str:
    disco = obter_disco(numero_disco)
    linhas = [
        "",
        "Estado final do HD novo:",
        disco.detalhe(),
        "",
        "Para usar o HD novo:",
        "  1. Desligue o computador e desconecte o HD antigo.",
        "  2. Ligue o HD novo na mesma porta/posicao do antigo.",
        "  3. No setup (UEFI/BIOS), confirme que o HD novo e a primeira opcao de boot.",
        "  4. Se aparecer erro de boot, inicie por um pendrive de instalacao do Windows,",
        "     abra o Prompt de Comando e rode: bcdboot C:\\Windows /s S: /f UEFI",
        "",
        "Mantenha o HD antigo intacto ate confirmar que o novo esta funcionando.",
    ]
    return "\n".join(linhas)
