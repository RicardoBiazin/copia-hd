# Copia_hd

Clona um HD para outro no Windows deixando o disco novo **funcional e inicializável**,
pronto para substituir o antigo.

O que garante o boot no HD novo é copiar o disco **inteiro** — setor 0 até o fim —
e não apenas os arquivos. Isso preserva a tabela de partições, a partição de
sistema EFI, a partição reservada, o Windows e a partição de recuperação, com
todos os identificadores que a configuração de boot referencia.

## Requisitos

- Windows 10/11, Python 3.10+
- Executar **como administrador** (acesso a disco físico exige elevação)
- O HD novo ligado (SATA, ou gaveta/adaptador USB)

## Uso

Duplo clique em `Executar_como_Admin.bat` abre o menu interativo. Por linha de comando:

```
python app.py listar                 # mostra discos, partições e volumes
python app.py clonar 0 1 --simular   # planeja o clone sem escrever nada
python app.py clonar 0 1             # clona o disco 0 no disco 1
python app.py arquivos 0 1           # cópia por arquivos (destino menor)
python app.py ajustar 1              # corrige GPT e expande partição de um clone
python app.py verificar 0 1          # confere a cópia por amostragem
```

**Sempre rode `--simular` primeiro** e confirme que os números de disco estão certos.
O destino é apagado por completo.

## Como funciona o clone (modo recomendado)

1. **Validação** — recusa destino menor, destino que é o disco de sistema em uso,
   e discos com tamanho de setor diferente (512e vs 4Kn não geram sistema que dá boot).
2. **Preparação** — desmonta e trava os volumes do destino, e o coloca offline.
3. **Cópia setor a setor** — blocos de 4 MB com I/O sem buffer, progresso com
   velocidade e tempo restante. `Ctrl+C` interrompe e o mesmo comando **retoma**
   de onde parou (estado em `logs/`).
4. **Setores defeituosos** — a leitura cai para setor a setor, tenta 3 vezes, e só
   então grava zeros no lugar, registrando a LBA. O clone continua em vez de abortar.
5. **Correção da GPT** — depois de copiar para um disco de tamanho diferente, a GPT
   fica inconsistente: o cabeçalho de backup aponta para o último setor do disco
   *antigo*. O programa reescreve o cabeçalho primário e o secundário, recoloca a
   cópia das entradas no fim do disco novo, recalcula os CRC-32 e ajusta a MBR
   protetiva para cobrir o disco inteiro.
6. **Expansão** — estende a partição de dados (o Windows) para o espaço extra.
7. **Verificação** — compara 64 amostras de 1 MB entre origem e destino por SHA-256
   e confere os arquivos de boot na partição EFI.

## Identificadores do disco: por que não são trocados por padrão

A configuração de boot (BCD) copiada da origem referencia o disco pelo **GUID**
(GPT) ou pela **assinatura** (MBR). Trocar esse identificador sem reconstruir o boot
faz o Windows não inicializar. Por isso o padrão é **preservar**.

A consequência: com os dois HDs ligados ao mesmo tempo, o Windows vê discos
duplicados e mantém os volumes do clone sem letra ou offline. Isso é normal e não
indica falha — desconecte o HD antigo antes de usar o novo.

Se você precisa dos dois discos utilizáveis simultaneamente, use `--novo-guid` e
reconstrua o boot depois:

```
bcdboot C:\Windows /s S: /f UEFI
```

## Modo cópia por arquivos (`arquivos`)

Para quando o HD novo é **menor** que o antigo. Particiona o destino do zero
(EFI + MSR + NTFS), copia com `robocopy /MIR /COPYALL` preservando ACLs, donos,
atributos e timestamps, e reconstrói o boot com `bcdboot`.

Exclui o que o Windows recria sozinho: `pagefile.sys`, `swapfile.sys`,
`hiberfil.sys`, `$RECYCLE.BIN`, `System Volume Information`.

**Limitação importante:** arquivos abertos pelo Windows em execução não são
copiados. Rode este modo a partir de um Windows PE, com os dois discos como
secundários, para que o resultado seja confiável.

## Cópia a quente

Clonar o disco do Windows enquanto ele está rodando produz uma imagem de um
sistema de arquivos em movimento — o equivalente a um desligamento forçado. Na
prática o Windows costuma reparar isso no primeiro boot, mas para uma migração
confiável o correto é rodar de um Windows PE / outro sistema, com o disco de
origem parado. O programa avisa quando detecta esse caso.

## Depois do clone

1. Desligue e desconecte o HD antigo.
2. Ligue o HD novo na mesma porta do antigo.
3. Confirme no setup UEFI/BIOS que o HD novo é a primeira opção de boot.
4. Se der erro de boot: inicie por um pendrive de instalação do Windows,
   Prompt de Comando, e rode `bcdboot C:\Windows /s S: /f UEFI`.

Mantenha o HD antigo intacto até confirmar que o novo funciona.

### Espaço sobrando por causa da partição de recuperação

Em disco de Windows a partição de recuperação normalmente fica **depois** do
volume C:. O espaço extra do HD novo cai atrás dela, e por não ser contíguo o
Windows não consegue estender o C: automaticamente — o programa avisa quando isso
acontece. O clone dá boot normalmente assim. Para aproveitar o espaço depois, já
no sistema migrado: apague a partição de recuperação, estenda o volume do Windows
e recrie a recuperação com `reagentc /enable`.

## Testes

```
python -m tests.test_copiahd
```

65 verificações sobre imagens de disco sintéticas — GPT construída à mão, volume
FAT32 com nomes longos, simulação de setor defeituoso. Nenhum disco real é tocado.

## Estrutura

| Arquivo | Função |
| --- | --- |
| `app.py` | ponto de entrada |
| `copiahd/cli.py` | menu interativo e linha de comando |
| `copiahd/winio.py` | acesso bruto a disco via ctypes (CreateFileW, IOCTLs) |
| `copiahd/disks.py` | inventário de discos/partições via cmdlets Storage |
| `copiahd/rawclone.py` | clone setor a setor, retomada, setores defeituosos |
| `copiahd/gpt.py` | correção de GPT/MBR para o tamanho do disco novo |
| `copiahd/fat.py` | leitor FAT somente leitura (inspeciona a partição EFI) |
| `copiahd/fileclone.py` | cópia por arquivos com robocopy + bcdboot |
| `copiahd/postclone.py` | expansão de partição, chkdsk, conferência de boot |
| `logs/` | logs de execução e estado de retomada |
