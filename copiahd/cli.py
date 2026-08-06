"""Interface de linha de comando e menu interativo do Copia_hd."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from . import VERSAO
from .disks import (
    Disco,
    ErroInventario,
    definir_estado_disco,
    eh_admin,
    formatar_bytes,
    listar_discos,
    obter_disco,
)
from .fileclone import (
    ErroCopiaArquivos,
    cabe_no_destino,
    copiar_volume,
    particionar_destino,
    reconstruir_boot,
)
from .postclone import (
    ajustar_gpt_destino,
    conferir_arquivos_de_boot,
    expandir_ultima_particao,
    relatorio_final,
    verificar_sistemas_arquivos,
)
from .rawclone import (
    BLOCO_PADRAO,
    ErroClone,
    executar_clone,
    montar_plano,
    preparar_destino,
    verificar_copia,
)

PASTA_LOGS = Path(__file__).resolve().parent.parent / "logs"


class Registro:
    """Escreve simultaneamente no console e no arquivo de log."""

    def __init__(self, arquivo: Path):
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        self.arquivo = arquivo
        self._fp = arquivo.open("a", encoding="utf-8")

    def __call__(self, mensagem: str = "") -> None:
        print(mensagem)
        self._fp.write(f"{datetime.now():%H:%M:%S} {mensagem}\n")
        self._fp.flush()

    def fechar(self) -> None:
        self._fp.close()


def _progresso_console(progresso) -> None:
    sys.stdout.write("\r" + progresso.linha())
    sys.stdout.flush()


def mostrar_discos(registrar=print) -> list[Disco]:
    discos = listar_discos()
    registrar("")
    registrar("Discos detectados:")
    for d in discos:
        registrar(d.detalhe())
    registrar("")
    return discos


def _exigir_admin(registrar=print) -> None:
    if not eh_admin():
        registrar(
            "ERRO: acesso bruto a disco exige privilegio de administrador.\n"
            "      Feche e abra de novo como administrador (use Executar_como_Admin.bat)."
        )
        sys.exit(2)


def _confirmar(destino: Disco, forcar: bool, registrar=print) -> None:
    if forcar:
        registrar("Confirmacao dispensada por --sim.")
        return
    registrar("")
    registrar("=" * 70)
    registrar(f"TODO o conteudo do disco {destino.numero} sera APAGADO:")
    registrar(destino.detalhe())
    registrar("=" * 70)
    esperado = f"APAGAR {destino.numero}"
    resposta = input(f'Digite exatamente "{esperado}" para continuar: ').strip()
    if resposta != esperado:
        registrar("Cancelado pelo usuario.")
        sys.exit(1)


def comando_listar(_args) -> int:
    mostrar_discos()
    if not eh_admin():
        print("Aviso: sem privilegio de administrador; a clonagem exige elevacao.")
    return 0


def comando_clonar(args) -> int:
    _exigir_admin()
    registrar = Registro(PASTA_LOGS / f"clone_{datetime.now():%Y%m%d_%H%M%S}.log")
    try:
        origem = obter_disco(args.origem)
        destino = obter_disco(args.destino)
        registrar(f"Copia_hd {VERSAO} - clone setor a setor")
        registrar(f"Origem : {origem.resumo().strip()}")
        registrar(f"Destino: {destino.resumo().strip()}")

        plano = montar_plano(
            origem,
            destino,
            bloco=args.bloco,
            copiar_disco_inteiro=not args.recortar,
        )
        registrar("")
        registrar(
            f"Plano: copiar {formatar_bytes(plano.bytes_a_copiar)} "
            f"(setor {plano.setor} B, bloco {formatar_bytes(plano.bloco)}, modo {plano.modo})"
        )
        for aviso in plano.avisos:
            registrar(f"  AVISO: {aviso}")

        if args.simular:
            registrar("")
            registrar("[SIMULACAO] Nenhuma escrita realizada. Etapas que seriam feitas:")
            registrar("  1. Desmontar e travar volumes do destino")
            registrar(f"  2. Copiar {formatar_bytes(plano.bytes_a_copiar)} da origem ao destino")
            registrar(
                "  3. Corrigir GPT/MBR do destino para o tamanho real do disco novo"
                + (" e trocar o GUID" if args.novo_guid else " (identificadores preservados)")
            )
            if not args.sem_expandir:
                registrar("  4. Expandir a ultima particao NTFS para o espaco livre")
            registrar("  5. Verificar copia por amostragem e conferir arquivos de boot")
            registrar(ajustar_gpt_destino(destino.numero, simular=True).linha())
            return 0

        _confirmar(destino, args.sim, registrar)

        registrar("")
        registrar("Preparando destino...")
        preparar_destino(destino.numero, registrar)
        if args.offline_destino:
            try:
                definir_estado_disco(destino.numero, online=False)
                registrar(f"  Disco {destino.numero} colocado offline.")
            except ErroInventario as exc:
                registrar(f"  Aviso: nao foi possivel colocar offline - {exc}")

        registrar("")
        registrar("Copiando (Ctrl+C interrompe; o progresso e retomavel)...")
        estado = PASTA_LOGS / f"retomada_disco{origem.numero}_para{destino.numero}.json"
        inicio = time.monotonic()
        try:
            progresso = executar_clone(
                plano,
                registrar=registrar,
                aoprogresso=_progresso_console,
                arquivo_estado=estado,
                retomar=not args.reiniciar,
            )
        except KeyboardInterrupt:
            print()
            registrar("Interrompido. Rode o mesmo comando para retomar de onde parou.")
            return 130
        print()
        registrar(
            f"Copia concluida: {formatar_bytes(progresso.copiados)} em "
            f"{time.monotonic() - inicio:.0f}s "
            f"(media {formatar_bytes(int(progresso.taxa))}/s)."
        )

        if args.offline_destino:
            try:
                definir_estado_disco(destino.numero, online=True)
            except ErroInventario:
                pass

        registrar("")
        registrar("Ajustes finais no HD novo:")
        registrar(ajustar_gpt_destino(destino.numero, novo_guid=args.novo_guid).linha())
        if args.novo_guid:
            registrar(
                "  ATENCAO: o GUID do disco foi trocado. A configuracao de boot (BCD) "
                "copiada aponta para o GUID antigo. Se o HD novo nao inicializar, "
                "reconstrua o boot: bcdboot C:\\Windows /s S: /f UEFI (do proprio "
                "sistema migrado ou de um pendrive de instalacao)."
            )
        if not args.sem_expandir:
            time.sleep(2)  # deixa o Windows reler a tabela de particao
            registrar(expandir_ultima_particao(destino.numero).linha())

        if not args.sem_verificar:
            registrar("")
            registrar("Verificando a copia por amostragem...")
            ok, divergentes = verificar_copia(plano, amostras=args.amostras, registrar=registrar)
            registrar(
                "  Resultado: copia identica nas amostras."
                if ok
                else f"  Resultado: {len(divergentes)} amostra(s) divergente(s)!"
            )

        registrar("")
        registrar(conferir_arquivos_de_boot(destino.numero).linha())
        if args.chkdsk:
            for etapa in verificar_sistemas_arquivos(destino.numero):
                registrar(etapa.linha())
        registrar(relatorio_final(destino.numero))
        registrar(f"Log salvo em: {registrar.arquivo}")
        return 0
    except (ErroClone, ErroInventario) as exc:
        registrar(f"ERRO: {exc}")
        return 1
    finally:
        registrar.fechar()


def comando_arquivos(args) -> int:
    _exigir_admin()
    registrar = Registro(PASTA_LOGS / f"arquivos_{datetime.now():%Y%m%d_%H%M%S}.log")
    try:
        origem = obter_disco(args.origem)
        destino = obter_disco(args.destino)
        registrar(f"Copia_hd {VERSAO} - copia por arquivos (destino menor)")
        registrar(f"Origem : {origem.resumo().strip()}")
        registrar(f"Destino: {destino.resumo().strip()}")

        if destino.tem_sistema or destino.tem_boot:
            registrar("ERRO: o destino e o disco de sistema em uso.")
            return 1
        couber, detalhe = cabe_no_destino(origem, destino)
        registrar(f"  {detalhe}")
        if not couber and not args.sim:
            registrar("ERRO: destino pequeno demais para os dados da origem.")
            return 1

        volume_origem = args.volume_origem or _maior_volume(origem)
        if not volume_origem:
            registrar("ERRO: nenhum volume com letra na origem. Atribua uma letra e repita.")
            return 1
        registrar(f"  Volume de origem: {volume_origem}:")

        if origem.tem_sistema:
            registrar(
                "  AVISO: o Windows de origem esta em execucao. Arquivos abertos nao "
                "serao copiados e o resultado pode nao inicializar. Prefira rodar "
                "este modo a partir de um Windows PE."
            )

        if args.simular:
            registrar("")
            registrar(particionar_destino(destino.numero, args.letra_windows, args.letra_efi, simular=True))
            copiar_volume(
                f"{volume_origem}:",
                f"{args.letra_windows}:",
                PASTA_LOGS / "robocopy_simulacao.log",
                simular=True,
                registrar=registrar,
            )
            registrar(reconstruir_boot(args.letra_windows, args.letra_efi, simular=True))
            return 0

        _confirmar(destino, args.sim, registrar)

        registrar("")
        registrar("Particionando o destino (EFI + MSR + NTFS)...")
        registrar(particionar_destino(destino.numero, args.letra_windows, args.letra_efi))

        registrar("Copiando arquivos com robocopy (pode levar horas)...")
        log_robo = PASTA_LOGS / f"robocopy_{datetime.now():%Y%m%d_%H%M%S}.log"
        resultado = copiar_volume(
            f"{volume_origem}:", f"{args.letra_windows}:", log_robo, registrar=registrar
        )
        registrar(f"  robocopy: {resultado.descricao()}")
        if not resultado.ok:
            registrar(f"  Detalhes em {log_robo}")
            return 1

        registrar("Reconstruindo o boot com bcdboot...")
        registrar(f"  {reconstruir_boot(args.letra_windows, args.letra_efi)}")

        registrar("")
        registrar(conferir_arquivos_de_boot(destino.numero).linha())
        registrar(relatorio_final(destino.numero))
        return 0
    except (ErroCopiaArquivos, ErroInventario) as exc:
        registrar(f"ERRO: {exc}")
        return 1
    finally:
        registrar.fechar()


def _maior_volume(disco: Disco) -> str | None:
    candidatos = [p for p in disco.particoes if p.letra and p.sistema_arquivos]
    if not candidatos:
        return None
    return max(candidatos, key=lambda p: p.tamanho).letra


def comando_ajustar(args) -> int:
    _exigir_admin()
    print(ajustar_gpt_destino(args.disco, novo_guid=args.novo_guid, simular=args.simular).linha())
    if not args.sem_expandir:
        print(expandir_ultima_particao(args.disco, simular=args.simular).linha())
    print(conferir_arquivos_de_boot(args.disco).linha())
    return 0


def comando_verificar(args) -> int:
    _exigir_admin()
    origem = obter_disco(args.origem)
    destino = obter_disco(args.destino)
    plano = montar_plano(origem, destino, copiar_disco_inteiro=not args.recortar)
    ok, divergentes = verificar_copia(plano, amostras=args.amostras)
    print("Copia identica nas amostras." if ok else f"{len(divergentes)} divergencia(s).")
    print(conferir_arquivos_de_boot(destino.numero).linha())
    return 0 if ok else 1


def menu_interativo() -> int:
    print(f"\n=== Copia_hd {VERSAO} - clonar HD antigo para HD novo ===")
    if not eh_admin():
        print("\nSem privilegio de administrador. Abra pelo Executar_como_Admin.bat")
        print("para poder clonar. Voce ainda pode listar os discos.\n")
    discos = mostrar_discos()
    print("O que deseja fazer?")
    print("  1) Clonar disco (setor a setor) - HD novo igual ou maior [recomendado]")
    print("  2) Copiar por arquivos - HD novo menor que o antigo")
    print("  3) Simular um clone (nao escreve nada)")
    print("  4) Ajustar um HD ja clonado (GPT + expandir particao)")
    print("  5) Verificar uma copia ja feita")
    print("  0) Sair")
    escolha = input("Opcao: ").strip()
    if escolha == "0" or not escolha:
        return 0

    def pedir_disco(rotulo: str) -> int:
        while True:
            valor = input(f"Numero do disco de {rotulo}: ").strip()
            if valor.isdigit() and any(d.numero == int(valor) for d in discos):
                return int(valor)
            print("  Numero invalido.")

    if escolha in {"1", "2", "3"}:
        origem = pedir_disco("ORIGEM (HD antigo)")
        destino = pedir_disco("DESTINO (HD novo - sera apagado)")
        args = argparse.Namespace(
            origem=origem,
            destino=destino,
            bloco=BLOCO_PADRAO,
            simular=(escolha == "3"),
            sim=False,
            recortar=False,
            reiniciar=False,
            sem_expandir=False,
            sem_verificar=False,
            novo_guid=False,
            offline_destino=True,
            chkdsk=False,
            amostras=64,
            volume_origem=None,
            letra_windows="W",
            letra_efi="S",
        )
        if escolha == "2":
            return comando_arquivos(args)
        return comando_clonar(args)
    if escolha == "4":
        disco = pedir_disco("HD JA CLONADO")
        return comando_ajustar(
            argparse.Namespace(disco=disco, novo_guid=False, sem_expandir=False, simular=False)
        )
    if escolha == "5":
        origem = pedir_disco("ORIGEM")
        destino = pedir_disco("DESTINO")
        return comando_verificar(
            argparse.Namespace(origem=origem, destino=destino, amostras=64, recortar=False)
        )
    print("Opcao invalida.")
    return 1


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="copia_hd",
        description="Clona um HD para outro no Windows, mantendo o disco novo funcional e inicializavel.",
        epilog="Sem argumentos, abre o menu interativo.",
    )
    p.add_argument("--versao", action="version", version=f"Copia_hd {VERSAO}")
    sub = p.add_subparsers(dest="comando")

    sub.add_parser("listar", help="lista discos, particoes e volumes").set_defaults(
        func=comando_listar
    )

    c = sub.add_parser("clonar", help="clone setor a setor (destino igual ou maior)")
    c.add_argument("origem", type=int, help="numero do disco de origem")
    c.add_argument("destino", type=int, help="numero do disco de destino (SERA APAGADO)")
    c.add_argument("--bloco", type=int, default=BLOCO_PADRAO, help="tamanho do bloco em bytes")
    c.add_argument("--simular", action="store_true", help="nao escreve nada, apenas planeja")
    c.add_argument("--sim", action="store_true", help="dispensa a confirmacao digitada")
    c.add_argument(
        "--recortar",
        action="store_true",
        help="permite copiar so ate o fim da ultima particao (destino um pouco menor)",
    )
    c.add_argument("--reiniciar", action="store_true", help="ignora estado de retomada")
    c.add_argument("--sem-expandir", action="store_true", help="nao expande a ultima particao")
    c.add_argument("--sem-verificar", action="store_true", help="pula a verificacao por amostragem")
    c.add_argument(
        "--novo-guid",
        action="store_true",
        help="troca o GUID/assinatura do disco novo (permite os dois HDs ligados juntos, "
        "mas pode exigir reconstruir o boot com bcdboot)",
    )
    c.add_argument(
        "--offline-destino",
        action="store_true",
        default=True,
        help="coloca o disco destino offline durante a copia",
    )
    c.add_argument("--chkdsk", action="store_true", help="roda chkdsk nos volumes do destino")
    c.add_argument("--amostras", type=int, default=64, help="numero de amostras na verificacao")
    c.set_defaults(func=comando_clonar)

    a = sub.add_parser("arquivos", help="copia por arquivos (destino menor que a origem)")
    a.add_argument("origem", type=int)
    a.add_argument("destino", type=int)
    a.add_argument("--volume-origem", help="letra do volume Windows na origem (ex: C)")
    a.add_argument("--letra-windows", default="W", help="letra temporaria do Windows no destino")
    a.add_argument("--letra-efi", default="S", help="letra temporaria da particao EFI no destino")
    a.add_argument("--simular", action="store_true")
    a.add_argument("--sim", action="store_true")
    a.set_defaults(func=comando_arquivos)

    j = sub.add_parser("ajustar", help="corrige GPT e expande a particao de um HD ja clonado")
    j.add_argument("disco", type=int)
    j.add_argument("--novo-guid", action="store_true", help="troca o GUID/assinatura do disco")
    j.add_argument("--sem-expandir", action="store_true")
    j.add_argument("--simular", action="store_true")
    j.set_defaults(func=comando_ajustar)

    v = sub.add_parser("verificar", help="compara origem e destino por amostragem")
    v.add_argument("origem", type=int)
    v.add_argument("destino", type=int)
    v.add_argument("--amostras", type=int, default=64)
    v.add_argument("--recortar", action="store_true")
    v.set_defaults(func=comando_verificar)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return menu_interativo()
    args = construir_parser().parse_args(argv)
    if not getattr(args, "func", None):
        return menu_interativo()
    return args.func(args)
